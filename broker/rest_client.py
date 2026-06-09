"""
Crypto.com Exchange v1 REST API client.
Auth: HMAC-SHA256. Rate limit: 10 req/s enforced per endpoint.
Retries: 5 attempts, exponential backoff, handles 10006 rate limit in both GET and POST.
FIX: _get handles rate limit code 10006 with retry (was raising immediately).
FIX: OCO signature uses json.dumps for nested list — Python repr broke HMAC.
FIX: create_oco_order accepts direction param — SHORT exits place BUY orders.
"""
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import settings

logger = logging.getLogger(__name__)
_REQ_COUNTER = 0


def _next_id() -> int:
    global _REQ_COUNTER
    _REQ_COUNTER += 1
    return _REQ_COUNTER


def _build_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=1.0, status_forcelist=[500, 502, 503, 504],
                  allowed_methods=["GET", "POST"])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def _param_str(params: Dict) -> str:
    """
    Build signature param string per Crypto.com v1 spec.
    FIX: list/dict values are JSON-serialized (not Python repr) so HMAC matches server.
    """
    parts = []
    for k in sorted(params):
        v = params[k]
        if isinstance(v, (list, dict)):
            parts.append(f"{k}{json.dumps(v, separators=(',', ':'))}")
        else:
            parts.append(f"{k}{v}")
    return "".join(parts)


class CryptoComRestClient:
    def __init__(self):
        self._session   = _build_session()
        self._last_call: Dict[str, float] = {}
        self._min_gap   = 0.105  # 100ms + 5ms buffer → ~9.5 req/s safe ceiling

    def _throttle(self, endpoint: str) -> None:
        now  = time.monotonic()
        wait = self._min_gap - (now - self._last_call.get(endpoint, 0.0))
        if wait > 0:
            time.sleep(wait)
        self._last_call[endpoint] = time.monotonic()

    def _sign(self, method: str, req_id: int, params: Dict, nonce: int) -> str:
        raw = f"{method}{req_id}{settings.API_KEY}{_param_str(params)}{nonce}"
        return hmac.new(settings.API_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()

    def _post(self, method: str, params: Optional[Dict] = None,
              private: bool = True) -> Dict:
        params  = params or {}
        nonce   = int(time.time() * 1000)
        req_id  = _next_id()
        body: Dict[str, Any] = {
            "id": req_id, "method": method,
            "nonce": nonce, "params": params,
        }
        if private:
            if not settings.API_KEY or not settings.API_SECRET:
                raise ValueError("API credentials missing for private endpoint")
            body["api_key"] = settings.API_KEY
            body["sig"]     = self._sign(method, req_id, params, nonce)

        url = f"{settings.REST_BASE_URL}/{method}"
        self._throttle(method)

        for attempt in range(5):
            try:
                resp = self._session.post(url, json=body, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") == 10006:
                    wait = 2 ** attempt
                    logger.warning("Rate limit POST %s attempt %d — retry in %ds", method, attempt + 1, wait)
                    time.sleep(wait)
                    continue
                if data.get("code") != 0:
                    raise RuntimeError(f"API error {data.get('code')}: {data.get('message')}")
                return data.get("result", {})
            except requests.exceptions.RequestException as e:
                wait = 2 ** attempt
                logger.warning("HTTP error POST %s attempt %d: %s — retry %ds", method, attempt + 1, e, wait)
                time.sleep(wait)

        raise RuntimeError(f"All retries exhausted: POST {method}")

    def _get(self, path: str, params: Optional[Dict] = None) -> Dict:
        """FIX: Added rate limit (10006) retry matching _post behavior."""
        url = f"{settings.REST_BASE_URL}/{path}"
        self._throttle(path)

        for attempt in range(5):
            try:
                resp = self._session.get(url, params=params, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") == 10006:
                    wait = 2 ** attempt
                    logger.warning("Rate limit GET %s attempt %d — retry in %ds", path, attempt + 1, wait)
                    time.sleep(wait)
                    continue
                if data.get("code") != 0:
                    raise RuntimeError(f"API error {data.get('code')}: {data.get('message')}")
                return data.get("result", {})
            except requests.exceptions.RequestException as e:
                wait = 2 ** attempt
                logger.warning("HTTP error GET %s attempt %d: %s — retry %ds", path, attempt + 1, e, wait)
                time.sleep(wait)

        raise RuntimeError(f"All retries exhausted: GET {path}")

    # ── Market data ───────────────────────────────────────────────────────────

    def get_ticker(self, instrument_name: str) -> Dict:
        return self._get("public/get-tickers", {"instrument_name": instrument_name})

    def get_book(self, instrument_name: str, depth: int = 10) -> Dict:
        return self._get("public/get-book",
                         {"instrument_name": instrument_name, "depth": depth})

    def get_candlesticks(self, instrument_name: str, timeframe: str,
                         count: int = 50,
                         start_ts: Optional[int] = None,
                         end_ts:   Optional[int] = None) -> List[Dict]:
        params: Dict[str, Any] = {
            "instrument_name": instrument_name,
            "timeframe": timeframe,
            "count": count,
        }
        if start_ts is not None:
            params["start_ts"] = start_ts
        if end_ts is not None:
            params["end_ts"] = end_ts
        result = self._get("public/get-candlestick", params)
        return result.get("data", [])

    def get_instruments(self) -> List[Dict]:
        return self._get("public/get-instruments").get("data", [])

    def get_trades(self, instrument_name: str, count: int = 10) -> List[Dict]:
        return self._get("public/get-trades",
                         {"instrument_name": instrument_name, "count": count}).get("data", [])

    # ── Account ───────────────────────────────────────────────────────────────

    def get_balance(self) -> List[Dict]:
        return self._post("private/user-balance").get("data", [])

    def get_usdt_balance(self) -> float:
        for entry in self.get_balance():
            balances = entry.get("position_balances", [entry] if isinstance(entry, dict) else [])
            for pos in balances:
                if isinstance(pos, dict) and pos.get("instrument_name") == "USDT":
                    return float(pos.get("quantity", 0))
        return 0.0

    # ── Orders ────────────────────────────────────────────────────────────────

    def create_limit_order(self, instrument: str, side: str, price: float,
                           quantity: float, client_oid: Optional[str] = None,
                           post_only: bool = False) -> Dict:
        params: Dict[str, Any] = {
            "instrument_name": instrument,
            "side":            side.upper(),
            "type":            "LIMIT",
            "price":           str(round(price, 2)),
            "quantity":        str(quantity),
            "time_in_force":   "GOOD_TILL_CANCEL",
        }
        if post_only:
            params["post_only"] = True
        if client_oid:
            params["client_oid"] = client_oid
        if settings.DRY_RUN:
            oid = f"DRY_{int(time.time() * 1000)}"
            logger.info("[DRY_RUN] create_limit_order: %s → %s", params, oid)
            return {"order_id": oid}
        return self._post("private/create-order", params=params)

    def create_stop_limit_order(self, instrument: str, side: str,
                                trigger_price: float, limit_price: float,
                                quantity: float,
                                client_oid: Optional[str] = None) -> Dict:
        params: Dict[str, Any] = {
            "instrument_name": instrument,
            "side":            side.upper(),
            "type":            "STOP_LIMIT",
            "price":           str(round(limit_price, 2)),
            "quantity":        str(quantity),
            "ref_price":       str(round(trigger_price, 2)),
            "ref_price_type":  "MARK_PRICE",
            "time_in_force":   "GOOD_TILL_CANCEL",
        }
        if client_oid:
            params["client_oid"] = client_oid
        if settings.DRY_RUN:
            oid = f"DRY_SL_{int(time.time() * 1000)}"
            logger.info("[DRY_RUN] create_stop_limit: %s → %s", params, oid)
            return {"order_id": oid}
        return self._post("private/create-order", params=params)

    def create_oco_order(self, instrument: str, quantity: float,
                         stop_trigger: float, stop_limit: float,
                         take_profit: float, direction: str = "LONG",
                         client_oid_prefix: str = "") -> Dict:
        """
        FIX: exit_side derived from direction.
        LONG position exits → SELL both legs.
        SHORT position exits → BUY both legs.
        Original code hardcoded 'SELL' for both legs — SHORT exits doubled exposure.
        """
        exit_side = "SELL" if direction == "LONG" else "BUY"
        order_list = [
            {
                "instrument_name": instrument,
                "side":            exit_side,
                "type":            "STOP_LIMIT",
                "price":           str(round(stop_limit, 2)),
                "quantity":        str(quantity),
                "ref_price":       str(round(stop_trigger, 2)),
                "ref_price_type":  "MARK_PRICE",
                "time_in_force":   "GOOD_TILL_CANCEL",
                **({"client_oid": f"{client_oid_prefix}_stop"} if client_oid_prefix else {}),
            },
            {
                "instrument_name": instrument,
                "side":            exit_side,
                "type":            "LIMIT",
                "price":           str(round(take_profit, 2)),
                "quantity":        str(quantity),
                "time_in_force":   "GOOD_TILL_CANCEL",
                **({"client_oid": f"{client_oid_prefix}_tp"} if client_oid_prefix else {}),
            },
        ]
        params: Dict[str, Any] = {
            "contingency_type": "OCO",
            "order_list":       order_list,
        }
        if settings.DRY_RUN:
            oid = f"DRY_OCO_{int(time.time() * 1000)}"
            logger.info("[DRY_RUN] create_oco direction=%s side=%s → %s", direction, exit_side, oid)
            return {"order_list_id": oid, "order_ids": [f"{oid}_stop", f"{oid}_tp"]}
        return self._post("private/create-order-list", params=params)

    def cancel_order(self, instrument: str, order_id: str) -> Dict:
        if settings.DRY_RUN:
            return {"order_id": order_id, "status": "CANCELLED"}
        return self._post("private/cancel-order",
                          params={"instrument_name": instrument, "order_id": order_id})

    def cancel_all_orders(self, instrument: Optional[str] = None) -> Dict:
        params: Dict[str, Any] = {}
        if instrument:
            params["instrument_name"] = instrument
        if settings.DRY_RUN:
            return {"status": "OK"}
        return self._post("private/cancel-all-orders", params=params)

    def get_order_detail(self, order_id: str) -> Dict:
        return self._post("private/get-order-detail", params={"order_id": order_id})

    def get_open_orders(self, instrument: Optional[str] = None) -> List[Dict]:
        params: Dict[str, Any] = {"page_size": 50}
        if instrument:
            params["instrument_name"] = instrument
        return self._post("private/get-open-orders", params=params).get("order_list", [])

    def set_cancel_on_disconnect(self, scope: str = "CONNECTION") -> Dict:
        if settings.DRY_RUN:
            return {"scope": scope, "status": "SET"}
        return self._post("private/set-cancel-on-disconnect", params={"scope": scope})

    def get_trade_history(self, instrument: str,
                          start_ms: int, end_ms: int) -> List[Dict]:
        params: Dict[str, Any] = {
            "instrument_name": instrument,
            "start_time":      str(start_ms),
            "end_time":        str(end_ms),
            "page_size":       200,
        }
        return self._post("private/get-trades", params=params).get("trade_list", [])
