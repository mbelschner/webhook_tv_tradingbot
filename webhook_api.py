from fastapi import FastAPI, Request, HTTPException
import requests
import os
import json
import datetime
from dotenv import load_dotenv
from typing import Optional, Dict

# Lädt Umgebungsvariablen aus der .env-Datei
load_dotenv()

app = FastAPI()

# --- API-Zugangsdaten und Konfiguration aus .env-Datei ---
API_KEY         = os.getenv("CC_API_KEY")
IDENTIFIER      = os.getenv("CC_IDENTIFIER")
PASSWORD        = os.getenv("CC_PASSWORD")
BASE_URL        = os.getenv("CC_BASE_URL", "https://api-capital.com") # Default-URL hinzugefügt

# Globale Variablen für die Session-Tokens
CST = None
XST = None

# --- Konfiguration der handelbaren Symbole ---
# Hier werden TradingView-Symbolnamen auf die "epic"-Namen von Capital.com gemappt
# und eine Standard-Positionsgröße definiert.
SYMBOL_EPIC_MAP = {
    "ETHUSD":     {"epic": "ETHUSD",       "size": 0.12},
    "DOGEUSD":    {"epic": "DOGEUSD",      "size": 2200},
    "GOLD":       {"epic": "GOLD",         "size": 1.7},
    "SILVER":     {"epic": "SILVER",       "size": 70},
    "COPPER":     {"epic": "COPPER",       "size": 550},
    "OIL_CRUDE":  {"epic": "OIL_CRUDE",    "size": 45},
    "EU50":       {"epic": "EU50",         "size": 0.8},
    "UK100":      {"epic": "UK100",        "size": 0.4},
    "EURUSD":     {"epic": "EURUSD",       "size": 7000},
    "LRC":        {"epic": "LRC",          "size": 0.5},
    "NATURALGAS": {"epic": "NATURALGAS",   "size": 700},
    "BTCEUR":     {"epic": "BTCEUR",       "size": 0.005}
}

# --- Konfiguration für Idempotenz (Vermeidung doppelter Signale) ---
IDEMP_STORE = "processed_signals.json"
IDEMP_TTL_DAYS = 2

# ===============================
#   UTILITIES & HELPER-FUNKTIONEN
# ===============================

def log(msg: str):
    """Schreibt eine Log-Nachricht auf die Konsole und in eine Datei."""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open("webhook_log.txt", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"Error writing to log file: {e}")

def login_to_capital():
    """Meldet sich bei der Capital.com API an und holt neue Session-Tokens."""
    global CST, XST
    log("🔐 Logging in to Capital.com…")
    try:
        r = requests.post(
            f"{BASE_URL}/api/v1/session",
            json={"identifier": IDENTIFIER, "password": PASSWORD},
            headers={"X-CAP-API-KEY": API_KEY},
            timeout=15
        )
        r.raise_for_status()  # Wirft einen Fehler bei HTTP-Statuscodes 4xx/5xx
        CST = r.headers.get("CST")
        XST = r.headers.get("X-SECURITY-TOKEN")
        if not CST or not XST:
            raise RuntimeError("Login successful, but tokens not found in headers.")
        log("✅ Login successful.")
    except requests.RequestException as e:
        log(f"❌ Login failed: {e}")
        raise RuntimeError(f"Login failed: {e}") from e

def capital_request(method: str, path: str, *, json_body=None, retry=True) -> requests.Response:
    """Wrapper für API-Anfragen mit automatischer Authentifizierung und Re-Login."""
    if not (CST and XST):
        login_to_capital()
    
    url = f"{BASE_URL}{path}"
    headers = {"X-CAP-API-KEY": API_KEY, "CST": CST, "X-SECURITY-TOKEN": XST}
    if json_body:
        headers["Content-Type"] = "application/json"

    r = requests.request(method.upper(), url, headers=headers, json=json_body, timeout=20)

    # Prüfen, ob ein Re-Login notwendig ist (abgelaufener Token)
    needs_relogin = (r.status_code == 401)
    if not needs_relogin:
        try:
            j = r.json()
            if isinstance(j, dict) and j.get("errorCode") in {"error.invalid.session.token", "error.security.account.token.invalid"}:
                needs_relogin = True
        except json.JSONDecodeError:
            pass

    if needs_relogin and retry:
        log("⚠️ Session invalid/expired → re-login and retry once.")
        login_to_capital()
        return capital_request(method, path, json_body=json_body, retry=False)

    return r

def get_open_positions() -> list:
    """Ruft alle offenen Positionen vom Broker ab."""
    r = capital_request("GET", "/api/v1/positions")
    if r.status_code != 200:
        log(f"❌ Fetch positions failed: {r.text}")
        return []
    return r.json().get("positions", [])

def parse_pos(p: Dict) -> Dict:
    """Vereinheitlicht das Positions-Objekt, da die API es manchmal verschachtelt zurückgibt."""
    pos_data = p.get("position", {})
    market_data = p.get("market", {})
    return {
        "dealId": p.get("dealId") or pos_data.get("dealId"),
        "epic": p.get("epic") or market_data.get("epic") or pos_data.get("epic"),
        "size": float(p.get("size") or pos_data.get("size") or 0.0),
        "direction": p.get("direction") or pos_data.get("direction"), # "BUY" / "SELL"
        "stopLevel": float(sl) if (sl := p.get("stopLevel") or pos_data.get("stopLevel")) else None,
        "avg": float(avg) if (avg := p.get("level") or pos_data.get("level") or pos_data.get("openLevel")) else None
    }

def find_position(epic: str) -> Optional[Dict]:
    """Sucht eine offene Position anhand ihres Epic-Namens."""
    for p in get_open_positions():
        pp = parse_pos(p)
        if pp["epic"] == epic:
            return pp
    return None

def delete_position(deal_id: str) -> bool:
    """Schließt eine Position vollständig über ihre dealId."""
    log(f"🗑️ Deleting position {deal_id} via DELETE request.")
    r = capital_request("DELETE", f"/api/v1/positions/otc/{deal_id}")
    if r.status_code in (200, 204):
        log("✅ Position deleted successfully.")
        return True
    log(f"⚠️ DELETE failed ({r.status_code}). Text: {r.text}")
    return False

# --- Idempotenz-Funktionen ---
def _load_ids() -> Dict[str, str]:
    try:
        with open(IDEMP_STORE, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    
    now = datetime.datetime.utcnow()
    pruned_data = {
        k: iso for k, iso in data.items()
        if (now - datetime.datetime.fromisoformat(iso)).days <= IDEMP_TTL_DAYS
    }
    if len(pruned_data) != len(data):
        _save_ids(pruned_data)
    return pruned_data

def _save_ids(data: Dict[str, str]):
    with open(IDEMP_STORE, "w") as f:
        json.dump(data, f, indent=2)

def already_processed(signal_id: Optional[str]) -> bool:
    if not signal_id: return False
    return signal_id in _load_ids()

def mark_processed(signal_id: Optional[str]):
    if not signal_id: return
    data = _load_ids()
    data[signal_id] = datetime.datetime.utcnow().isoformat()
    _save_ids(data)

# ===============================
#   FASTAPI WEBHOOK ENDPUNKT
# ===============================
@app.post("/webhook")
async def handle_webhook(request: Request):
    try:
        data = await request.json()
        log(f"📥 Received payload: {data}")

        # --- Daten aus dem Alert auslesen ---
        symbol = data.get("symbol")
        action = data.get("action")
        intent = data.get("intent")
        signal_id = data.get("signal_id")
        stop_loss = data.get("stop_loss")
        close_percent = data.get("close_percent") # NEUER PARAMETER für partielle Exits

        # --- Validierung der Eingabedaten ---
        if not symbol or not action:
            raise HTTPException(400, "Missing 'symbol' or 'action'")
        if symbol not in SYMBOL_EPIC_MAP:
            raise HTTPException(400, f"Unknown symbol: {symbol}")
        if action not in ["buy", "sell"]:
            action = "close" # Wenn 'action' 'close' ist, behandeln wir es als Schließung

        if already_processed(signal_id):
            log(f"🧊 Duplicate signal ignored (signal_id={signal_id})")
            return {"status": "duplicate_ignored", "signal_id": signal_id}

        epic = SYMBOL_EPIC_MAP[symbol]["epic"]
        pos = find_position(epic)

        # ==========================
        #   LOGIK ZUM SCHLIESSEN
        # ==========================
        if intent == "close":
            if not pos:
                log("ℹ️ No open position found; nothing to close.")
                mark_processed(signal_id)
                return {"status": "no_position_to_close"}

            # --- NEU: Logik für partielle vs. volle Schließung ---
            is_partial_close = False
            if close_percent:
                try:
                    cp = float(close_percent)
                    if 0 < cp < 100:
                        is_partial_close = True
                except (ValueError, TypeError):
                    log(f"⚠️ Invalid close_percent value ignored: {close_percent}")

            if is_partial_close:
                # --- PARTIELLE SCHLIESSUNG ---
                size_to_close = pos["size"] * (cp / 100.0)
                # Auf eine für den Broker sinnvolle Anzahl von Nachkommastellen runden
                size_to_close = round(size_to_close, 8)
                
                close_direction = "SELL" if pos["direction"] == "BUY" else "BUY"
                
                payload = {
                    "epic": epic, 
                    "direction": close_direction, 
                    "size": size_to_close,
                    "orderType": "MARKET",
                    "forceOpen": False  # WICHTIG: Reduziert die Position, anstatt eine neue zu eröffnen
                }
                log(f"📤 Sending partial close order: {payload}")
                r_close = capital_request("POST", "/api/v1/positions/otc", json_body=payload)
                
                if r_close.status_code not in (200, 201):
                    log(f"❌ Partial close order error: {r_close.text}")
                    raise HTTPException(500, detail=f"Partial close failed: {r_close.text}")
                
                log(f"✅ Partial close executed for {cp}% of position.")
                mark_processed(signal_id)
                return {"status": "partial_close_executed", "details": payload}
            
            else:
                # --- VOLLSTÄNDIGE SCHLIESSUNG (deine bisherige Logik) ---
                log(f"Executing full close for position {pos.get('dealId')}.")
                if not delete_position(pos["dealId"]):
                     raise HTTPException(500, detail="Full close via DELETE failed.")
                
                mark_processed(signal_id)
                return {"status": "positions_closed_fully"}

        # ==========================
        #   LOGIK ZUM ÖFFNEN
        # ==========================
        elif intent == "open":
            if pos:
                log(f"ℹ️ Position already exists for {epic}. Ignoring open signal.")
                mark_processed(signal_id)
                return {"status": "ignored_position_exists"}

            entry_payload = {
                "epic": epic,
                "direction": "BUY" if action == "buy" else "SELL",
                "size": SYMBOL_EPIC_MAP[symbol]["size"],
                "orderType": "MARKET",
                "forceOpen": True,
                "guaranteedStop": False
            }
            if stop_loss:
                entry_payload["stopLevel"] = float(stop_loss)

            log(f"📤 Sending entry order: {entry_payload}")
            r_entry = capital_request("POST", "/api/v1/positions/otc", json_body=entry_payload)
            
            if r_entry.status_code not in (200, 201):
                log(f"❌ Entry order error: {r_entry.text}")
                raise HTTPException(500, detail=f"Entry failed: {r_entry.text}")

            mark_processed(signal_id)
            log("✅ Entry order executed.")
            return {"status": "entry_executed", "details": entry_payload}

        else:
            log(f"⚠️ Unknown intent: {intent}. Ignoring.")
            return {"status": "unknown_intent", "intent": intent}

    except HTTPException as he:
        log(f"HTTP Exception: {he.status_code} - {he.detail}")
        raise he
    except Exception as e:
        log(f"🔥 UNEXPECTED SERVER ERROR: {e}")
        raise HTTPException(status_code=500, detail=str(e))
