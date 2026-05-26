"""
Crypto.com Exchange v1 REST API client.
Auth: HMAC-SHA256 per Crypto.com API v1 spec.
FIX: param_str serialisation: non-scalar (list/dict) values are json.dumps'd
     so HMAC matches what the server computes for nested params (e.g. OCO order_list).
Retry: 5 attempts exponential backoff. Rate limit: 101ms/endpoint.
"""
import hashlib, hmac, json, logging, time
from typing import Any, Dict, List, Optional
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from config import settings

logger = logging.getLogger(__name__)
_REQ_ID = 0


def _next_id() -> int:
    global _REQ_ID
    _REQ_ID += 1
    return _REQ_ID


def _build_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=1.0,
                  status_forcelist=[500, 502, 503, 504],
                  allowed_methods=["POST", "GET"])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def _param_value_str(v: Any) -> str:
    """FIX: serialise non-scalar param values as JSON for HMAC param_str construction."""
    if isinstance(v, (list, dict)):
        return json.dumps(v, separators=(",", ":"))
    return str(v)


class CryptoComRestClient:
    def __init__(self):
        self._session = _build_session()
        self._last_call: Dict[str, float] = {}
        self._rate_s = 0.101  # 10 req/s per endpoint

    def _throttle(self, endpoint: str) -> None:
        now = time.monotonic()
        wait = self._rate_s - (now - self._last_call.get(endpoint, 0.0))
        if wait > 0:
            time.sleep(wait)
        self._last_call[endpoint] = time.monotonic()

    def _post(self, method: str, params: Optional[Dict] = None, private: bool = True) -> Dict:
        params = params or {}
        nonce  = int(time.time() * 1000)
        req_id = _next_id()
        body: Dict[str, Any] = {
            "id": req_id, "method": method, "nonce": nonce, "params": params,
        }
        if private:
            if not settings.API_KEY or not settings.API_SECRET:
                raise ValueError("API_KEY and API_SECRET required for private endpoints")
            # FIX: json.dumps non-scalar values (e.g. order_list) in param_str
            param_str = "".join(
                f"{k}{_param_value_str(params[k])}" for k in sorted(params)
            )
            raw = f"{method}{req_id}{settings.API_KEY}{param_str}{nonce}"
            sig = hmac.new(
                settings.API_SECRET.encode(), raw.encode(), hashlib.sha256
            ).hexdigest()
            body["api_key"] = settings.API_KEY
            body["sig"]     = sig

        url = f"{settings.REST_BASE_URL}/{method}"
        self._throttle(method)
        last_exc = None
        for attempt in range(5):
            try:
                resp = self._session.post(url, json=body, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                code = data.get("code", -1)
                if code == 10006:  # rate limit
                    wait = 2 ** attempt
                    logger.warning("Rate limit on %s — retry in %ds", method, wait)
                    time.sleep(wait)
                    continue
                if code != 0:
                    raise RuntimeError(f"API error {code}: {data.get('message', '')}")
                return data.get("result", {})
            except requests.exceptions.RequestException as exc:
                wait = 2 ** attempt
                logger.warning("HTTP error %s attempt %d: %s — retry in %ds",
                               method, attempt + 1, exc, wait)
                time.sleep(wait)
                last_exc = exc
        raise RuntimeError(f"All retries exhausted for {method}") from last_exc

    def _get(self, path: str, params: Optional[Dict] = None) -> Dict:
        url = f"{settings.REST_BASE_URL}/{path}"
        self._throttle(path)
        last_exc = None
        for attempt in range(5):
            try:
                resp = self._session.get(url, params=params, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                code = data.get("code", -1)
                if code == 10006:
                    wait = 2 ** attempt
                    logger.warning("GET rate limit on %s — retry in %ds", path, wait)
                    time.sleep(wait)
                    continue
                if code != 0:
                    raise RuntimeError(f"API error {code}: {data.get('message', '')}")
                return data.get("result", {})
            except requests.exceptions.RequestException as exc:
                wait = 2 ** attempt
                logger.warning("GET %s attempt %d: %s — retry in %ds", path, attempt + 1, exc, wait)
                time.sleep(wait)
                last_exc = exc
        raise RuntimeError(f"All retries exhausted for {path}") from last_exc

    # ── Public endpoints ─────────────────────────────────────────────────────
    def get_ticker(self, instrument_name: str) -> Dict:
        return self._get("public/get-tickers", {"instrument_name": instrument_name})

    def get_book(self, instrument_name: str, depth: int = 10) -> Dict:
        return self._get("public/get-book",
                         {"instrument_name": instrument_name, "depth": depth})

    def get_candlesticks(self, instrument_name: str, timeframe: str, count: int = 50) -> List:
        data = self._get("public/get-candlestick",
                         {"instrument_name": instrument_name,
                          "timeframe": timeframe, "count": count})
        candles = data.get("data", [])
        # Normalise field names: Crypto.com returns t, o, h, l, c, v
        # Add volume_usd as alias for v (volume in base asset * close price)
        result = []
        for c in candles:
            base_vol = float(c.get("v", 0))
            close    = float(c.get("c", 0))
            c["volume_usd"] = base_vol * close
            result.append(c)
        return sorted(result, key=lambda x: x["t"])

    def get_instruments(self) -> List:
        return self._get("public/get-instruments").get("data", [])

    def get_trades(self, instrument_name: str, count: int = 10) -> List:
        return self._get("public/get-trades",
                         {"instrument_name": instrument_name, "count": count}).get("data", [])

    # ── Private endpoints ─────────────────────────────────────────────────────
    def get_balance(self) -> List:
        return self._post("private/user-balance").get("data", [])

    def get_usdt_balance(self) -> float:
        """Walk balance response regardless of nesting structure."""
        for entry in self.get_balance():
            # Flat structure
            if isinstance(entry, dict) and entry.get("instrument_name") == "USDT":
                return float(entry.get("quantity", 0))
            # Nested position_balances
            for pos in entry.get("position_balances", []):
                if isinstance(pos, dict) and pos.get("instrument_name") == "USDT":
                    return float(pos.get("quantity", 0))
        return 0.0

    def create_limit_order(self, instrument: str, side: str, price: float,
                           quantity: float, client_oid: Optional[str] = None,
                           post_only: bool = True) -> Dict:
        params: Dict[str, Any] = {
            "instrument_name": instrument,
            "side":            side.upper(),
            "type":            "LIMIT",
            "price":           str(round(price, 2)),
            "quantity":        str(quantity),
            "time_in_force":   "GOOD_TILL_CANCEL",
        }
        if post_only: params["post_only"] = True
        if client_oid: params["client_oid"] = client_oid
        if settings.DRY_RUN:
            logger.info("[DRY_RUN] create_limit_order: %s", params)
            return {"order_id": f"DRY_{int(time.time() * 1000)}"}
        return self._post("private/create-order", params=params)

    def create_stop_limit_order(self, instrument: str, side: str,
                                trigger_price: float, limit_price: float,
                                quantity: float, client_oid: Optional[str] = None,
                                ref_price_type: str = "MARK_PRICE") -> Dict:
        params: Dict[str, Any] = {
            "instrument_name": instrument,
            "side":            side.upper(),
            "type":            "STOP_LIMIT",
            "price":           str(round(limit_price, 2)),
            "quantity":        str(quantity),
            "ref_price":       str(round(trigger_price, 2)),
            "ref_price_type":  ref_price_type,
            "time_in_force":   "GOOD_TILL_CANCEL",
        }
        if client_oid: params["client_oid"] = client_oid
        if settings.DRY_RUN:
            logger.info("[DRY_RUN] create_stop_limit_order: %s", params)
            return {"order_id": f"DRY_SL_{int(time.time() * 1000)}"}
        return self._post("private/create-order", params=params)

    def create_oco_order(self, instrument: str, quantity: float,
                         stop_trigger: float, stop_limit: float,
                         take_profit: float, client_oid_prefix: str = "",
                         direction: str = "LONG") -> Dict:
        """
        OCO: two-leg simultaneous order.
        LONG exits: SELL stop-limit + SELL limit take-profit.
        SHORT exits: BUY stop-limit + BUY limit take-profit.
        param_str HMAC uses json.dumps for order_list (FIX applied via _param_value_str).
        """
        exit_side = "SELL" if direction == "LONG" else "BUY"
        order_list = [
            {
                "instrument_name": instrument, "side": exit_side,
                "type":            "STOP_LIMIT",
                "price":           str(round(stop_limit, 2)),
                "quantity":        str(quantity),
                "ref_price":       str(round(stop_trigger, 2)),
                "ref_price_type":  "MARK_PRICE",
                "time_in_force":   "GOOD_TILL_CANCEL",
                **( {"client_oid": f"{client_oid_prefix}_stop"} if client_oid_prefix else {} ),
            },
            {
                "instrument_name": instrument, "side": exit_side,
                "type":            "LIMIT",
                "price":           str(round(take_profit, 2)),
                "quantity":        str(quantity),
                "time_in_force":   "GOOD_TILL_CANCEL",
                **( {"client_oid": f"{client_oid_prefix}_tp"} if client_oid_prefix else {} ),
            },
        ]
        if settings.DRY_RUN:
            logger.info("[DRY_RUN] create_oco_order dir=%s exit_side=%s stop=%.2f tp=%.2f",
                        direction, exit_side, stop_trigger, take_profit)
            return {
                "order_list_id": f"DRY_OCO_{int(time.time() * 1000)}",
                "order_ids":     ["DRY_SL", "DRY_TP"],
            }
        # _post's param_str will json.dumps the order_list value via _param_value_str
        params: Dict[str, Any] = {"contingency_type": "OCO", "order_list": order_list}
        return self._post("private/create-order-list", params=params)

    def cancel_order(self, instrument: str, order_id: str) -> Dict:
        if settings.DRY_RUN:
            return {"order_id": order_id, "status": "CANCELLED"}
        return self._post("private/cancel-order",
                          params={"instrument_name": instrument, "order_id": order_id})

    def cancel_all_orders(self, instrument: Optional[str] = None) -> Dict:
        params: Dict[str, Any] = {}
        if instrument: params["instrument_name"] = instrument
        if settings.DRY_RUN: return {"status": "OK"}
        return self._post("private/cancel-all-orders", params=params)

    def get_order_detail(self, order_id: str) -> Dict:
        return self._post("private/get-order-detail", params={"order_id": order_id})

    def get_open_orders(self, instrument: Optional[str] = None) -> List:
        params: Dict[str, Any] = {"page_size": 50}
        if instrument: params["instrument_name"] = instrument
        return self._post("private/get-open-orders", params=params).get("order_list", [])

    def set_cancel_on_disconnect(self, scope: str = "CONNECTION") -> Dict:
        if settings.DRY_RUN: return {"scope": scope, "status": "SET"}
        return self._post("private/set-cancel-on-disconnect", params={"scope": scope})

    def get_trade_history(self, instrument: str, start_ms: int, end_ms: int) -> List:
        params = {
            "instrument_name": instrument,
            "start_time":      str(start_ms),
            "end_time":        str(end_ms),
            "page_size":       200,
        }
        return self._post("private/get-trades", params=params).get("trade_list", [])
