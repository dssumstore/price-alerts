#!/usr/bin/env python3
"""
price_alert.py  -  Memecoin price-alert watcher (DexScreener -> WhatsApp)

Reads your coin list from  watchlist.txt , checks live prices on DexScreener,
and messages your WhatsApp (via CallMeBot) when a target is crossed.

Designed to run on GitHub Actions (free, always-on, no PC needed):
  - one quick pass per run (the Action re-runs it on a schedule)
  - remembers which alerts already fired in  state.json
  - WhatsApp phone + apikey are read from environment variables (GitHub Secrets)

Uses only Python's standard library - nothing to install.

Run locally to test:    python price_alert.py
Run forever on a VPS:   python price_alert.py --loop
"""

import json
import os
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from email.mime.text import MIMEText

# ---------------------------------------------------------------------------
# Settings (sensible defaults; override with environment variables if you like)
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_FILE = os.path.join(HERE, "watchlist.txt")
STATE_FILE = os.path.join(HERE, "state.json")

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "30"))   # only used with --loop
REPEAT_ALERT = os.environ.get("REPEAT_ALERT", "false").lower() == "true"
REPEAT_COOLDOWN = int(os.environ.get("REPEAT_COOLDOWN", "3600"))

DEXS_URL = "https://api.dexscreener.com/latest/dex/tokens/{addr}"

# --- Notification credentials (set these as GitHub Secrets / env vars) ------
WHATSAPP_PHONE = os.environ.get("WHATSAPP_PHONE", "")     # e.g. +447911123456
WHATSAPP_APIKEY = os.environ.get("WHATSAPP_APIKEY", "")   # from CallMeBot

# Optional extras (leave blank to ignore)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Watchlist + state
# ---------------------------------------------------------------------------
def load_watchlist():
    """Each non-comment line:  LABEL, address, chain, direction, target"""
    alerts = []
    if not os.path.exists(WATCHLIST_FILE):
        log(f"ERROR: {WATCHLIST_FILE} not found.")
        return alerts
    with open(WATCHLIST_FILE, encoding="utf-8") as f:
        for n, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) != 5:
                log(f"  skipping line {n} (need 5 comma-separated values): {line}")
                continue
            label, address, chain, direction, target = parts
            direction = direction.lower()
            if direction not in ("above", "below"):
                log(f"  skipping line {n} (direction must be above/below): {line}")
                continue
            try:
                target = float(target)
            except ValueError:
                log(f"  skipping line {n} (target not a number): {line}")
                continue
            alerts.append({"label": label, "address": address, "chain": chain,
                           "direction": direction, "target": target})
    return alerts


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def alert_key(a):
    return f"{a['chain']}:{a['address'].lower()}:{a['direction']}:{a['target']}"


# ---------------------------------------------------------------------------
# Price fetch
# ---------------------------------------------------------------------------
def fetch_price(address, chain):
    """Return (price_usd, pair) for the deepest-liquidity pair, or (None, None)."""
    url = DEXS_URL.format(addr=address)
    req = urllib.request.Request(url, headers={"User-Agent": "price-alert/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    pairs = data.get("pairs") or []
    if chain:
        pairs = [p for p in pairs if (p.get("chainId") or "").lower() == chain.lower()]
    if not pairs:
        return None, None
    pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0),
               reverse=True)
    best = pairs[0]
    price = best.get("priceUsd")
    try:
        return (float(price) if price is not None else None), best
    except (TypeError, ValueError):
        return None, best


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
def _http_get(url, params):
    full = url + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(full, timeout=20) as r:
        return r.read()


def notify_whatsapp(title, body):
    if not (WHATSAPP_PHONE and WHATSAPP_APIKEY):
        return
    try:
        _http_get("https://api.callmebot.com/whatsapp.php",
                  {"phone": WHATSAPP_PHONE, "text": f"{title}\n{body}",
                   "apikey": WHATSAPP_APIKEY})
        log("  -> WhatsApp sent")
    except Exception as e:
        log(f"  -> WhatsApp failed: {e}")


def notify_telegram(title, body):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        _http_get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                  {"chat_id": TELEGRAM_CHAT_ID, "text": f"{title}\n{body}"})
        log("  -> Telegram sent")
    except Exception as e:
        log(f"  -> Telegram failed: {e}")


def notify_ntfy(title, body):
    if not NTFY_TOPIC:
        return
    try:
        req = urllib.request.Request(
            f"{NTFY_SERVER.rstrip('/')}/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={"Title": title, "Priority": "high", "Tags": "money_with_wings"})
        urllib.request.urlopen(req, timeout=20)
        log("  -> ntfy push sent")
    except Exception as e:
        log(f"  -> ntfy failed: {e}")


def send_all(title, body):
    notify_whatsapp(title, body)
    notify_telegram(title, body)
    notify_ntfy(title, body)


def any_notifier_configured():
    return bool((WHATSAPP_PHONE and WHATSAPP_APIKEY)
                or (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
                or NTFY_TOPIC)


def condition_met(direction, price, target):
    return price <= target if direction == "below" else price >= target


# ---------------------------------------------------------------------------
# Main check pass
# ---------------------------------------------------------------------------
def run_once(alerts, state):
    changed = False
    for a in alerts:
        key = alert_key(a)
        last = state.get(key, 0)
        if not REPEAT_ALERT and last:
            continue
        if REPEAT_ALERT and last and (time.time() - last) < REPEAT_COOLDOWN:
            continue

        try:
            price, pair = fetch_price(a["address"], a["chain"])
        except Exception as e:
            log(f"{a['label']}: fetch error: {e}")
            continue

        if price is None:
            log(f"{a['label']}: no {a['chain']} pair found (check address/chain).")
            continue

        log(f"{a['label']}: ${price:.10g}  (target {a['direction']} {a['target']})")

        if condition_met(a["direction"], price, a["target"]):
            arrow = "dropped to" if a["direction"] == "below" else "hit"
            url = (pair or {}).get("url", "")
            title = f"ALERT: {a['label']} {arrow} ${price:.10g}"
            body = (f"{a['label']} is ${price:.10g}\n"
                    f"Target was {a['direction']} {a['target']}\n{url}")
            log(f"  *** TRIGGERED: {title}")
            send_all(title, body)
            state[key] = time.time()
            changed = True
    return changed


def main():
    if not any_notifier_configured():
        log("ERROR: no notification method configured. Set WHATSAPP_PHONE and "
            "WHATSAPP_APIKEY (or Telegram/ntfy) as environment variables/Secrets.")
        sys.exit(1)

    alerts = load_watchlist()
    if not alerts:
        log("No valid alerts in watchlist.txt. Nothing to do.")
        return

    state = load_state()
    log(f"Loaded {len(alerts)} alert(s).")

    loop = "--loop" in sys.argv
    if loop:
        log(f"Loop mode: checking every {CHECK_INTERVAL}s. Ctrl+C to stop.")
        while True:
            if run_once(alerts, state):
                save_state(state)
            time.sleep(CHECK_INTERVAL)
    else:
        if run_once(alerts, state):
            save_state(state)
        log("Pass complete.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped.")
