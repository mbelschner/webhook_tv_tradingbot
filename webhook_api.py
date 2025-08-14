from fastapi import FastAPI, Request, HTTPException
import requests, os, json, datetime
from dotenv import load_dotenv
from typing import Optional, Dict

load_dotenv()
app = FastAPI()

API_KEY    = os.getenv("CC_API_KEY")
IDENTIFIER = os.getenv("CC_IDENTIFIER")
PASSWORD   = os.getenv("CC_PASSWORD")
BASE_URL   = os.getenv("CC_BASE_URL")

CST = None
XST = None

# ---- Konfiguration ----
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

SL_ABS_TICK = float(os.getenv("SL_ABS_TICK", "0"))      # fixer Tick (z. B. 0.01); 0 = aus
SL_PCT_MIN  = float(os.getenv("SL_PCT_MIN", "0.001"))   # 0.1% Mindeständerung

IDEMP_STORE = "processed_signals.json"
IDEMP_TTL_DAYS = 2

# ---- Utilities ----
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
                      headers={"X-CAP-API-KEY": API_KEY}, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"Login failed: {r.text}")
    CST = r.headers.get("CST")
    XST = r.headers.get("X-SECURITY-TOKEN")
    log("✅ Login successful.")

def capital_request(method: str, path: str, *, json_body=None, headers_extra=None, retry=True) -> requests.Response:
    """Auth-Wrapper mit Auto-ReLogin & einmaligem Retry bei Token-Expiry."""
    if not (CST and XST):
        login_to_capital()
    url = f"{BASE_URL}{path}"
    headers = {"X-CAP-API-KEY": API_KEY, "CST": CST, "X-SECURITY-TOKEN": XST}
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    if headers_extra:
        headers.update(headers_extra)

    r = requests.request(method.upper(), url, headers=headers, json=json_body, timeout=20)

    needs_relogin = (r.status_code == 401)
    try:
        j = r.json()
        if isinstance(j, dict) and j.get("errorCode") in {"error.invalid.session.token", "error.security.account.token.invalid"}:
            needs_relogin = True
    except Exception:
        pass

    if needs_relogin and retry:
        log("⚠️ Session invalid/expired → re-login, retry once")
        login_to_capital()
        return capital_request(method, path, json_body=json_body, headers_extra=headers_extra, retry=False)

    return r

def get_open_positions():
    r = capital_request("GET", "/api/v1/positions")
    if r.status_code != 200:
        raise RuntimeError(f"Fetch positions failed: {r.text}")
    return r.json().get("positions", [])

def parse_pos(p: Dict):
    # Capital kann verschachtelt liefern
    deal_id = p.get("dealId") or p.get("position", {}).get("dealId")
    epic    = p.get("epic")   or p.get("market", {}).get("epic") or p.get("position",{}).get("epic")
    size    = p.get("size")   or p.get("position", {}).get("size")
    dir_    = p.get("direction") or p.get("position", {}).get("direction")  # "BUY"/"SELL"
    sl      = p.get("stopLevel") or p.get("position", {}).get("stopLevel")
    avg     = (p.get("level") or p.get("position", {}).get("level")
               or p.get("position", {}).get("openLevel"))
    return {"dealId": deal_id, "epic": epic, "size": float(size) if size else 0.0,
            "direction": dir_, "stopLevel": float(sl) if sl else None,
            "avg": float(avg) if avg else None}

def find_position(epic:str) -> Optional[Dict]:
    for p in get_open_positions():
        pp = parse_pos(p)
        if pp["epic"] == epic:
            return pp
    return None

def need_sl_change(current_sl, new_sl, ref_price):
    if new_sl is None: return False
    if current_sl is None: return True
    abs_diff = abs(float(current_sl) - float(new_sl))
    if SL_ABS_TICK > 0 and abs_diff >= SL_ABS_TICK:
        return True
    pct_diff = abs_diff / float(ref_price) if ref_price else 0.0
    return pct_diff >= SL_PCT_MIN

def amend_stop_limit(deal_id: str, new_sl: float = None, new_tp: float = None):
    payload = {}
    if new_sl is not None: payload["stopLevel"]  = float(new_sl)
    if new_tp is not None: payload["limitLevel"] = float(new_tp)
    if not payload:
        return
    log(f"🛠️ Amending position {deal_id} -> {payload}")
    r = capital_request("PUT", f"/api/v1/positions/otc/{deal_id}", json_body=payload)
    if r.status_code not in (200, 201):
        log(f"❌ Amend failed ({r.status_code}). Fallback text: {r.text}")
        raise HTTPException(status_code=500, detail=r.text)
    log("✅ Stop/Limit amended.")

def delete_position(deal_id: str):
    log(f"🗑️ DELETE position {deal_id}")
    r = capital_request("DELETE", f"/api/v1/positions/otc/{deal_id}")
    if r.status_code in (200, 204):
        log("✅ Position deleted.")
        return True
    log(f"⚠️ DELETE failed ({r.status_code}). Text: {r.text}")
    return False

# ---- Idempotenz: signal_id Cache ----
def _load_ids() -> Dict[str, str]:
    try:
        with open(IDEMP_STORE, "r") as f: data = json.load(f)
    except Exception:
        data = {}
    # prune old
    now = datetime.datetime.utcnow()
    keep = {}
    for k, iso in data.items():
        try:
            t = datetime.datetime.fromisoformat(iso)
            if (now - t).days <= IDEMP_TTL_DAYS: keep[k] = iso
        except Exception:
            pass
    if keep != data:
        with open(IDEMP_STORE, "w") as f: json.dump(keep, f)
    return keep

def _save_ids(data: Dict[str,str]):
    with open(IDEMP_STORE, "w") as f: json.dump(data, f)

def already_processed(signal_id: Optional[str]) -> bool:
    if not signal_id: return False
    data = _load_ids()
    return signal_id in data

def mark_processed(signal_id: Optional[str]):
    if not signal_id: return
    data = _load_ids()
    data[signal_id] = datetime.datetime.utcnow().isoformat()
    _save_ids(data)

# ---- Webhook ----
@app.post("/webhook")
async def handle_webhook(request: Request):
    try:
        raw = await request.body()
        log(f"📥 Raw payload: {raw}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise HTTPException(status_code=422, detail="Invalid JSON")

        symbol      = data.get("symbol")
        action      = data.get("action")     # "buy"|"sell"|"close"
        intent      = data.get("intent")     # optional
        side        = data.get("side")       # optional: "long"|"short" (nur bei close nützlich)
        stop_loss   = data.get("stop_loss")  # optional
        take_profit = data.get("take_profit")# optional
        signal_id   = data.get("signal_id")  # optional für Idempotenz

        if not symbol or not action:
            raise HTTPException(400, "Missing 'symbol' or 'action'")
        if symbol not in SYMBOL_EPIC_MAP:
            raise HTTPException(400, f"Unknown symbol: {symbol}")
        if action not in ["buy","sell","close"]:
            raise HTTPException
