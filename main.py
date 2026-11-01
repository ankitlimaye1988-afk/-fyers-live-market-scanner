import os
import time
import threading
import logging
from datetime import datetime, timezone

import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from worker.strategy import evaluate

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("fyers-scanner")

APP_ID = os.getenv("FYERS_APP_ID", "").strip()
TOKEN = os.getenv("FYERS_ACCESS_TOKEN", "").strip()
API_KEY = os.getenv("DASHBOARD_API_KEY", "").strip()
SCAN_MINUTES = max(1, int(os.getenv("SCAN_INTERVAL_MINUTES", "15")))
CANDLE_MINUTES = max(1, int(os.getenv("CANDLE_INTERVAL_MINUTES", "5")))

# Reference NIFTY 50 list; review/update it periodically.
NIFTY50 = set('ADANIENT ADANIPORTS APOLLOHOSP ASIANPAINT AXISBANK BAJAJ-AUTO '
'BAJFINANCE BAJAJFINSV BEL BHARTIARTL CIPLA COALINDIA DRREDDY EICHERMOT '
'ETERNAL GRASIM HCLTECH HDFCBANK HDFCLIFE HEROMOTOCO HINDALCO HINDUNILVR '
'ICICIBANK INDUSINDBK INFY ITC JIOFIN JSWSTEEL KOTAKBANK LT M&M MARUTI '
'NESTLEIND NTPC ONGC POWERGRID RELIANCE SBILIFE SBIN SHRIRAMFIN SUNPHARMA '
'TATACONSUM TATAMOTORS TATASTEEL TCS TECHM TITAN TRENT ULTRACEMCO'.split())

app = FastAPI(title="FYERS Live Market Scanner")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])
state = {"connected": False, "status": "waiting_for_credentials", "last_tick": None,
         "last_scan": None, "symbols": [], "signals": [], "candles": {}, "errors": []}
lock = threading.RLock()
socket_client = None

def watch_symbols():
    allow = {s.strip().upper() for s in os.getenv("FNO_UNDERLYINGS", "").split(",") if s.strip()}
    return ["NSE:" + s + "-EQ" for s in sorted(NIFTY50 & allow)]

def on_message(message):
    items = message if isinstance(message, list) else [message]
    for item in items:
        if not isinstance(item, dict):
            continue
        symbol = item.get("symbol") or item.get("n")
        price = item.get("ltp") or item.get("last_traded_price")
        if not symbol or price is None:
            continue
        try:
            price = float(price)
            stamp = int(float(item.get("exch_feed_time") or item.get("last_traded_time") or time.time()))
        except (TypeError, ValueError):
            continue
        bucket = stamp // (CANDLE_MINUTES * 60) * (CANDLE_MINUTES * 60)
        key = str(bucket)
        with lock:
            bars = state["candles"].setdefault(symbol, {})
            candle = bars.setdefault(key, {"timestamp": bucket, "open": price,
                "high": price, "low": price, "close": price, "volume": 0})
            candle["high"] = max(candle["high"], price)
            candle["low"] = min(candle["low"], price)
            candle["close"] = price
            if item.get("vol_traded_today") is not None:
                try:
                    candle["volume"] = int(item["vol_traded_today"])
                except (TypeError, ValueError):
                    pass
            state["last_tick"] = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
            for old in sorted(bars, key=int)[:-400]:
                bars.pop(old, None)

def on_connect():
    symbols = watch_symbols()
    if not symbols:
        state["status"] = "no_fno_underlyings_configured"
        log.error("Set FNO_UNDERLYINGS to eligible NIFTY 50 F&O symbols.")
        return
    socket_client.subscribe(symbols=symbols, data_type="SymbolUpdate")
    socket_client.keep_running()
    with lock:
        state.update(connected=True, status="connected", symbols=symbols)

def on_error(error):
    with lock:
        state.update(connected=False, status="socket_error")
        state["errors"] = (state["errors"] + [str(error)])[-10:]
    log.error("FYERS socket error: %s", error)

def on_close(message):
    with lock:
        state.update(connected=False, status="disconnected")

def run_socket():
    global socket_client
    if not APP_ID or not TOKEN:
        log.warning("FYERS credentials missing; running in setup mode.")
        return
    try:
        from fyers_apiv3.FyersWebsocket import data_ws
        socket_client = data_ws.FyersDataSocket(
            access_token=APP_ID + ":" + TOKEN, log_path="", litemode=False,
            write_to_file=False, reconnect=True, on_connect=on_connect,
            on_close=on_close, on_error=on_error, on_message=on_message,
            reconnect_retry=50)
        socket_client.connect()
    except Exception as exc:
        with lock:
            state.update(connected=False, status="startup_error")
            state["errors"] = (state["errors"] + [str(exc)])[-10:]
        log.exception("Could not start FYERS socket")

def scan_once():
    boundary = int(time.time()) // (CANDLE_MINUTES * 60) * (CANDLE_MINUTES * 60)
    found = []
    with lock:
        for symbol, bars in state["candles"].items():
            rows = sorted((v for k, v in bars.items() if int(k) < boundary),
                          key=lambda row: row["timestamp"])
            if len(rows) < 140:
                continue
            signal = evaluate(pd.DataFrame(rows))
            if signal:
                found.append({"symbol": symbol,
                    "timestamp": datetime.fromtimestamp(rows[-1]["timestamp"], timezone.utc).isoformat(),
                    **signal})
        state["signals"] = found
        state["last_scan"] = datetime.now(timezone.utc).isoformat()

def scheduler():
    while True:
        wait = SCAN_MINUTES * 60 - (int(time.time()) % (SCAN_MINUTES * 60)) + 3
        time.sleep(wait)
        try:
            scan_once()
        except Exception:
            log.exception("Scheduled scan failed")

@app.on_event("startup")
async def startup():
    threading.Thread(target=run_socket, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()

def authorize(key):
    if API_KEY and key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

@app.get("/health")
def health():
    return {"ok": True, "connected": state["connected"], "status": state["status"],
            "last_tick": state["last_tick"], "last_scan": state["last_scan"],
            "symbol_count": len(state["symbols"])}

@app.get("/api/status")
def status(x_api_key: str | None = Header(default=None)):
    authorize(x_api_key)
    return {k: state[k] for k in ("connected", "status", "last_tick", "last_scan", "symbols", "errors")}

@app.get("/api/signals")
def signals(x_api_key: str | None = Header(default=None)):
    authorize(x_api_key)
    return {"signals": state["signals"], "last_scan": state["last_scan"]}
