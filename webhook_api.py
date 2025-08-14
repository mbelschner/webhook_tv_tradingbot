from fastapi import FastAPI, Request, HTTPException
import requests, os, json, datetime, math
from dotenv import load_dotenv

load_dotenv()
app = FastAPI()

API_KEY    = os.getenv("CC_API_KEY")
IDENTIFIER = os.getenv("CC_IDENTIFIER")
PASSWORD   = os.getenv("CC_PASSWORD")
BASE_URL   = os.getenv("CC_BASE_URL")

CST = None
XST = None

SYMBOL_EPIC_MAP = {
    "DOGEUSD":     {"epic": "DOGEUSD",     "size": 2200},
    "GOLD":        {"epic": "GOLD",        "size": 1.7},
    "SILVER":      {"epic": "SILVER",      "size": 70},
    "COPPER":      {"epic": "COPPER",      "size": 550},
    "OIL_CRUDE":   {"epic": "OIL_CRUDE",   "size": 45},
    "EU50":        {"epic": "EU50",        "size": 0.8},
    "UK100":       {"epic": "UK100",       "size": 0.4},
    "EURUSD":      {"epic": "EURUSD",      "size": 7000},
    "LRC":         {"epic": "LRC",         "size": 0.5},
    "ETHUSD":      {"epic": "ETHUSD",      "size": 0.12},
    "NATURALGAS":  {"epic": "NATURALGAS",  "size": 700},
    "NVDA":        {"epic": "NVDA",        "size": 8}
}

SL_ABS_TICK = float(os.getenv("SL_ABS_TICK", "0"))      # optional fixer Tick (z. B. 0.01)
SL_PCT_MIN  = float(os.getenv("SL_PCT_MIN", "0.001"))   # z. B. 0.1% Mindeständerung

def log(msg:str):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open("webhook_log.txt","a") as f: f.write(line+"\n")

def login_to_capital():
    global CST, XST
    log("🔐 Logging in to Capital.com…")
    r = requests.post(f"{BASE_URL}/api/v1/session",
                      json={"identifier": IDENTIFIER, "password": PASSWORD},
                      headers={"X-CAP-API-KEY": API_KEY})
    if r.status_code != 200:
        raise RuntimeError(f"Login failed: {r.text}")
    CST = r.headers.get("CST"); XST = r.headers.get("X-SECURITY-TOKEN")
    log("✅ Login successful.")

def api_headers(json_ct=True):
    h = {"X-CAP-API-KEY": API_KEY, "CST": CST, "X-SECURITY-TOKEN": XST}
    if json_ct: h["Content-Type"] = "application/json"
    return h

def get_open_positions():
    r = requests.get(f"{BASE_URL}/api/v1/positions", headers=api_headers(json_ct=False))
    if r.status_code != 200:
        raise RuntimeError(f"Fetch positions failed: {r.text}")
    return r.json().get("positions", [])

def parse_pos(p):
    """Robust aus Positionsobjekt lesen (Capital liefert oft verschachtelt)."""
    # Versuche flaches und verschachteltes Format
    deal_id = p.get("dealId") or p.get("position", {}).get("dealId")
    epic    = p.get("epic")   or p.get("market", {}).get("epic") or p.get("position",{}).get("epic")
    size    = p.get("size")   or p.get("position", {}).get("size")
    dir_    = p.get("direction") or p.get("position", {}).get("direction")
    sl      = p.get("stopLevel") or p.get("position", {}).get("stopLevel")
    avg     = p.get("level") or p.get("position", {}).get("level") or p.get("position",{}).get("openLevel")
    return {"dealId":deal_id, "epic":epic, "size":float(size) if size else 0.0,
            "direction":dir_, "stopLevel": float(sl) if sl else None,
            "avg": float(avg) if avg else None}

def find_position(epic:str):
    for p in get_open_positions():
        pp = parse_pos(p)
        if pp["epic"] == epic:
            return pp
    return None

def need_sl_change(current_sl, new_sl, ref_price):
    if new_sl is None: return False
    if current_sl is None: return True
    abs_diff = abs(float(current_sl) - float(new_sl))
    pct_diff = abs_diff / float(ref_price) if ref_price else 0.0
    if SL_ABS_TICK > 0 and abs_diff >= SL_ABS_TICK: return True
    return pct_diff >= SL_PCT_MIN

def amend_stop_loss(deal_id:str, new_sl:float, new_tp:float=None):
    payload = {}
    if new_sl is not None: payload["stopLevel"]  = float(new_sl)
    if new_tp is not None: payload["limitLevel"] = float(new_tp)
    log(f"🛠️ Amending position {deal_id} -> {payload}")
    # PUT kann je nach Setup auch via POST + Override nötig sein:
    r = requests.put(f"{BASE_URL}/api/v1/positions/otc/{deal_id}",
                     headers=api_headers(), json=payload)
    if r.status_code == 401:
        log("⚠️ Session expired, re-login and retry amend")
        login_to_capital()
        r = requests.put(f"{BASE_URL}/api/v1/positions/otc/{deal_id}",
                         headers=api_headers(), json=payload)
    if r.status_code not in (200, 201):
        log(f"❌ Amend SL failed: {r.text}")
        raise HTTPException(status_code=500, detail=r.text)
    log("✅ Stop/Limit amended.")

@app.post("/webhook")
async def handle_webhook(request: Request):
    try:
        raw = await request.body()
        log(f"📥 Raw payload: {raw}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise HTTPException(status_code=422, detail="Invalid JSON")

        symbol = data.get("symbol"); action = data.get("action")
        stop_loss = data.get("stop_loss"); take_profit = data.get("take_profit")
        intent = data.get("intent")  # optional: "open"|"close"|"amend_sl"
        if not symbol or not action:
            raise HTTPException(400, "Missing 'symbol' or 'action'")
        if symbol not in SYMBOL_EPIC_MAP:
            raise HTTPException(400, f"Unknown symbol: {symbol}")
        if action not in ["buy","sell","close"]:
            raise HTTPException(400, f"Invalid action: {action}")

        epic = SYMBOL_EPIC_MAP[symbol]["epic"]
        size = float(data.get("size", SYMBOL_EPIC_MAP[symbol]["size"]))

        if not CST or not XST:
            login_to_capital()

        # Positionssnapshot
        pos = find_position(epic)  # None oder Dict
        dir_open_req = "BUY" if action == "buy" else "SELL"

        # -------- INTENT-LOGIK --------
        # 1) CLOSE
        if action == "close" or intent == "close":
            log(f"🔄 Close request for {symbol}")
            if not pos:
                raise HTTPException(400, f"No open position to close for {symbol}")
            close_direction = "SELL" if pos["direction"] == "BUY" else "BUY"
            payload = {"epic": epic, "direction": close_direction, "size": pos["size"],
                       "orderType":"MARKET","currencyCode":"USD","forceOpen": False}
            log(f"📤 Sending close order: {payload}")
            r2 = requests.post(f"{BASE_URL}/api/v1/positions", headers=api_headers(), json=payload)
            if r2.status_code != 200:
                log(f"❌ Close order error: {r2.text}")
                raise HTTPException(500, r2.text)
            log("✅ Position closed.")
            return {"status":"positions closed", "details": payload}

        # 2) OPEN / AMEND-SL
        # Wenn bereits gleichgerichtete Position existiert -> **kein** neuer Entry.
        if pos and pos["direction"] == dir_open_req:
            log(f"ℹ️ Position in same direction already open: {pos}")
            # Nur SL/TP anpassen, falls sinnvoll
            if need_sl_change(pos["stopLevel"], stop_loss, ref_price=pos["avg"]) or take_profit is not None:
                amend_stop_loss(pos["dealId"], stop_loss, take_profit)
                return {"status":"amended stop/limit", "dealId": pos["dealId"],
                        "old_stop": pos["stopLevel"], "new_stop": stop_loss}
            else:
                log("🟰 No meaningful SL change; ignoring entry.")
                return {"status":"ignored (dup entry / no SL change)"}

        # Gegenposition offen? -> du entscheidest: schließen oder flippen.
        if pos and pos["direction"] != dir_open_req:
            log(f"↔️ Opposite position open; closing before open.")
            # Close first
            close_direction = "SELL" if pos["direction"] == "BUY" else "BUY"
            r_close = requests.post(f"{BASE_URL}/api/v1/positions", headers=api_headers(),
                                    json={"epic":epic,"direction":close_direction,"size":pos["size"],
                                          "orderType":"MARKET","currencyCode":"USD","forceOpen": False})
            if r_close.status_code != 200:
                log(f"❌ Close-before-open error: {r_close.text}")
                raise HTTPException(500, r_close.text)

        # Neuer Entry (nur wenn keine gleichgerichtete Position existiert)
        entry_payload = {"epic":epic,"direction":dir_open_req,"size":size,
                         "orderType":"MARKET","currencyCode":"USD",
                         "forceOpen": False}   # <— wichtig
        if stop_loss is not None:  entry_payload["stopLevel"]  = float(stop_loss)
        if take_profit is not None:entry_payload["limitLevel"] = float(take_profit)

        log(f"📤 Sending entry order: {entry_payload}")
        r = requests.post(f"{BASE_URL}/api/v1/positions", headers=api_headers(), json=entry_payload)
        if r.status_code == 401:
            log("⚠️ Session expired, re-login and retry entry")
            login_to_capital()
            r = requests.post(f"{BASE_URL}/api/v1/positions", headers=api_headers(), json=entry_payload)
        if r.status_code != 200:
            log(f"❌ Entry order error: {r.text}")
            raise HTTPException(500, r.text)

        log("✅ Entry order executed.")
        return {"status":"entry executed", "details": entry_payload}

    except HTTPException as he:
        log(f"⚠️ HTTPException: {he.detail}"); raise
    except Exception as e:
        log(f"🔥 Unexpected error: {e}")
        raise HTTPException(500, str(e))
