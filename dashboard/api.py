"""
Dashboard API + self-contained HTML dashboard.
GET  /           → full dashboard (no external deps at runtime)
GET  /api/state  → live bot state JSON (polled every 2s)
GET  /api/trades → SQLite trade history (polled every 6s)
POST /api/kill   → trigger kill switch
GET  /api/health → Railway health check
"""
import logging
import time

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from dashboard.persistence import fetch_trades, fetch_daily_summary
from dashboard.state_store import store

logger = logging.getLogger(__name__)
app = FastAPI(title="Bot Dashboard", docs_url=None, redoc_url=None)


@app.get("/api/health")
async def health():
    return {"status": "ok", "ts": int(time.time())}


@app.get("/api/state")
async def get_state():
    return JSONResponse(store.snapshot())


@app.get("/api/trades")
async def get_trades():
    try:
        trades  = await fetch_trades(50)
        summary = await fetch_daily_summary()
    except Exception as exc:
        logger.warning("DB read error: %s", exc)
        trades  = store.recent_trades(50)
        summary = {"today_trades": 0, "today_pnl": 0.0}
    return JSONResponse({"trades": trades, "summary": summary})


@app.post("/api/kill")
async def kill():
    if store._kill_triggered:
        return JSONResponse({"status": "already_triggered"})
    store.trigger_kill()
    logger.warning("KILL SWITCH triggered via dashboard")
    return JSONResponse({"status": "triggered"})


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang='en'>
<head>
<meta charset='UTF-8'>
<meta name='viewport' content='width=device-width, initial-scale=1.0'>
<title>Bot Terminal</title>
<link rel='preconnect' href='https://fonts.googleapis.com'>
<link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>
<link href='https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@400;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap' rel='stylesheet'>
<style>
/* ── Tokens ──────────────────────────────────────────────────────── */
:root {
  --bg:          oklch(9% 0.012 240);
  --surf:        oklch(13% 0.016 240);
  --surf-2:      oklch(16% 0.018 240);
  --border:      oklch(21% 0.022 240);
  --border-hi:   oklch(30% 0.028 240);

  --gold:        oklch(76% 0.16 85);
  --gold-dim:    oklch(18% 0.05 85);
  --gold-glow:   oklch(76% 0.16 85 / 0.15);

  --green:       oklch(70% 0.18 145);
  --green-dim:   oklch(16% 0.07 145);
  --red:         oklch(60% 0.22 20);
  --red-dim:     oklch(15% 0.08 20);
  --blue:        oklch(65% 0.15 250);
  --blue-dim:    oklch(14% 0.06 250);
  --amber:       oklch(75% 0.17 70);
  --amber-dim:   oklch(18% 0.06 70);

  --text:        oklch(88% 0.008 240);
  --muted:       oklch(52% 0.01 240);
  --faint:       oklch(35% 0.01 240);

  --font-display: 'Barlow Condensed', sans-serif;
  --font-mono:    'JetBrains Mono', monospace;

  --r8:  0.5rem;
  --r4:  0.25rem;
}

/* ── Reset ───────────────────────────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { font-size: 16px; }
body {
  font-family: var(--font-mono);
  font-size: 0.8125rem;
  color: var(--text);
  min-height: 100vh;
  background:
    radial-gradient(ellipse 70% 55% at 8% 0%,    oklch(14% 0.04 250 / 0.65) 0%, transparent 55%),
    radial-gradient(ellipse 50% 40% at 92% 100%,  oklch(22% 0.07 85  / 0.28) 0%, transparent 50%),
    var(--bg);
  line-height: 1.5;
}
/* Scanline atmosphere */
body::after {
  content: '';
  position: fixed; inset: 0;
  background: repeating-linear-gradient(
    0deg,
    transparent, transparent 2px,
    oklch(100% 0 0 / 0.012) 2px, oklch(100% 0 0 / 0.012) 3px
  );
  pointer-events: none;
  z-index: 9999;
}

/* ── Banners ─────────────────────────────────────────────────────── */
#banner-dry {
  display: none;
  background: var(--amber-dim);
  border-bottom: 1px solid var(--amber);
  color: var(--amber);
  font-family: var(--font-display);
  font-weight: 700;
  font-size: 0.875rem;
  letter-spacing: 0.18em;
  text-align: center;
  padding: 0.5rem;
  text-transform: uppercase;
}
#banner-kill {
  display: none;
  background: var(--red-dim);
  border-bottom: 2px solid var(--red);
  color: var(--red);
  font-family: var(--font-display);
  font-weight: 800;
  font-size: 0.9375rem;
  letter-spacing: 0.2em;
  text-align: center;
  padding: 0.625rem;
  text-transform: uppercase;
  animation: pulse-border 1.8s ease-in-out infinite;
}
@keyframes pulse-border {
  0%,100% { border-color: var(--red); }
  50%      { border-color: oklch(60% 0.22 20 / 0.3); }
}

/* ── Header ──────────────────────────────────────────────────────── */
header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 1.125rem 2rem 1rem;
  border-bottom: 1px solid var(--border);
  position: sticky; top: 0;
  background: oklch(9% 0.012 240 / 0.95);
  backdrop-filter: blur(8px);
  z-index: 100;
}
.wordmark {
  font-family: var(--font-display);
  font-weight: 800;
  font-size: 1.5rem;
  letter-spacing: 0.12em;
  color: var(--text);
  text-transform: uppercase;
}
.wordmark em {
  color: var(--gold);
  font-style: normal;
}
.header-meta {
  display: flex;
  align-items: center;
  gap: 2rem;
}
.live-indicator {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.75rem;
  color: var(--muted);
  letter-spacing: 0.04em;
}
.pulse-dot {
  width: 0.5rem; height: 0.5rem;
  border-radius: 50%;
  background: var(--green);
  animation: heartbeat 2s ease-in-out infinite;
}
.pulse-dot.offline { background: var(--red); animation: none; }
@keyframes heartbeat {
  0%,100% { opacity: 1; transform: scale(1); }
  50%      { opacity: 0.3; transform: scale(0.8); }
}
#hdr-uptime { font-size: 0.75rem; color: var(--muted); font-family: var(--font-mono); }
#hdr-refresh { font-size: 0.6875rem; color: var(--faint); }

/* ── Status strip ────────────────────────────────────────────────── */
#status-strip {
  display: flex;
  align-items: center;
  gap: 0;
  padding: 0;
  border-bottom: 1px solid var(--border);
  overflow-x: auto;
  font-family: var(--font-display);
  font-weight: 600;
  font-size: 0.8125rem;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  opacity: 0;
  transform: translateY(-4px);
  transition: opacity 0.4s ease 0.1s, transform 0.4s ease 0.1s;
}
#status-strip.visible { opacity: 1; transform: translateY(0); }
.strip-cell {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  padding: 0.625rem 1.5rem;
  border-right: 1px solid var(--border);
  min-width: 10rem;
}
.strip-cell:last-child { border-right: none; }
.strip-label { font-size: 0.625rem; color: var(--muted); letter-spacing: 0.15em; margin-bottom: 0.1875rem; }
.strip-val   { color: var(--text); font-size: 0.875rem; }
.strip-val.ok     { color: var(--green); }
.strip-val.warn   { color: var(--amber); }
.strip-val.danger { color: var(--red); }
.strip-val.gold   { color: var(--gold); }

/* ── Main layout ─────────────────────────────────────────────────── */
.layout {
  display: grid;
  grid-template-columns: 58fr 42fr;
  gap: 1.25rem;
  padding: 1.25rem 2rem 2rem;
  max-width: 1700px;
  margin: 0 auto;
}
.col { display: flex; flex-direction: column; gap: 1rem; }

/* ── Card base ───────────────────────────────────────────────────── */
.card {
  background: var(--surf);
  border: 1px solid var(--border);
  border-radius: var(--r8);
  padding: 1.25rem 1.375rem;
  opacity: 0;
  transform: translateY(8px);
  transition: opacity 0.4s ease, transform 0.4s ease;
}
.card.visible { opacity: 1; transform: translateY(0); }
.card-label {
  font-family: var(--font-display);
  font-size: 0.625rem;
  font-weight: 700;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--muted);
  margin-bottom: 1rem;
}
.card-label span {
  display: inline-block;
  width: 0.375rem; height: 0.375rem;
  border-radius: 50%;
  background: var(--gold);
  margin-right: 0.5rem;
  vertical-align: middle;
  position: relative; top: -1px;
}

/* ── Mission panel (open position) ──────────────────────────────── */
#mission-card {
  border-color: var(--border);
  transition: border-color 0.3s ease, box-shadow 0.3s ease, opacity 0.4s ease, transform 0.4s ease;
}
#mission-card.active-long {
  border-color: var(--green);
  box-shadow: 0 0 0 1px var(--green-dim), inset 0 0 40px oklch(70% 0.18 145 / 0.04);
}
#mission-card.active-short {
  border-color: var(--red);
  box-shadow: 0 0 0 1px var(--red-dim), inset 0 0 40px oklch(60% 0.22 20 / 0.04);
}
.mission-flat {
  display: flex;
  align-items: center;
  gap: 1rem;
  color: var(--faint);
  font-size: 0.8125rem;
}
.mission-flat-icon {
  width: 2.5rem; height: 2.5rem;
  border: 1px solid var(--border);
  border-radius: var(--r4);
  display: flex; align-items: center; justify-content: center;
  font-size: 1.125rem;
  color: var(--faint);
}
.mission-active { display: none; }
.dir-badge {
  font-family: var(--font-display);
  font-weight: 800;
  font-size: 3.5rem;
  letter-spacing: 0.04em;
  line-height: 1;
  margin-bottom: 0.25rem;
}
.dir-badge.long  { color: var(--green); }
.dir-badge.short { color: var(--red); }
.mission-asset {
  font-family: var(--font-display);
  font-weight: 600;
  font-size: 1rem;
  color: var(--muted);
  letter-spacing: 0.12em;
  text-transform: uppercase;
  margin-bottom: 1.25rem;
}
.pnl-big {
  font-family: var(--font-display);
  font-weight: 800;
  font-size: 2.25rem;
  letter-spacing: 0.02em;
  line-height: 1;
  margin-bottom: 0.25rem;
}
.pnl-big.pos { color: var(--green); }
.pnl-big.neg { color: var(--red); }
.pnl-label {
  font-size: 0.6875rem;
  color: var(--muted);
  letter-spacing: 0.1em;
  text-transform: uppercase;
  margin-bottom: 1.25rem;
}

/* Price range bar */
.range-bar-wrap {
  margin-bottom: 1.25rem;
}
.range-labels {
  display: flex;
  justify-content: space-between;
  font-size: 0.6875rem;
  color: var(--muted);
  letter-spacing: 0.06em;
  margin-bottom: 0.375rem;
}
.range-labels .rl-stop   { color: var(--red); }
.range-labels .rl-target { color: var(--green); }
.range-track {
  height: 0.375rem;
  background: var(--surf-2);
  border-radius: 1rem;
  position: relative;
  overflow: visible;
}
.range-fill {
  height: 100%;
  border-radius: 1rem;
  background: linear-gradient(90deg, var(--red-dim), var(--green-dim));
  transition: width 0.3s ease;
}
.range-cursor {
  position: absolute;
  top: 50%; transform: translate(-50%, -50%);
  width: 0.75rem; height: 0.75rem;
  border-radius: 50%;
  background: var(--gold);
  box-shadow: 0 0 0 2px var(--bg), 0 0 8px var(--gold-glow);
  transition: left 0.3s ease;
}
.range-prices {
  display: flex;
  justify-content: space-between;
  font-size: 0.6875rem;
  color: var(--faint);
  margin-top: 0.25rem;
}

/* Mission meta grid */
.mission-meta {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: 0.75rem 1rem;
  padding-top: 1rem;
  border-top: 1px solid var(--border);
}
.meta-item-label { font-size: 0.625rem; color: var(--muted); letter-spacing: 0.08em; text-transform: uppercase; }
.meta-item-val   { font-size: 0.875rem; color: var(--text); margin-top: 0.125rem; }

/* ── Engine cards (BTC / ETH) ────────────────────────────────────── */
.engine-row {
  display: flex;
  align-items: center;
  gap: 1.25rem;
  margin-bottom: 0.875rem;
}
.engine-state {
  font-family: var(--font-display);
  font-weight: 700;
  font-size: 0.75rem;
  letter-spacing: 0.14em;
  padding: 0.25rem 0.75rem;
  border-radius: var(--r4);
  text-transform: uppercase;
}
.state-flat    { background: var(--surf-2);   color: var(--muted); }
.state-pending { background: var(--amber-dim); color: var(--amber); }
.state-open    { background: var(--green-dim); color: var(--green); }
.state-exit    { background: var(--blue-dim);  color: var(--blue);  }
.state-killed  { background: var(--red-dim);   color: var(--red);   }

.engine-name {
  font-family: var(--font-display);
  font-weight: 800;
  font-size: 1.25rem;
  letter-spacing: 0.06em;
  color: var(--text);
}
.book-row {
  display: flex;
  gap: 1.5rem;
  font-size: 0.75rem;
  color: var(--muted);
}
.book-row span { color: var(--text); }
.spread-ok   { color: var(--green); }
.spread-wide { color: var(--amber); }

/* ── Circuit breakers ────────────────────────────────────────────── */
.cb-item {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  padding: 0.75rem 0;
  border-bottom: 1px solid var(--border);
  gap: 1rem;
}
.cb-item:last-child { border-bottom: none; padding-bottom: 0; }
.cb-item:first-child { padding-top: 0; }
.cb-name { font-family: var(--font-display); font-weight: 700; font-size: 0.9375rem; color: var(--text); letter-spacing: 0.04em; }
.cb-desc { font-size: 0.6875rem; color: var(--muted); margin-top: 0.125rem; letter-spacing: 0.03em; }
.cb-status {
  flex-shrink: 0;
  font-family: var(--font-display);
  font-weight: 700;
  font-size: 0.6875rem;
  letter-spacing: 0.15em;
  padding: 0.25rem 0.75rem;
  border-radius: var(--r4);
  text-transform: uppercase;
  white-space: nowrap;
}
.cb-ok   { background: var(--green-dim); color: var(--green); }
.cb-fail { background: var(--red-dim);   color: var(--red); animation: cb-flash 1s ease-in-out infinite; }
@keyframes cb-flash { 0%,100%{opacity:1;} 50%{opacity:0.55;} }

/* ── Account ─────────────────────────────────────────────────────── */
.acct-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1rem;
}
.acct-item-label { font-size: 0.625rem; color: var(--muted); letter-spacing: 0.1em; text-transform: uppercase; margin-bottom: 0.25rem; }
.acct-big {
  font-family: var(--font-display);
  font-weight: 700;
  font-size: 1.625rem;
  letter-spacing: 0.02em;
  line-height: 1;
}
.acct-big.pos { color: var(--green); }
.acct-big.neg { color: var(--red); }
.acct-big.neu { color: var(--text); }
.acct-sub { font-size: 0.6875rem; color: var(--muted); margin-top: 0.25rem; }

/* ── Backtest ────────────────────────────────────────────────────── */
.bt-gate {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  margin-bottom: 1rem;
}
.bt-gate-badge {
  font-family: var(--font-display);
  font-weight: 800;
  font-size: 0.75rem;
  letter-spacing: 0.16em;
  padding: 0.3125rem 0.875rem;
  border-radius: var(--r4);
  text-transform: uppercase;
}
.bt-gate-pass { background: var(--green-dim); color: var(--green); }
.bt-gate-fail { background: var(--red-dim);   color: var(--red); }
.bt-gate-pending { background: var(--surf-2); color: var(--muted); }
.bt-instrument { font-family: var(--font-display); font-weight: 700; font-size: 1rem; color: var(--text); }
.bt-stats {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 0.5rem;
  margin-bottom: 0.75rem;
}
.bt-stat-label { font-size: 0.5625rem; color: var(--muted); letter-spacing: 0.08em; text-transform: uppercase; }
.bt-stat-val   { font-size: 0.9375rem; font-weight: 700; color: var(--text); margin-top: 0.125rem; }
.bt-plain { font-size: 0.75rem; color: var(--muted); line-height: 1.6; }
.bt-fail-list { margin-top: 0.5rem; padding: 0.5rem 0.75rem; background: var(--red-dim); border-left: 2px solid var(--red); border-radius: var(--r4); font-size: 0.6875rem; color: var(--red); line-height: 1.8; }
.bt-divider { border: none; border-top: 1px solid var(--border); margin: 1rem 0; }

/* ── Trade log ───────────────────────────────────────────────────── */
.trade-scroll { overflow-x: auto; margin: 0 -0.25rem; }
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.75rem;
  min-width: 44rem;
}
th {
  text-align: left;
  padding: 0.375rem 0.625rem;
  color: var(--muted);
  font-size: 0.5625rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  border-bottom: 1px solid var(--border);
  font-family: var(--font-display);
  font-weight: 700;
  white-space: nowrap;
}
td {
  padding: 0.5625rem 0.625rem;
  border-bottom: 1px solid var(--border);
  color: var(--text);
  vertical-align: middle;
}
tr:last-child td { border-bottom: none; }
tr:hover td { background: oklch(100% 0 0 / 0.018); }
.td-pos { color: var(--green); font-weight: 700; }
.td-neg { color: var(--red);   font-weight: 700; }
.td-muted { color: var(--muted); }
.dir-long  { color: var(--green); font-family: var(--font-display); font-weight: 800; font-size: 0.9375rem; letter-spacing: 0.08em; }
.dir-short { color: var(--red);   font-family: var(--font-display); font-weight: 800; font-size: 0.9375rem; letter-spacing: 0.08em; }
.reason-target { color: var(--green); }
.reason-stop   { color: var(--red); }
.reason-other  { color: var(--amber); }
.empty-row td  { color: var(--faint); text-align: center; padding: 2rem; font-size: 0.8125rem; }

/* ── Kill switch ─────────────────────────────────────────────────── */
.kill-layout {
  display: flex;
  align-items: center;
  gap: 1.5rem;
}
.kill-text { flex: 1; }
.kill-title {
  font-family: var(--font-display);
  font-weight: 800;
  font-size: 1.125rem;
  color: var(--text);
  letter-spacing: 0.06em;
  margin-bottom: 0.375rem;
}
.kill-desc { font-size: 0.75rem; color: var(--muted); line-height: 1.7; }
.kill-desc strong { color: var(--text); }
.btn-halt {
  flex-shrink: 0;
  padding: 0.875rem 2rem;
  background: transparent;
  border: 1.5px solid var(--red);
  color: var(--red);
  border-radius: var(--r4);
  font-family: var(--font-display);
  font-weight: 800;
  font-size: 0.875rem;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease, transform 0.1s ease, box-shadow 0.15s ease;
  min-width: 10rem;
  min-height: 2.75rem;
}
.btn-halt:hover:not(:disabled) {
  background: var(--red);
  color: #fff;
  transform: translateY(-1px);
  box-shadow: 0 4px 16px oklch(60% 0.22 20 / 0.3);
}
.btn-halt:active:not(:disabled) { transform: translateY(0); }
.btn-halt:disabled {
  border-color: var(--faint);
  color: var(--faint);
  cursor: not-allowed;
}
.btn-halt:focus-visible {
  outline: 2px solid var(--red);
  outline-offset: 3px;
}

/* ── Kill modal ──────────────────────────────────────────────────── */
#kill-modal-bg {
  display: none;
  position: fixed; inset: 0;
  background: oklch(0% 0 0 / 0.7);
  z-index: 500;
  align-items: center;
  justify-content: center;
  backdrop-filter: blur(4px);
}
#kill-modal-bg.open { display: flex; }
#kill-modal {
  background: var(--surf);
  border: 1px solid var(--red);
  border-radius: var(--r8);
  padding: 2rem;
  max-width: 26rem;
  width: calc(100% - 2rem);
  box-shadow: 0 24px 64px oklch(0% 0 0 / 0.5);
}
.modal-title {
  font-family: var(--font-display);
  font-weight: 800;
  font-size: 1.375rem;
  letter-spacing: 0.08em;
  color: var(--red);
  margin-bottom: 0.75rem;
}
.modal-body {
  font-size: 0.8125rem;
  color: var(--muted);
  line-height: 1.7;
  margin-bottom: 1.5rem;
}
.modal-body strong { color: var(--text); }
.modal-buttons { display: flex; gap: 0.75rem; justify-content: flex-end; }
.btn-confirm {
  padding: 0.625rem 1.5rem;
  background: var(--red);
  color: #fff;
  border: none;
  border-radius: var(--r4);
  font-family: var(--font-display);
  font-weight: 800;
  font-size: 0.875rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  cursor: pointer;
  transition: opacity 0.15s;
  min-height: 2.75rem;
}
.btn-confirm:hover { opacity: 0.85; }
.btn-cancel {
  padding: 0.625rem 1.5rem;
  background: transparent;
  color: var(--muted);
  border: 1px solid var(--border);
  border-radius: var(--r4);
  font-family: var(--font-display);
  font-weight: 700;
  font-size: 0.875rem;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
  min-height: 2.75rem;
}
.btn-cancel:hover { color: var(--text); border-color: var(--border-hi); }

/* ── Responsive ──────────────────────────────────────────────────── */
@media (max-width: 64rem) {
  .layout { grid-template-columns: 1fr; }
  .bt-stats { grid-template-columns: repeat(3, 1fr); }
}
@media (max-width: 40rem) {
  header { padding: 0.875rem 1rem; }
  .layout { padding: 1rem; }
  .acct-grid { grid-template-columns: 1fr; }
  #status-strip { flex-direction: column; }
  .strip-cell { border-right: none; border-bottom: 1px solid var(--border); width: 100%; }
  .kill-layout { flex-direction: column; align-items: flex-start; }
  .mission-meta { grid-template-columns: 1fr 1fr; }
}
</style>
</head>
<body>

<div id='banner-dry'>⚠ DRY RUN — Watching the market. No real orders are placed.</div>
<div id='banner-kill'>🛑 BOT HALTED — Open positions are protected by resting exchange orders</div>

<header>
  <div class='wordmark'>BOT<em>TERMINAL</em></div>
  <div class='header-meta'>
    <div class='live-indicator'>
      <div class='pulse-dot' id='live-dot'></div>
      <span id='hdr-uptime'>—</span>
    </div>
    <div id='hdr-refresh'>—</div>
  </div>
</header>

<!-- Plain-language status strip -->
<div id='status-strip'>
  <div class='strip-cell'>
    <div class='strip-label'>Bot status</div>
    <div class='strip-val' id='ss-status'>—</div>
  </div>
  <div class='strip-cell'>
    <div class='strip-label'>BTC</div>
    <div class='strip-val' id='ss-btc'>—</div>
  </div>
  <div class='strip-cell'>
    <div class='strip-label'>ETH</div>
    <div class='strip-val' id='ss-eth'>—</div>
  </div>
  <div class='strip-cell'>
    <div class='strip-label'>Profit today</div>
    <div class='strip-val' id='ss-pnl'>—</div>
  </div>
  <div class='strip-cell'>
    <div class='strip-label'>Risk systems</div>
    <div class='strip-val' id='ss-risk'>—</div>
  </div>
</div>

<div class='layout'>
  <!-- ── LEFT COLUMN ── -->
  <div class='col'>

    <!-- Open position / mission panel -->
    <div class='card' id='mission-card'>
      <div class='card-label'><span></span>Open Position</div>
      <div class='mission-flat' id='mission-flat'>
        <div class='mission-flat-icon'>○</div>
        <div>
          <div style='color:var(--muted);font-size:0.875rem;'>No open position</div>
          <div style='color:var(--faint);font-size:0.75rem;margin-top:0.125rem;'>Scanning market for signals</div>
        </div>
      </div>
      <div class='mission-active' id='mission-active'>
        <div style='display:flex;align-items:flex-end;gap:1.5rem;margin-bottom:0.25rem;'>
          <div class='dir-badge' id='m-dir'>LONG</div>
          <div class='mission-asset' id='m-asset'>BTC/USD</div>
        </div>
        <div class='pnl-big' id='m-pnl'>+$0.00</div>
        <div class='pnl-label' id='m-pnl-label'>Current gain / loss on this trade</div>
        <div class='range-bar-wrap' id='m-range-wrap'>
          <div class='range-labels'>
            <span class='rl-stop' id='m-stop-lbl'>↓ Stop loss</span>
            <span style='color:var(--muted);'>Current price</span>
            <span class='rl-target' id='m-target-lbl'>↑ Take profit</span>
          </div>
          <div class='range-track'>
            <div class='range-fill' id='m-range-fill' style='width:50%;'></div>
            <div class='range-cursor' id='m-cursor' style='left:50%;'></div>
          </div>
          <div class='range-prices'>
            <span id='m-stop-price'>—</span>
            <span id='m-current-price'>—</span>
            <span id='m-target-price'>—</span>
          </div>
        </div>
        <div class='mission-meta'>
          <div>
            <div class='meta-item-label'>You entered at</div>
            <div class='meta-item-val' id='m-entry'>—</div>
          </div>
          <div>
            <div class='meta-item-label'>Quantity</div>
            <div class='meta-item-val' id='m-qty'>—</div>
          </div>
          <div>
            <div class='meta-item-label'>Open since</div>
            <div class='meta-item-val' id='m-since'>—</div>
          </div>
        </div>
      </div>
    </div>

    <!-- BTC engine -->
    <div class='card' id='card-btc'>
      <div class='card-label'><span></span>BTC — Momentum Strategy</div>
      <div class='engine-row'>
        <div class='engine-name'>BTCUSD</div>
        <div class='engine-state state-flat' id='btc-state-badge'>FLAT</div>
      </div>
      <div class='book-row'>
        <span>Bid: <span id='btc-bid'>—</span></span>
        <span>Ask: <span id='btc-ask'>—</span></span>
        <span>Spread: <span id='btc-spread' class='spread-ok'>—</span></span>
      </div>
    </div>

    <!-- ETH engine -->
    <div class='card' id='card-eth'>
      <div class='card-label'><span></span>ETH — Mean Reversion Strategy</div>
      <div class='engine-row'>
        <div class='engine-name'>ETHUSD</div>
        <div class='engine-state state-flat' id='eth-state-badge'>FLAT</div>
      </div>
      <div class='book-row'>
        <span>Bid: <span id='eth-bid'>—</span></span>
        <span>Ask: <span id='eth-ask'>—</span></span>
        <span>Spread: <span id='eth-spread' class='spread-ok'>—</span></span>
      </div>
    </div>

    <!-- Circuit breakers -->
    <div class='card' id='card-cb'>
      <div class='card-label'><span></span>Risk Guards</div>
      <div class='cb-item'>
        <div>
          <div class='cb-name'>Account Floor</div>
          <div class='cb-desc' id='cb-min-desc'>—</div>
        </div>
        <div class='cb-status cb-ok' id='cb-min-pill'>CLEAR</div>
      </div>
      <div class='cb-item'>
        <div>
          <div class='cb-name'>Daily Loss Limit</div>
          <div class='cb-desc' id='cb-dd-desc'>—</div>
        </div>
        <div class='cb-status cb-ok' id='cb-dd-pill'>CLEAR</div>
      </div>
      <div class='cb-item'>
        <div>
          <div class='cb-name'>Trade Count Limit</div>
          <div class='cb-desc' id='cb-trades-desc'>—</div>
        </div>
        <div class='cb-status cb-ok' id='cb-trades-pill'>CLEAR</div>
      </div>
    </div>

    <!-- Account -->
    <div class='card' id='card-acct'>
      <div class='card-label'><span></span>Account</div>
      <div class='acct-grid'>
        <div>
          <div class='acct-item-label'>Balance</div>
          <div class='acct-big neu' id='acct-balance'>—</div>
          <div class='acct-sub' id='acct-peak'>—</div>
        </div>
        <div>
          <div class='acct-item-label'>Today's realized profit/loss</div>
          <div class='acct-big' id='acct-pnl'>—</div>
          <div class='acct-sub' id='acct-trades'>—</div>
        </div>
      </div>
    </div>

  </div>

  <!-- ── RIGHT COLUMN ── -->
  <div class='col'>

    <!-- Backtest gates -->
    <div class='card' id='card-bt'>
      <div class='card-label'><span></span>Strategy Validation (before going live)</div>
      <!-- BTC -->
      <div class='bt-gate'>
        <div class='bt-gate-badge bt-gate-pending' id='btc-gate-badge'>Pending</div>
        <div class='bt-instrument'>BTC Momentum</div>
      </div>
      <div class='bt-stats'>
        <div><div class='bt-stat-label'>Backtest trades</div><div class='bt-stat-val' id='bt-btc-n'>—</div></div>
        <div><div class='bt-stat-label'>Win rate</div><div class='bt-stat-val' id='bt-btc-wr'>—</div></div>
        <div><div class='bt-stat-label'>Avg profit/risk</div><div class='bt-stat-val' id='bt-btc-rr'>—</div></div>
        <div><div class='bt-stat-label'>Max loss streak</div><div class='bt-stat-val' id='bt-btc-dd'>—</div></div>
        <div><div class='bt-stat-label'>Sharpe</div><div class='bt-stat-val' id='bt-btc-sh'>—</div></div>
      </div>
      <div class='bt-plain' id='bt-btc-plain'></div>
      <div class='bt-fail-list' id='bt-btc-fails' style='display:none;'></div>
      <hr class='bt-divider'>
      <!-- ETH -->
      <div class='bt-gate'>
        <div class='bt-gate-badge bt-gate-pending' id='eth-gate-badge'>Pending</div>
        <div class='bt-instrument'>ETH Mean Reversion</div>
      </div>
      <div class='bt-stats'>
        <div><div class='bt-stat-label'>Backtest trades</div><div class='bt-stat-val' id='bt-eth-n'>—</div></div>
        <div><div class='bt-stat-label'>Win rate</div><div class='bt-stat-val' id='bt-eth-wr'>—</div></div>
        <div><div class='bt-stat-label'>Avg profit/risk</div><div class='bt-stat-val' id='bt-eth-rr'>—</div></div>
        <div><div class='bt-stat-label'>Max loss streak</div><div class='bt-stat-val' id='bt-eth-dd'>—</div></div>
        <div><div class='bt-stat-label'>Sharpe</div><div class='bt-stat-val' id='bt-eth-sh'>—</div></div>
      </div>
      <div class='bt-plain' id='bt-eth-plain'></div>
      <div class='bt-fail-list' id='bt-eth-fails' style='display:none;'></div>
    </div>

    <!-- Trade log -->
    <div class='card' id='card-trades'>
      <div class='card-label'><span></span>Closed Trades</div>
      <div class='trade-scroll'>
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Asset</th>
              <th>Direction</th>
              <th>Entered</th>
              <th>Exited</th>
              <th>Profit / Loss</th>
              <th>Closed because</th>
              <th>Duration</th>
            </tr>
          </thead>
          <tbody id='trade-tbody'>
            <tr class='empty-row'><td colspan='8'>No closed trades yet</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Kill switch -->
    <div class='card' id='card-kill'>
      <div class='card-label'><span style='background:var(--red);'></span>Emergency Halt</div>
      <div class='kill-layout'>
        <div class='kill-text'>
          <div class='kill-title'>Stop the bot immediately</div>
          <div class='kill-desc'>
            Cancels all pending entry orders and stops the bot from taking new trades.
            <strong>Any open position stays protected by resting stop-loss and take-profit orders on the exchange</strong>
            — those don't go away when you hit halt. The dashboard stays live so you can see what happened.
          </div>
        </div>
        <button class='btn-halt' id='kill-btn' onclick='openKillModal()'>HALT BOT</button>
      </div>
    </div>

  </div>
</div>

<!-- Kill confirmation modal -->
<div id='kill-modal-bg'>
  <div id='kill-modal' role='dialog' aria-modal='true' aria-labelledby='modal-title'>
    <div class='modal-title' id='modal-title'>Halt the bot?</div>
    <div class='modal-body'>
      The bot will stop taking new trades immediately.<br><br>
      <strong>Your open positions are not abandoned.</strong> Stop-loss and take-profit
      orders placed on the exchange remain active. They will execute automatically
      without the bot running.<br><br>
      You'll need to redeploy to restart.
    </div>
    <div class='modal-buttons'>
      <button class='btn-cancel' onclick='closeKillModal()'>Cancel</button>
      <button class='btn-confirm' onclick='executeKill()'>Yes, halt the bot</button>
    </div>
  </div>
</div>

<script>
'use strict';

/* ── Helpers ─────────────────────────────────────────────────────── */
const $ = id => document.getElementById(id);
const N = (n, d=2) => (n == null || isNaN(n)) ? '—' : Number(n).toFixed(d);
const Pct = n => N(n * 100, 1) + '%';
const PnlStr = n => (n >= 0 ? '+$' : '-$') + Math.abs(n).toFixed(4);
const elapsed = ts => {
  const m = Math.round((Date.now()/1000 - ts) / 60);
  if (m < 60) return m + 'm ago';
  const h = Math.floor(m / 60); const rm = m % 60;
  return h + 'h ' + rm + 'm ago';
};
const fmtUptime = s => {
  const h = String(Math.floor(s/3600)).padStart(2,'0');
  const m = String(Math.floor((s%3600)/60)).padStart(2,'0');
  const sec = String(s%60).padStart(2,'0');
  return h + ':' + m + ':' + sec;
};
const humanReason = r => ({
  'TARGET':             'Hit profit target ✓',
  'STOP':               'Stop loss triggered',
  'TIMEOUT':            'Time limit — closed',
  'MAX_HOLD_EXCEEDED':  'Time limit — closed',
  'emergency_cancel_after_oco_failure': 'Emergency close',
}[r] || r);

function setStateBadge(el, state) {
  const map = {
    'FLAT':          ['FLAT',         'state-flat'],
    'ENTRY_PENDING': ['Placing order','state-pending'],
    'POSITION_OPEN': ['In trade',     'state-open'],
    'EXIT_PENDING':  ['Closing',      'state-exit'],
  };
  const [label, cls] = map[state] || ['—', 'state-flat'];
  el.textContent = label;
  el.className = 'engine-state ' + cls;
}

/* ── State update ─────────────────────────────────────────────────── */
let currentBid = { btc: 0, eth: 0 };

function applyState(d) {
  /* Status strip */
  if (d.kill_triggered) {
    $('banner-kill').style.display = 'block';
    $('kill-btn').disabled = true;
    $('kill-btn').textContent = 'HALTED';
    $('live-dot').classList.add('offline');
    $('ss-status').textContent = 'HALTED';
    $('ss-status').className = 'strip-val danger';
  } else if (d.dry_run) {
    $('banner-dry').style.display = 'block';
    $('ss-status').textContent = 'DRY RUN';
    $('ss-status').className = 'strip-val warn';
  } else {
    $('ss-status').textContent = 'RUNNING';
    $('ss-status').className = 'strip-val ok';
  }

  $('hdr-uptime').textContent = fmtUptime(d.uptime_seconds || 0);
  $('hdr-refresh').textContent = 'Last updated ' + new Date().toLocaleTimeString('en-US',{hour12:false});

  /* Risk */
  const rk = d.risk || {};
  const c  = rk.circuits || {};
  const bal = rk.balance || 0;
  const pnl = rk.daily_pnl || 0;

  $('acct-balance').textContent = '$' + N(bal, 4);
  $('acct-peak').textContent = 'Session high: $' + N(rk.session_peak, 4);
  $('acct-pnl').textContent = PnlStr(pnl);
  $('acct-pnl').className = 'acct-big ' + (pnl >= 0 ? 'pos' : 'neg');
  $('acct-trades').textContent = (rk.daily_trades||0) + ' of ' + (rk.max_daily_trades||3) + ' trades used today';

  $('ss-pnl').textContent = PnlStr(pnl);
  $('ss-pnl').className = 'strip-val ' + (pnl >= 0 ? 'ok' : 'danger');

  /* Circuit breakers */
  const setCB = (pillId, descId, ok, descText) => {
    const pill = $(pillId);
    pill.textContent = ok ? 'CLEAR' : 'TRIPPED';
    pill.className = 'cb-status ' + (ok ? 'cb-ok' : 'cb-fail');
    $(descId).textContent = descText;
  };
  setCB('cb-min-pill', 'cb-min-desc', c.min_balance_ok !== false,
        'Balance $' + N(bal,2) + ' — must stay above $' + N(rk.min_balance,2));
  setCB('cb-dd-pill', 'cb-dd-desc', c.daily_drawdown_ok !== false,
        'Today\'s loss: ' + Pct(c.daily_drawdown_pct||0) + ' of balance — limit is ' + Pct(rk.drawdown_limit||0.1));
  setCB('cb-trades-pill', 'cb-trades-desc', c.max_trades_ok !== false,
        (rk.daily_trades||0) + ' trades taken today — limit is ' + (rk.max_daily_trades||3));

  const allClear = c.min_balance_ok !== false && c.daily_drawdown_ok !== false && c.max_trades_ok !== false;
  $('ss-risk').textContent = allClear ? 'All clear' : 'Circuit tripped!';
  $('ss-risk').className = 'strip-val ' + (allClear ? 'ok' : 'danger');

  /* Engines */
  const engines = d.engines || {};
  const btcKey  = Object.keys(engines).find(k => k.includes('BTC'));
  const ethKey  = Object.keys(engines).find(k => k.includes('ETH'));
  const btcEng  = btcKey ? engines[btcKey] : null;
  const ethEng  = ethKey ? engines[ethKey] : null;

  if (btcEng) {
    setStateBadge($('btc-state-badge'), btcEng.state);
    $('btc-bid').textContent    = '$' + N(btcEng.last_bid, 0);
    $('btc-ask').textContent    = '$' + N(btcEng.last_ask, 0);
    const bsp = btcEng.spread_pct || 0;
    $('btc-spread').textContent = N(bsp * 100, 4) + '%';
    $('btc-spread').className   = 'spread-' + (bsp < 0.0005 ? 'ok' : 'wide');
    currentBid.btc = btcEng.last_bid;
    $('ss-btc').textContent = statePlain(btcEng.state);
    $('ss-btc').className   = 'strip-val ' + stateColor(btcEng.state);
  }
  if (ethEng) {
    setStateBadge($('eth-state-badge'), ethEng.state);
    $('eth-bid').textContent    = '$' + N(ethEng.last_bid, 2);
    $('eth-ask').textContent    = '$' + N(ethEng.last_ask, 2);
    const esp = ethEng.spread_pct || 0;
    $('eth-spread').textContent = N(esp * 100, 4) + '%';
    $('eth-spread').className   = 'spread-' + (esp < 0.0005 ? 'ok' : 'wide');
    currentBid.eth = ethEng.last_bid;
    $('ss-eth').textContent = statePlain(ethEng.state);
    $('ss-eth').className   = 'strip-val ' + stateColor(ethEng.state);
  }

  /* Mission panel — show whichever instrument has an open position */
  let openPos = null, openAsset = '';
  if (btcEng && btcEng.position) { openPos = btcEng.position; openAsset = 'BTC / USD'; }
  else if (ethEng && ethEng.position) { openPos = ethEng.position; openAsset = 'ETH / USD'; }

  updateMission(openPos, openAsset,
    openAsset.includes('BTC') ? currentBid.btc : currentBid.eth);

  /* Backtest */
  const bt = d.backtest || {};
  if (btcKey && bt[btcKey]) applyBacktest('btc', bt[btcKey]);
  if (ethKey && bt[ethKey]) applyBacktest('eth', bt[ethKey]);

  /* Animate in on first render */
  document.querySelectorAll('.card, #status-strip').forEach(el => el.classList.add('visible'));
}

function statePlain(s) {
  return {FLAT:'Watching',ENTRY_PENDING:'Placing order',
          POSITION_OPEN:'In trade',EXIT_PENDING:'Closing trade'}[s] || s;
}
function stateColor(s) {
  return {FLAT:'',ENTRY_PENDING:'warn',POSITION_OPEN:'ok',EXIT_PENDING:'gold'}[s] || '';
}

function updateMission(pos, asset, currentPrice) {
  const mc = $('mission-card');
  const mf = $('mission-flat');
  const ma = $('mission-active');

  if (!pos) {
    mf.style.display = 'flex';
    ma.style.display = 'none';
    mc.className = 'card visible';
    return;
  }
  mf.style.display = 'none';
  ma.style.display = 'block';

  const isLong = pos.direction === 'LONG';
  mc.className = 'card visible ' + (isLong ? 'active-long' : 'active-short');

  $('m-dir').textContent = pos.direction;
  $('m-dir').className   = 'dir-badge ' + (isLong ? 'long' : 'short');
  $('m-asset').textContent = asset;

  const upnl = pos.unrealized_pnl || 0;
  $('m-pnl').textContent = PnlStr(upnl);
  $('m-pnl').className   = 'pnl-big ' + (upnl >= 0 ? 'pos' : 'neg');
  $('m-pnl-label').textContent = (upnl >= 0 ? 'Unrealized gain' : 'Unrealized loss') + ' on this trade';

  /* Price range bar */
  const stop   = pos.stop_price   || 0;
  const target = pos.target_price || 0;
  const curr   = currentPrice     || pos.fill_price || 0;
  const fill   = pos.fill_price   || 0;

  $('m-stop-price').textContent    = '$' + N(stop, 2);
  $('m-current-price').textContent = 'Now: $' + N(curr, 2);
  $('m-target-price').textContent  = '$' + N(target, 2);
  $('m-stop-lbl').textContent   = isLong ? '↓ Stop loss at' : '↑ Stop loss at';
  $('m-target-lbl').textContent = isLong ? '↑ Take profit at' : '↓ Take profit at';

  const lo = Math.min(stop, target), hi = Math.max(stop, target);
  const range = hi - lo;
  const cursorPct = range > 0 ? Math.max(0, Math.min(100, ((curr - lo) / range) * 100)) : 50;
  $('m-cursor').style.left     = cursorPct + '%';
  $('m-range-fill').style.width = cursorPct + '%';

  $('m-entry').textContent = '$' + N(fill, 4);
  $('m-qty').textContent   = N(pos.quantity, 6);
  $('m-since').textContent = pos.entry_ts ? elapsed(pos.entry_ts) : '—';
}

function applyBacktest(prefix, bt) {
  const badge = $(prefix + '-gate-badge');
  if (!bt.n_trades && !bt.gate_pass) {
    badge.textContent  = 'Pending';
    badge.className    = 'bt-gate-badge bt-gate-pending';
  } else {
    badge.textContent  = bt.gate_pass ? 'Passed ✓' : 'Failed ✗';
    badge.className    = 'bt-gate-badge ' + (bt.gate_pass ? 'bt-gate-pass' : 'bt-gate-fail');
  }
  $('bt-' + prefix + '-n').textContent  = bt.n_trades || '—';
  $('bt-' + prefix + '-wr').textContent = bt.win_rate ? Pct(bt.win_rate) : '—';
  $('bt-' + prefix + '-rr').textContent = bt.avg_rr ? N(bt.avg_rr, 2) + 'x' : '—';
  $('bt-' + prefix + '-dd').textContent = bt.max_drawdown ? Pct(bt.max_drawdown) : '—';
  $('bt-' + prefix + '-sh').textContent = bt.sharpe ? N(bt.sharpe, 2) : '—';

  const plain = $('bt-' + prefix + '-plain');
  if (bt.n_trades > 0) {
    const wr = Math.round((bt.win_rate || 0) * 100);
    plain.textContent = 'Over the last 90 days this strategy would have been profitable on ' +
      wr + '% of trades, with an average profit ' + N(bt.avg_rr, 2) +
      'x the size of its average loss.';
  }
  const failEl = $('bt-' + prefix + '-fails');
  if (bt.gate_failures && bt.gate_failures.length) {
    failEl.innerHTML = bt.gate_failures.map(f => '✕ ' + f).join('<br>');
    failEl.style.display = 'block';
  } else {
    failEl.style.display = 'none';
  }
}

/* ── Trade log ────────────────────────────────────────────────────── */
function applyTrades(data) {
  const trades = data.trades || [];
  const tbody  = $('trade-tbody');
  if (!trades.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="8">No closed trades yet</td></tr>';
    return;
  }
  tbody.innerHTML = trades.map(t => {
    const pnlCls = t.realized_pnl >= 0 ? 'td-pos' : 'td-neg';
    const rCls   = t.exit_reason === 'TARGET' ? 'reason-target'
                 : t.exit_reason === 'STOP'   ? 'reason-stop'
                 : 'reason-other';
    const instr  = (t.instrument || '').replace('USD-PERP','').replace('USDT','');
    return '<tr>' +
      '<td class="td-muted">' + t.id + '</td>' +
      '<td>' + instr + '</td>' +
      '<td class="dir-' + t.direction.toLowerCase() + '">' + t.direction + '</td>' +
      '<td>$' + N(t.fill_price, 2) + '</td>' +
      '<td>$' + N(t.exit_price, 2) + '</td>' +
      '<td class="' + pnlCls + '">' + PnlStr(t.realized_pnl) + '</td>' +
      '<td class="' + rCls + '">' + humanReason(t.exit_reason) + '</td>' +
      '<td class="td-muted">' + N(t.duration_minutes, 0) + 'm</td>' +
    '</tr>';
  }).join('');
}

/* ── Kill modal ───────────────────────────────────────────────────── */
function openKillModal()  { $('kill-modal-bg').classList.add('open'); }
function closeKillModal() { $('kill-modal-bg').classList.remove('open'); }
async function executeKill() {
  closeKillModal();
  try {
    const r = await fetch('/api/kill', {method:'POST'});
    const d = await r.json();
    if (d.status === 'triggered' || d.status === 'already_triggered') {
      $('banner-kill').style.display = 'block';
      $('kill-btn').disabled = true;
      $('kill-btn').textContent = 'HALTED';
      $('live-dot').classList.add('offline');
    }
  } catch(e) {
    alert('Kill request failed — check Railway logs.');
  }
}
$('kill-modal-bg').addEventListener('click', e => { if (e.target === $('kill-modal-bg')) closeKillModal(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeKillModal(); });

/* ── Polling ──────────────────────────────────────────────────────── */
async function fetchState() {
  try {
    const r = await fetch('/api/state');
    if (!r.ok) return;
    applyState(await r.json());
  } catch(e) {
    $('hdr-refresh').textContent = 'Connection lost — ' + new Date().toLocaleTimeString();
  }
}
async function fetchTrades() {
  try {
    const r = await fetch('/api/trades');
    if (!r.ok) return;
    applyTrades(await r.json());
  } catch(e) { /* silent */ }
}

fetchState();
fetchTrades();
setInterval(fetchState,  2000);
setInterval(fetchTrades, 6000);
</script>
</body>
</html>"""


@app.get('/', response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(content=DASHBOARD_HTML)
