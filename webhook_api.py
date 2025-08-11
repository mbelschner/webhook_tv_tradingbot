from fastapi import FastAPI, Request, HTTPException
import requests
import os
from dotenv import load_dotenv
import json
import datetime

load_dotenv()

app = FastAPI()

# Capital.com API-Zugangsdaten
API_KEY    = os.getenv("CC_API_KEY")
IDENTIFIER = os.getenv("CC_IDENTIFIER")
PASSWORD   = os.getenv("CC_PASSWORD")
BASE_URL   = os.getenv("CC_BASE_URL")  # z.B. https://api-capital.backend-capital.com

# Session-Tokens (global)
CST = None
XST = None

# Symbol → Epic + Default Size Mapping
SYMBOL_EPIC_MAP = {
    "DOGEUSD":     {"epic": "DOGEUSD",     "size": 2200},
    "GOLD":       {"epic": "GOLD",       "size": 1.5},
    "SILVER":     {"epic": "SILVER",     "size": 65},
    "COPPER":     {"epic": "COPPER",     "size": 500},
    "OIL_CRUDE":  {"epic": "OIL_CRUDE",  "size": 35},
    "EU50":       {"epic": "EU50",       "size": 0.8},
    "UK100":      {"epic": "UK100",      "size": 0.4},
    "EURUSD":     {"epic": "EURUSD",     "size": 6000},
    "LRC":        {"epic": "LRC",        "size": 0.5},
    "ETHUSD":     {"epic": "ETHUSD",     "size": 0.3},
    "PLATINUM":   {"epic": "PLATINUM",   "size": 1.1}
    # Weitere hinzufügen nach Bedarf
}

def log(msg: str):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open("webhook_log.txt", "a") as f:
        f.write(line + "\n")

def login_to_capital():
    """Loggt sich bei Capital.com ein und aktualisiert CST/XST."""
    global CST, XST
    log("🔐 Logging in to Capital.com…")
    res = requests.post(
        f"{BASE_URL}/api/v1/session",
        json={"identifier": IDENTIFIER, "password": PASSWORD},
        headers={"X-CAP-API-KEY": API_KEY}
    )
    if res.status_code != 200:
        raise RuntimeError(f"Login failed: {res.text}")
    CST = res.headers.get("CST")
    XST = res.headers.get("X-SECURITY-TOKEN")
    log("✅ Login successful.")

def get_open_positions():
    """Holt offene Positionen vom Konto."""
    res = requests.get(
        f"{BASE_URL}/api/v1/positions",
        headers={
            "X-CAP-API-KEY": API_KEY,
            "CST": CST,
            "X-SECURITY-TOKEN": XST
        }
    )
    if res.status_code != 200:
        raise RuntimeError(f"Fetch positions failed: {res.text}")
    return res.json().get("positions", [])

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
        action      = data.get("action")
        stop_loss   = data.get("stop_loss")    # optional
        take_profit = data.get("take_profit")  # optional
        # size can override default
        if not symbol or not action:
            raise HTTPException(status_code=400, detail="Missing 'symbol' or 'action'")
        if symbol not in SYMBOL_EPIC_MAP:
            raise HTTPException(status_code=400, detail=f"Unknown symbol: {symbol}")
        if action not in ["buy", "sell", "close"]:
            raise HTTPException(status_code=400, detail=f"Invalid action: {action}")

        symbol_info = SYMBOL_EPIC_MAP[symbol]
        epic        = symbol_info["epic"]
        size        = float(data.get("size", symbol_info["size"]))

        # ensure logged in
        if not CST or not XST:
            login_to_capital()

        # ENTRY: buy / sell
        if action in ["buy", "sell"]:
            direction     = "BUY" if action == "buy" else "SELL"
            order_payload = {
                "epic": epic,
                "direction": direction,
                "size": size,
                "orderType": "MARKET",
                "currencyCode": "USD",
                "forceOpen": True
            }
            if stop_loss is not None:
                order_payload["stopLevel"] = float(stop_loss)
            if take_profit is not None:
                order_payload["limitLevel"] = float(take_profit)

            log(f"📤 Sending entry order: {order_payload}")
            res = requests.post(
                f"{BASE_URL}/api/v1/positions",
                headers={
                    "X-CAP-API-KEY": API_KEY,
                    "CST": CST,
                    "X-SECURITY-TOKEN": XST,
                    "Content-Type": "application/json"
                },
                json=order_payload
            )

            # Re-login bei Session-Expiration
            if res.status_code == 401:
                log("⚠️ Session expired, re-login and retry entry")
                login_to_capital()
                return await handle_webhook(request)

            if res.status_code != 200:
                log(f"❌ Entry order error: {res.text}")
                raise HTTPException(status_code=500, detail=res.text)

            log("✅ Entry order executed.")
            return {"status": "entry executed", "details": order_payload}

        # EXIT: close existing position(s)
        else:  # action == "close"
            log(f"🔄 Closing position(s) for {symbol}")
            positions = get_open_positions()
            closed    = []
            for pos in positions:
                if pos.get("epic") == epic:
                    # Determine opposite direction to close
                    dir_ = pos.get("direction")  # "BUY" or "SELL"
                    close_direction = "SELL" if dir_ == "BUY" else "BUY"
                    position_size   = pos.get("size")
                    close_payload   = {
                        "epic": epic,
                        "direction": close_direction,
                        "size": position_size,
                        "orderType": "MARKET",
                        "currencyCode": "USD",
                        "forceOpen": False
                    }
                    log(f"📤 Sending close order: {close_payload}")
                    r2 = requests.post(
                        f"{BASE_URL}/api/v1/positions",
                        headers={
                            "X-CAP-API-KEY": API_KEY,
                            "CST": CST,
                            "X-SECURITY-TOKEN": XST,
                            "Content-Type": "application/json"
                        },
                        json=close_payload
                    )
                    if r2.status_code != 200:
                        log(f"❌ Close order error: {r2.text}")
                        raise HTTPException(status_code=500, detail=r2.text)
                    closed.append(close_payload)

            if not closed:
                raise HTTPException(status_code=400, detail=f"No open position to close for {symbol}")

            log(f"✅ Closed positions: {closed}")
            return {"status": "positions closed", "details": closed}

    except HTTPException as he:
        log(f"⚠️ HTTPException: {he.detail}")
        raise
    except Exception as e:
        log(f"🔥 Unexpected error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


