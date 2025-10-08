from fastapi import FastAPI, Request, HTTPException
import requests, os, json, datetime
from dotenv import load_dotenv
from typing import Optional, Dict

load_dotenv()
app = FastAPI()

API_KEY     = os.getenv("CC_API_KEY")
IDENTIFIER  = os.getenv("CC_IDENTIFIER")
PASSWORD    = os.getenv("CC_PASSWORD")
BASE_URL    = os.getenv("CC_BASE_URL", "https://api-capital.com")

CST = None
XST = None

SYMBOL_EPIC_MAP = {
    "GOLD":       {"epic": "GOLD",       "size": 1.2},
    "SILVER":     {"epic": "SILVER",     "size": 60},
    "COPPER":     {"epic": "COPPER",     "size": 550},
    "OIL_CRUDE":  {"epic": "OIL_CRUDE",  "size": 44},
    "EURUSD":     {"epic": "EURUSD",     "size": 7000},
    "LRC":        {"epic": "LRC",        "size": 0.2},
    "NATURALGAS": {"epic": "NATURALGAS", "size": 400},
    "TSLA":       {"epic": "TSLA",       "size": 3.4},
    "USDJPY":     {"epic": "USDJPY",     "size": 9000},
    "EURNZD":     {"epic": "EURNZD",     "size": 5000},
    "GBPUSD":     {"epic": "GBPUSD",     "size": 7000},
    "COFFEEARABICA": {"epic": "COFFEEARABICA", "size": 200},
    "OIL_BRENT":  {"epic": "OIL_BRENT", "size": 30}
}

IDEMP_STORE = "processed_signals.json"
IDEMP_TTL_DAYS = 2

def log(msg: str):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open("webhook_log.txt", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"Error writing to log file: {e}")

def login_to_capital():
    global CST, XST
    log("🔐 Logging in to Capital.com…")
    r = requests.post(
        f"{BASE_URL}/api/v1/session",
        json={"identifier": IDENTIFIER, "password": PASSWORD},
        headers={"X-CAP-API-KEY": API_KEY},
        timeout=15
    )
    r.raise_for_status()
    CST = r.headers.get("CST")
    XST = r.headers.get("X-SECURITY-TOKEN")
    if not CST or not XST:
        raise RuntimeError("Login successful, but tokens not found in headers.")
    log("✅ Login successful.")

def capital_request(method: str, path: str, *, json_body=None, retry=True) -> requests.Response:
    if not (CST and XST):
        login_to_capital()
    url = f"{BASE_URL}{path}"
    headers = {"X-CAP-API-KEY": API_KEY, "CST": CST, "X-SECURITY-TOKEN": XST}
    if json_body is not None:
        headers["Content-Type"] = "application/json"

    r = requests.request(method.upper(), url, headers=headers, json=json_body, timeout=20)

    needs_relogin = (r.status_code == 401)
    if not needs_relogin:
        try:
            j = r.json()
            if isinstance(j, dict) and j.get("errorCode") in {
                "error.invalid.session.token", "error.security.account.token.invalid"
            }:
                needs_relogin = True
        except json.JSONDecodeError:
            pass

    if needs_relogin and retry:
        log("⚠️ Session invalid/expired → re-login and retry once.")
        login_to_capital()
        return capital_request(method, path, json_body=json_body, retry=False)
    return r

def get_open_positions() -> list:
    r = capital_request("GET", "/api/v1/positions")
    if r.status_code != 200:
        log(f"❌ Fetch positions failed: {r.text}")
        return []
    return r.json().get("positions", [])

def parse_pos(p: Dict) -> Dict:
    pos_data = p.get("position", {})
    market_data = p.get("market", {})
    direction = (p.get("direction") or pos_data.get("direction") or "").upper()
    return {
        "dealId": p.get("dealId") or pos_data.get("dealId"),
        "epic": p.get("epic") or market_data.get("epic") or pos_data.get("epic"),
        "size": float(p.get("size") or pos_data.get("size") or 0.0),
        "direction": direction,
        "stopLevel": float(sl) if (sl := p.get("stopLevel") or pos_data.get("stopLevel")) else None,
        "avg": float(avg) if (avg := p.get("level") or pos_data.get("level") or pos_data.get("openLevel")) else None
    }

def find_position(epic: str) -> Optional[Dict]:
    for p in get_open_positions():
        pp = parse_pos(p)
        if pp["epic"] == epic:
            return pp
    return None

def delete_position(deal_id: str) -> bool:
    log(f"🗑️ Deleting position {deal_id} via DELETE request.")
    r = capital_request("DELETE", f"/api/v1/positions/{deal_id}")
    if r.status_code in (200, 204):
        log("✅ Position deleted successfully.")
        return True
    log(f"⚠️ DELETE failed ({r.status_code}). Text: {r.text}")
    return False

def _load_ids() -> Dict[str, str]:
    try:
        with open(IDEMP_STORE, "r") as f: data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): data = {}
    now = datetime.datetime.utcnow()
    pruned = {k: iso for k, iso in data.items() if (now - datetime.datetime.fromisoformat(iso)).days <= IDEMP_TTL_DAYS}
    if len(pruned) != len(data): _save_ids(pruned)
    return pruned

def _save_ids(data: Dict[str, str]):
    with open(IDEMP_STORE, "w") as f: json.dump(data, f, indent=2)

def already_processed(signal_id: Optional[str]) -> bool:
    return bool(signal_id) and signal_id in _load_ids()

def mark_processed(signal_id: Optional[str]):
    if not signal_id: return
    data = _load_ids()
    data[signal_id] = datetime.datetime.utcnow().isoformat()
    _save_ids(data)

def place_order(epic: str, direction: str, size: float, *, force_open: bool, stop_level: Optional[float]=None):
    payload = { "epic": epic, "direction": direction.upper(), "size": float(size), "orderType": "MARKET", "forceOpen": bool(force_open), "guaranteedStop": False }
    if stop_level is not None: payload["stopLevel"] = float(stop_level)
    log(f"📤 Sending order: {payload}")
    r = capital_request("POST", "/api/v1/positions", json_body=payload)
    return r

# ===============================
#   FASTAPI WEBHOOK
# ===============================
@app.post("/webhook")
async def handle_webhook(request: Request):
    try:
        data = await request.json()
        log(f"📥 Received payload: {data}")

        symbol      = data.get("symbol")
        action      = (data.get("action") or "").lower()      # "buy" | "sell"
        intent      = (data.get("intent") or "").lower()      # "open" | "close" | "close_partial"
        signal_id   = data.get("signal_id")
        stop_loss   = data.get("stop_loss")
        size_ratio_raw = data.get("size") # KORREKTUR: Lese das 'size' Feld, nicht 'close_percent'

        if not symbol or not intent:
            raise HTTPException(400, "Missing 'symbol' or 'intent'")
        if symbol not in SYMBOL_EPIC_MAP:
            raise HTTPException(400, f"Unknown symbol: {symbol}")

        if already_processed(signal_id):
            log(f"🧊 Duplicate signal ignored (signal_id={signal_id})")
            return {"status": "duplicate_ignored", "signal_id": signal_id}

        epic = SYMBOL_EPIC_MAP[symbol]["epic"]
        pos  = find_position(epic)

        # KORREKTUR: Fasse "close" und "close_partial" zusammen
        if intent in ("close", "close_partial"):
            if not pos:
                log("ℹ️ No open position; nothing to close.")
                mark_processed(signal_id)
                return {"status": "no_position_to_close"}

            # Prüfe, ob es ein Partial Close ist
            is_partial = False
            size_ratio = 0.0
            if intent == "close_partial" and size_ratio_raw is not None:
                try:
                    # KORREKTUR: "size" ist ein Verhältnis (z.B. 0.5), kein Prozentsatz
                    size_ratio = float(size_ratio_raw)
                    is_partial = 0 < size_ratio < 1
                except (TypeError, ValueError):
                    log(f"⚠️ Invalid size for partial close ignored: {size_ratio_raw}")

            if is_partial:
                # KORREKTUR: Multipliziere direkt mit dem Verhältnis
                size_to_close = round(pos["size"] * size_ratio, 8)
                close_dir = "SELL" if pos["direction"] == "BUY" else "BUY"
                r = place_order(epic, close_dir, size_to_close, force_open=False)
                if r.status_code not in (200, 201):
                    log(f"❌ Partial close order error: {r.text}")
                    raise HTTPException(500, f"Partial close failed: {r.text}")
                log(f"✅ Partial close executed for {size_ratio*100}% of position.")
                mark_processed(signal_id)
                return {"status": "partial_close_executed", "ratio": size_ratio, "size_closed": size_to_close}
            
            # Wenn kein 'is_partial', handle es als Full Close
            if not delete_position(pos["dealId"]):
                raise HTTPException(500, "Full close via DELETE failed.")
            mark_processed(signal_id)
            return {"status": "positions_closed_fully"}

        elif intent == "open":
            if action not in ("buy", "sell"):
                raise HTTPException(400, "For 'open' you must provide action 'buy' or 'sell'")
            if pos:
                log(f"ℹ️ Position already exists for {epic}. Ignoring open signal.")
                mark_processed(signal_id)
                return {"status": "ignored_position_exists"}

            size = SYMBOL_EPIC_MAP[symbol]["size"]
            r = place_order(epic, "BUY" if action == "buy" else "SELL", size, force_open=True, stop_level=float(stop_loss) if stop_loss is not None else None)
            if r.status_code not in (200, 201):
                log(f"❌ Entry order error: {r.text}")
                raise HTTPException(500, f"Entry failed: {r.text}")

            mark_processed(signal_id)
            log("✅ Entry order executed.")
            return {"status": "entry_executed", "size": size, "direction": action.upper()}

        else:
            log(f"⚠️ Unknown intent: {intent}. Ignoring.")
            return {"status": "unknown_intent", "intent": intent}

    except HTTPException as he:
        log(f"HTTP Exception: {he.status_code} - {he.detail}")
        raise he
    except Exception as e:
        log(f"🔥 UNEXPECTED SERVER ERROR: {e}")
        raise HTTPException(status_code=500, detail=str(e))









