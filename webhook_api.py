from fastapi import FastAPI, Request, HTTPException
import requests
import os
from dotenv import load_dotenv
import json
import datetime

load_dotenv()

app = FastAPI()

# Capital.com API-Zugangsdaten
API_KEY = os.getenv("CC_API_KEY")
IDENTIFIER = os.getenv("CC_IDENTIFIER")
PASSWORD = os.getenv("CC_PASSWORD")
BASE_URL = os.getenv("CC_BASE_URL")

# Capital.com Session Tokens (global)
CST = None
XST = None

# Symbol-Epic-Zuordnung
SYMBOL_EPIC_MAP = {
    "BTCUSD": "BTCUSD",
    "GOLD": "GOLD",
    "SILVER": "SILVER",
    # Weitere hinzufügen...
}

# Logging-Funktion
def log(msg):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")
    with open("webhook_log.txt", "a") as f:
        f.write(f"[{timestamp}] {msg}\n")

# Login-Funktion
def login_to_capital():
    global CST, XST
    log("🔐 Logging in to Capital.com...")
    res = requests.post(
        f"{BASE_URL}/api/v1/session",
        json={"identifier": IDENTIFIER, "password": PASSWORD},
        headers={"X-CAP-API-KEY": API_KEY}
    )
    if res.status_code != 200:
        raise RuntimeError(f"Login fehlgeschlagen: {res.text}")

    CST = res.headers.get("CST")
    XST = res.headers.get("X-SECURITY-TOKEN")
    log("✅ Login erfolgreich.")

# Webhook Endpoint
@app.post("/webhook")
async def handle_webhook(request: Request):
    try:
        raw_body = await request.body()
        log(f"📥 Raw Payload: {raw_body}")

        try:
            data = json.loads(raw_body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=422, detail="❌ Ungültiges JSON")

        symbol = data.get("symbol")
        action = data.get("action")
        size = data.get("size", 0.1)  # Optional: Größe übergeben, sonst Default

        if not symbol or not action:
            raise HTTPException(status_code=400, detail="Fehlende 'symbol' oder 'action'")

        if symbol not in SYMBOL_EPIC_MAP:
            raise HTTPException(status_code=400, detail=f"Unbekanntes Symbol: {symbol}")

        if action not in ["buy", "sell"]:
            raise HTTPException(status_code=400, detail=f"Ungültige Aktion: {action}")

        epic = SYMBOL_EPIC_MAP[symbol]
        direction = "BUY" if action == "buy" else "SELL"

        if not CST or not XST:
            login_to_capital()

        order_payload = {
            "epic": epic,
            "direction": direction,
            "size": size,
            "orderType": "MARKET",
            "currencyCode": "USD",
            "forceOpen": True
        }

        log(f"📤 Sende Order: {order_payload}")
        response = requests.post(
            f"{BASE_URL}/api/v1/positions",
            headers={
                "X-CAP-API-KEY": API_KEY,
                "CST": CST,
                "X-SECURITY-TOKEN": XST,
                "Content-Type": "application/json"
            },
            json=order_payload
        )

        if response.status_code == 401:
            log("⚠️ Session expired – erneuter Login")
            login_to_capital()
            return await handle_webhook(request)  # Retry

        if response.status_code != 200:
            log(f"❌ Fehler beim Ordern: {response.text}")
            raise HTTPException(status_code=500, detail=response.text)

        log("✅ Order erfolgreich gesendet.")
        return {"status": "order executed", "details": order_payload}

    except Exception as e:
        log(f"🔥 Allgemeiner Fehler: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
