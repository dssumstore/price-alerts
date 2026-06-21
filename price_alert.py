#!/usr/bin/env python3
"""
price_alert.py  -  Memecoin alert watcher (DexScreener -> WhatsApp)

Reads watchlist.txt and WhatsApps you (via CallMeBot) on:
  - price targets (with optional "ladder" follow-ups every X% further move)
  - sudden pumps / dumps (auto-detected; no number needed)

Standard library only - nothing to install.

WATCHLIST FORMAT (one alert per line). The 4th word picks the alert TYPE:

  PRICE TARGET:
    LABEL, address, chain, below, TARGET[, STEP%]
    LABEL, address, chain, above, TARGET[, STEP%]

  PUMP / DUMP (auto - you do NOT set a percentage):
    LABEL, address, chain, pump
    LABEL, address, chain, dump
      It watches the token's 5-minute and 1-hour moves and alerts when there's
      a noticeable spike, telling you the exact %. Re-alerts at most once per
      PUMP_COOLDOWN_MIN minutes (default 30).
      Optional sensitivity word: low (calmer), med (default), high (touchier):
        LABEL, address, chain, pump, high
      Or pin an exact threshold if you really want one:
        LABEL, address, chain, pump, 25, h1   (25% over the h1 window)

  Lines starting with # are ignored.

  Run once (GitHub Actions):   python price_alert.py
  Run forever (VM/Pi):         python price_alert.py --loop
"""

import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_FILE = os.path.join(HERE, "watchlist.txt")
STATE_FILE = os.path.join(HERE, "state.json")

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "30"))      # seconds (--loop)
PUMP_COOLDOWN_MIN = float(os.environ.get("PUMP_COOLDOWN_MIN", "30"))
VOLUME_COOLDOWN_MIN = float(os.environ.get("VOLUME_COOLDOWN_MIN", "60"))

DEXS_BULK_URL = "https://api.dexscreener.com/latest/dex/tokens/{addrs}"

WHATSAPP_PHONE = os.environ.get("WHATSAPP_PHONE", "")
WHATSAPP_APIKEY = os.environ.get("WHATSAPP_APIKEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")

WINDOW_LABEL = {"m5": "5 min", "h1": "1 hour", "h6": "6 hours", "h24": "24 hours"}
VALID_WINDOWS = set(WINDOW_LABEL)

# Auto-pump sensitivity: how big a move counts as "noticeable", per window.
SENSITIVITY = {
    "low":  {"m5": 15.0, "h1": 35.0},   # only big spikes
    "med":  {"m5": 8.0,  "h1": 20.0},   # sensible default
    "high": {"m5": 5.0,  "h1": 12.0},   # touchy, more alerts
}


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Formatting (NO "$" anywhere - CallMeBot's shell mangles $0, $1, ...)
# ---------------------------------------------------------------------------
def fmt_price(p):
    try:
        p = float(p)
    except (TypeError, ValueError):
        return str(p)
    if p == 0:
        return "0"
    ap = abs(p)
    if ap >= 1:
        s = f"{p:,.4f}"
    else:
        exp = math.floor(math.log10(ap))
        decimals = min(18, max(4, -exp + 3))
        s = f"{p:.{decimals}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def fmt_int(v):
    try:
        return f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return str(v)


# ---------------------------------------------------------------------------
# Watchlist + state
# ---------------------------------------------------------------------------
def load_watchlist():
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
            if len(parts) < 4:
                log(f"  skipping line {n} (need at least 4 values): {line}")
                continue
            label, address, chain, mode = parts[:4]
            mode = mode.lower()
            extra = [p for p in parts[4:] if p != ""]

            a = {"label": label, "address": address, "chain": chain, "mode": mode,
                 "v1": None, "step": 0.0, "window": "h1", "cooldown_min": None,
                 "pump_kind": "auto", "sens": "med"}

            if mode in ("above", "below"):
                if not extra:
                    log(f"  skipping line {n} ({mode} needs a target price): {line}")
                    continue
                try:
                    a["v1"] = float(extra[0])
                except ValueError:
                    log(f"  skipping line {n} (target not a number): {line}")
                    continue
                if len(extra) > 1:
                    try:
                        a["step"] = float(extra[1])
                    except ValueError:
                        log(f"  line {n}: step not a number, ignoring: {line}")

            elif mode in ("pump", "dump"):
                if extra:
                    w = extra[0].lower()
                    if w in ("low", "med", "medium", "high"):
                        a["sens"] = "med" if w == "medium" else w
                    else:
                        try:
                            a["v1"] = float(extra[0])
                            a["pump_kind"] = "fixed"
                        except ValueError:
                            log(f"  line {n}: '{extra[0]}' not low/med/high or a "
                                f"number, using auto/med: {line}")
                        if a["pump_kind"] == "fixed" and len(extra) > 1:
                            if extra[1].lower() in VALID_WINDOWS:
                                a["window"] = extra[1].lower()
                            else:
                                log(f"  line {n}: window must be m5/h1/h6/h24, "
                                    f"using h1: {line}")

            elif mode == "volume":
                if not extra:
                    log(f"  skipping line {n} (volume needs a multiplier): {line}")
                    continue
                try:
                    a["v1"] = float(extra[0])
                except ValueError:
                    log(f"  skipping line {n} (multiplier not a number): {line}")
                    continue
                if len(extra) > 1:
                    try:
                        a["cooldown_min"] = float(extra[1])
                    except ValueError:
                        log(f"  line {n}: cooldown not a number, using default: {line}")
            else:
                log(f"  skipping line {n} (bad type '{mode}'): {line}")
                continue

            alerts.append(a)
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
    base = f"{a['chain']}:{a['address'].lower()}:{a['mode']}"
    if a["mode"] in ("above", "below"):
        return f"{base}:{a['v1']}"
    if a["mode"] in ("pump", "dump"):
        if a["pump_kind"] == "fixed":
            return f"{base}:{a['v1']}:{a['window']}"
        return f"{base}:auto:{a['sens']}"
    return f"{base}:{a['v1']}"   # volume


# ---------------------------------------------------------------------------
# Batched price fetch
# ---------------------------------------------------------------------------
def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def fetch_all_pairs(addresses):
    pairs = []
    unique = list(dict.fromkeys(a.lower() for a in addresses))
    for chunk in _chunks(unique, 30):
        url = DEXS_BULK_URL.format(addrs=",".join(chunk))
        req = urllib.request.Request(url, headers={"User-Agent": "price-alert/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        pairs.extend(data.get("pairs") or [])
    return pairs


def best_pair(pairs, address, chain):
    addr, ch = address.lower(), chain.lower()

    def liq(p):
        return float((p.get("liquidity") or {}).get("usd") or 0)

    base_matches, quote_matches = [], []
    for p in pairs:
        if (p.get("chainId") or "").lower() != ch:
            continue
        bt = ((p.get("baseToken") or {}).get("address") or "").lower()
        qt = ((p.get("quoteToken") or {}).get("address") or "").lower()
        if bt == addr:
            base_matches.append(p)
        elif qt == addr:
            quote_matches.append(p)
    pool = base_matches or quote_matches
    return max(pool, key=liq) if pool else None


def pair_price(pair):
    try:
        return float(pair.get("priceUsd"))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
def _http_get(url, params):
    with urllib.request.urlopen(url + "?" + urllib.parse.urlencode(params),
                                timeout=20) as r:
        return r.read()


def notify_whatsapp(title, body):
    if not (WHATSAPP_PHONE and WHATSAPP_APIKEY):
        return
    try:
        _http_get("https://api.callmebot.com/whatsapp.php",
                  {"phone": WHATSAPP_PHONE, "text": f"{title}\n\n{body}",
                   "apikey": WHATSAPP_APIKEY})
        log("  -> WhatsApp sent")
    except Exception as e:
        log(f"  -> WhatsApp failed: {e}")


def notify_telegram(title, body):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        _http_get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                  {"chat_id": TELEGRAM_CHAT_ID, "text": f"{title}\n\n{body}"})
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
            headers={"Title": title, "Priority": "high", "Tags": "rotating_light"})
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


def cooled_down(state, key, now, minutes):
    return (now - state.get(key, 0)) >= minutes * 60


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Handlers (each returns True if state changed)
# ---------------------------------------------------------------------------
def handle_target(a, price, url, state, key):
    st = state.get(key)
    changed = False
    if st is not None and not isinstance(st, dict):
        st = {"first": price, "ref": price}
        state[key] = st
        changed = True

    if not isinstance(st, dict):
        hit = price <= a["v1"] if a["mode"] == "below" else price >= a["v1"]
        if hit:
            verb = "dropped to" if a["mode"] == "below" else "rose to"
            title = f"{a['label']} {verb} {fmt_price(price)} USD"
            body = (f"Price now: {fmt_price(price)} USD\n"
                    f"Your target: {a['mode']} {fmt_price(a['v1'])} USD\n{url}")
            log(f"  *** TARGET: {title}")
            send_all(title, body)
            state[key] = {"first": price, "ref": price, "ts": time.time()}
            changed = True
    else:
        step = a.get("step", 0) or 0
        if step > 0:
            ref = st.get("ref") or price
            crossed = (price <= ref * (1 - step / 100.0) if a["mode"] == "below"
                       else price >= ref * (1 + step / 100.0))
            if crossed:
                first = st.get("first", price)
                move = (price / first - 1) * 100 if first else 0
                if a["mode"] == "below":
                    title = f"{a['label']} still falling: {fmt_price(price)} USD"
                    change = f"Down {abs(move):.1f}% since your alert"
                else:
                    title = f"{a['label']} still climbing: {fmt_price(price)} USD"
                    change = f"Up {abs(move):.1f}% since your alert"
                body = (f"Price now: {fmt_price(price)} USD\n"
                        f"{change} (alert was at {fmt_price(first)} USD)\n{url}")
                log(f"  *** STEP: {title}")
                send_all(title, body)
                st["ref"] = price
                st["ts"] = time.time()
                state[key] = st
                changed = True
    return changed


def detect_pump(a, pair):
    """Return (percent, window) if a pump/dump should fire, else None."""
    pc = pair.get("priceChange") or {}
    if a["pump_kind"] == "fixed":
        ch = _num(pc.get(a["window"]))
        if ch is None:
            return None
        if a["mode"] == "pump" and ch >= a["v1"]:
            return ch, a["window"]
        if a["mode"] == "dump" and ch <= -a["v1"]:
            return ch, a["window"]
        return None

    # auto: check the fast window first, then the hourly one
    thr = SENSITIVITY[a["sens"]]
    for w in ("m5", "h1"):
        ch = _num(pc.get(w))
        if ch is None:
            continue
        if a["mode"] == "pump" and ch >= thr[w]:
            return ch, w
        if a["mode"] == "dump" and ch <= -thr[w]:
            return ch, w
    return None


def handle_pump(a, price, pair, url, state, key, now):
    hit = detect_pump(a, pair)
    if hit is None:
        return False
    ch, window = hit
    if not cooled_down(state, key, now, PUMP_COOLDOWN_MIN):
        return False
    wl = WINDOW_LABEL.get(window, window)
    if a["mode"] == "pump":
        title = f"{a['label']} pumping +{ch:.1f}% ({wl})"
        line = f"Up {ch:.1f}% in the last {wl}"
    else:
        title = f"{a['label']} dumping {ch:.1f}% ({wl})"
        line = f"Down {abs(ch):.1f}% in the last {wl}"
    body = f"Price now: {fmt_price(price)} USD\n{line}\n{url}"
    log(f"  *** {a['mode'].upper()}: {title}")
    send_all(title, body)
    state[key] = now
    return True


def handle_volume(a, price, pair, url, state, key, now):
    vol = pair.get("volume") or {}
    h1 = _num(vol.get("h1")) or 0
    h24 = _num(vol.get("h24")) or 0
    avg_hour = h24 / 24.0
    if avg_hour <= 0:
        return False
    ratio = h1 / avg_hour
    if ratio < a["v1"]:
        return False
    cooldown = a["cooldown_min"] if a["cooldown_min"] else VOLUME_COOLDOWN_MIN
    if not cooled_down(state, key, now, cooldown):
        return False
    title = f"{a['label']} volume spike {ratio:.1f}x"
    body = (f"Last hour volume: {fmt_int(h1)} USD\n"
            f"That is {ratio:.1f}x its usual hour\n"
            f"Price now: {fmt_price(price)} USD\n{url}")
    log(f"  *** VOLUME: {title}")
    send_all(title, body)
    state[key] = now
    return True


# ---------------------------------------------------------------------------
# Main pass
# ---------------------------------------------------------------------------
def run_once(alerts, state):
    try:
        pairs = fetch_all_pairs([a["address"] for a in alerts])
    except Exception as e:
        log(f"fetch error: {e}")
        return False

    now = time.time()
    changed = False
    for a in alerts:
        key = alert_key(a)
        pair = best_pair(pairs, a["address"], a["chain"])
        if pair is None:
            log(f"{a['label']}: no {a['chain']} pair found (check address/chain).")
            continue
        price = pair_price(pair)
        if price is None:
            log(f"{a['label']}: no price yet.")
            continue
        url = pair.get("url", "")
        log(f"{a['label']} [{a['mode']}]: {fmt_price(price)} USD")

        if a["mode"] in ("above", "below"):
            changed |= handle_target(a, price, url, state, key)
        elif a["mode"] in ("pump", "dump"):
            changed |= handle_pump(a, price, pair, url, state, key, now)
        elif a["mode"] == "volume":
            changed |= handle_volume(a, price, pair, url, state, key, now)
    return changed


def main():
    if not any_notifier_configured():
        log("ERROR: no notification method configured. Set WHATSAPP_PHONE and "
            "WHATSAPP_APIKEY (or Telegram/ntfy) as environment variables.")
        sys.exit(1)

    alerts = load_watchlist()
    if not alerts:
        log("No valid alerts in watchlist.txt. Nothing to do.")
        return

    state = load_state()
    log(f"Loaded {len(alerts)} alert(s).")

    if "--loop" in sys.argv:
        log(f"Loop mode: checking every {CHECK_INTERVAL}s. Ctrl+C to stop.")
        while True:
            try:
                if run_once(alerts, state):
                    save_state(state)
            except Exception as e:
                log(f"cycle error: {e}")
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
