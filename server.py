"""
server.py — BotCoin Remote Dashboard Server.

Serves the dashboard UI and proxies API calls to the user's bot server.
The proxy allows the browser to call the bot API over HTTPS (same origin)
without mixed content issues, regardless of the bot server's IP.

Security: The proxy only forwards to port 8081 and only passes through
allowed API paths. It holds NO credentials — the password travels in the
request body/headers from the browser, same as before.
"""

import os
import json
import time
import sqlite3
import threading
import requests
from collections import defaultdict
from flask import Flask, send_from_directory, request, Response
from pathlib import Path

app = Flask(__name__, static_folder="static")

# Maps the bot's raw dip_tier1 value (decimal) to the dashboard's display name
# for the 5-detent aggression knob. Keep this in lockstep with the dashboard
# AGGRESSION_PRESETS constant — if they drift, Grok will lie about the user's
# current level.
AGGRESSION_PRESETS = (
    (0.120, "Conservative"),  # T1=12%, T2=22%, T3=35%
    (0.070, "Balanced"),      # T1=7%,  T2=15%, T3=22%
    (0.050, "Moderate"),      # T1=5%,  T2=10%, T3=16%
    (0.030, "Aggressive"),    # T1=3%,  T2=7%,  T3=12%
    (0.015, "Ultra"),         # T1=1.5%, T2=3%, T3=6%
)

def aggression_level_name(dip_tier1: float) -> str:
    """Return the human display name for the current aggression preset, or
    'Custom' if the bot's dip_tier1 doesn't match any preset within 0.001."""
    for value, name in AGGRESSION_PRESETS:
        if abs(dip_tier1 - value) < 0.001:
            return name
    return "Custom"

# ── Community Stats DB ─────────────────────────────────────────────────────
STATS_DB   = Path("/app/data/community_stats.db")
_stats_lock = threading.Lock()

def _init_stats_db():
    STATS_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(STATS_DB) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS active_bots (
                bot_ip      TEXT PRIMARY KEY,
                first_seen  REAL,
                last_seen   REAL,
                trade_count INTEGER DEFAULT 0
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS counters (
                key   TEXT PRIMARY KEY,
                value INTEGER DEFAULT 0
            )
        """)
        # Seed counters if not present
        con.execute("INSERT OR IGNORE INTO counters (key, value) VALUES ('total_installs', 0)")
        con.execute("INSERT OR IGNORE INTO counters (key, value) VALUES ('total_trades', 0)")
        con.commit()

def _record_bot_seen(bot_ip: str, trade_count: int = 0):
    """Record a bot IP as active and update its trade count."""
    now = time.time()
    with _stats_lock:
        with sqlite3.connect(STATS_DB) as con:
            con.execute("""
                INSERT INTO active_bots (bot_ip, first_seen, last_seen, trade_count)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(bot_ip) DO UPDATE SET
                    last_seen   = excluded.last_seen,
                    trade_count = MAX(active_bots.trade_count, excluded.trade_count)
            """, (bot_ip, now, now, trade_count))
            # Update global trade total
            con.execute("""
                UPDATE counters SET value = (
                    SELECT SUM(trade_count) FROM active_bots
                ) WHERE key = 'total_trades'
            """)
            con.commit()

def _increment_installs():
    with _stats_lock:
        with sqlite3.connect(STATS_DB) as con:
            con.execute("UPDATE counters SET value = value + 1 WHERE key = 'total_installs'")
            con.commit()

def _get_community_stats():
    cutoff_30d = time.time() - (30 * 86400)
    with sqlite3.connect(STATS_DB) as con:
        active = con.execute(
            "SELECT COUNT(*) FROM active_bots WHERE last_seen > ?", (cutoff_30d,)
        ).fetchone()[0]
        total_bots = con.execute("SELECT COUNT(*) FROM active_bots").fetchone()[0]
        installs   = con.execute(
            "SELECT value FROM counters WHERE key = 'total_installs'"
        ).fetchone()[0]
        trades     = con.execute(
            "SELECT value FROM counters WHERE key = 'total_trades'"
        ).fetchone()[0]
    return {
        "active_bots":    active,
        "total_bots":     total_bots,
        "total_installs": installs,
        "total_trades":   trades,
    }

_init_stats_db()

# ── Simple rate limiter ────────────────────────────────────────────────────────
# Tracks request counts per IP in a sliding 60-second window.
# Scanners typically fire 50-200 requests/min; legitimate users rarely exceed 30.
_rate_data = defaultdict(list)  # ip -> [timestamps]
_RATE_LIMIT = 60                # max requests per window
_RATE_WINDOW = 60               # seconds
_BLOCKED_IPS = set()            # permanently blocked this session

def _check_rate(ip):
    if ip in _BLOCKED_IPS:
        return False
    now = time.time()
    window_start = now - _RATE_WINDOW
    _rate_data[ip] = [t for t in _rate_data[ip] if t > window_start]
    _rate_data[ip].append(now)
    if len(_rate_data[ip]) > _RATE_LIMIT:
        _BLOCKED_IPS.add(ip)
        app.logger.warning(f"[rate-limit] Blocked {ip} after {len(_rate_data[ip])} req/min")
        return False
    return True

@app.before_request
def rate_limit():
    # Use remote_addr only — CF-Connecting-IP can be spoofed by non-Cloudflare traffic
    # Cloudflare's own IP is what we see as remote_addr, which is trustworthy
    ip = request.remote_addr
    if not _check_rate(ip):
        return Response("Too many requests", status=429)

ALLOWED_PATHS = (
    "/api/health",
    "/api/status",
    "/api/trades",
    "/api/settings",
    "/api/update",
    "/api/version",
    "/api/setup/",
    "/api/open_orders",
    "/api/dca_baseline",
    "/api/deposits",
    "/api/maker_stats",
)


@app.route("/proxy", methods=["GET", "POST", "OPTIONS"])
def proxy():
    """Proxy API calls to the bot server — resolves mixed content for HTTPS dashboard."""
    import re
    bot_ip   = request.args.get("ip", "")
    api_path = request.args.get("path", "")

    if not api_path.startswith("/"):
        api_path = "/" + api_path

    # Security: only allow known API paths
    if not any(api_path.startswith(p) for p in ALLOWED_PATHS):
        return {"error": "Not allowed"}, 403

    # Security: only allow plain IPs, no hostnames
    if not re.match(r'^[\d.]+$', bot_ip):
        return {"error": "Invalid bot IP"}, 400

    # Security: block private/loopback/link-local IPs (SSRF protection)
    import ipaddress
    try:
        parsed_ip = ipaddress.ip_address(bot_ip)
        if parsed_ip.is_private or parsed_ip.is_loopback or parsed_ip.is_link_local or parsed_ip.is_reserved:
            return {"error": "Invalid bot IP"}, 400
    except ValueError:
        return {"error": "Invalid bot IP"}, 400

    target_url = f"http://{bot_ip}:8081{api_path}"

    try:
        resp = requests.request(
            method=request.method,
            url=target_url,
            headers={k: v for k, v in request.headers if k.lower() not in ('host', 'content-length')},
            data=request.get_data(),
            params=request.args,
            timeout=15,
            allow_redirects=False,
        )

        # Silently record bot activity for community stats
        # Extract trade_count from the status response so the ticker stays accurate
        if api_path == "/api/status" and request.method == "GET" and resp.status_code == 200:
            def _record_after_response(ip, body):
                try:
                    data = json.loads(body)
                    tc = data.get("bot", {}).get("trade_count", 0)
                    _record_bot_seen(ip, trade_count=tc)
                except Exception:
                    _record_bot_seen(ip)
            threading.Thread(
                target=_record_after_response,
                args=(bot_ip, resp.content),
                daemon=True
            ).start()

        return Response(
            resp.content,
            status=resp.status_code,
            headers={k: v for k, v in resp.headers.items()
                     if k.lower() not in ('transfer-encoding', 'content-encoding')}
        )
    except requests.exceptions.ConnectionError:
        return {"error": f"Could not reach {bot_ip}:8081 — check the IP and make sure port 8081 is open on your bot server."}, 502
    except requests.exceptions.Timeout:
        return {"error": "Bot server timed out"}, 504
    except Exception as e:
        return {"error": str(e)}, 500


# ── SSH Installer ──────────────────────────────────────────────────────────────

INSTALL_SCRIPT = """
set -e
export DEBIAN_FRONTEND=noninteractive
echo '[1/5] Updating system...'
apt-get update -y -q
echo '[2/5] Installing Docker...'
curl -fsSL https://get.docker.com | sh -s -- -q
echo '[3/5] Installing git...'
apt-get install -y -q git curl
echo '[4/5] Cloning BotCoin...'
rm -rf /root/kraken-btc-bot
git clone -q https://github.com/helprose98/botcoin-bot.git /root/kraken-btc-bot
cd /root/kraken-btc-bot
touch .env
mkdir -p data logs
echo '[5/5] Starting containers...'
docker compose up -d --build
bash install-update-watcher.sh
echo 'BOTCOIN_INSTALL_COMPLETE'
"""

# In-memory job store: job_id -> {lines: [], done: bool, error: str|None}
_install_jobs = {}
_install_lock = __import__('threading').Lock()


@app.route("/install/start", methods=["POST"])
def install_start():
    """Start an install job. Returns a job_id immediately."""
    import re, uuid, threading, paramiko

    body     = request.get_json(force=True, silent=True) or {}
    ip       = body.get("ip", "").strip()
    password = body.get("password", "").strip()

    if not ip or not re.match(r'^[\d.]+$', ip):
        return {"ok": False, "error": "Invalid IP"}, 400
    if not password:
        return {"ok": False, "error": "Password required"}, 400

    job_id = str(uuid.uuid4())[:8]
    with _install_lock:
        _install_jobs[job_id] = {"lines": [], "done": False, "error": None}

    def run():
        def log(msg, kind="log"):
            with _install_lock:
                _install_jobs[job_id]["lines"].append({"kind": kind, "msg": msg})

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            log(f"Connecting to {ip}...", "status")
            client.connect(ip, username="root", password=password, timeout=15)
            log("Connected. Starting installation...", "status")

            transport = client.get_transport()
            channel   = transport.open_session()
            channel.get_pty()
            channel.exec_command(f"bash << 'ENDBASH'\n{INSTALL_SCRIPT}\nENDBASH")

            buf = ""
            while True:
                if channel.recv_ready():
                    chunk = channel.recv(4096).decode("utf-8", errors="replace")
                    buf += chunk
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if line:
                            if "BOTCOIN_INSTALL_COMPLETE" in line:
                                log("BotCoin installed successfully!", "done")
                                _increment_installs()
                            else:
                                log(line)
                elif channel.exit_status_ready():
                    code = channel.recv_exit_status()
                    if code != 0:
                        log(f"Install failed (exit code {code})", "error")
                    break
                else:
                    __import__('time').sleep(0.1)
            client.close()
        except paramiko.AuthenticationException:
            log("Authentication failed — check your root password", "error")
        except Exception as e:
            log(str(e), "error")
        finally:
            with _install_lock:
                _install_jobs[job_id]["done"] = True

    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "job_id": job_id}


@app.route("/install/status", methods=["GET"])
def install_status():
    """Poll for install progress. Returns new lines since last_index."""
    job_id     = request.args.get("job", "")
    last_index = int(request.args.get("from", 0))

    with _install_lock:
        job = _install_jobs.get(job_id)
    if not job:
        return {"ok": False, "error": "Job not found"}, 404

    new_lines = job["lines"][last_index:]
    return {
        "ok":    True,
        "lines": new_lines,
        "total": len(job["lines"]),
        "done":  job["done"],
    }


# ── Dash version + self-update ──────────────────────────────────────────────

DASH_VERSION_PATH = Path("/app/VERSION")
DASH_GITHUB_RAW   = "https://raw.githubusercontent.com/helprose98/botcoin-dash/main/VERSION"


@app.route("/dash/version")
def dash_version():
    """Returns current dash version and checks GitHub for updates."""
    try:
        current = DASH_VERSION_PATH.read_text().strip()
    except Exception:
        current = "unknown"
    try:
        resp = requests.get(DASH_GITHUB_RAW, timeout=5)
        latest = resp.text.strip()
    except Exception:
        latest = current
    def ver_gt(a, b):
        try: return tuple(int(x) for x in a.split(".")) > tuple(int(x) for x in b.split("."))
        except: return False
    return {"current": current, "latest": latest, "update_available": ver_gt(latest, current)}


@app.route("/dash/maker_stats")
def dash_maker_stats():
    """Proxy the bot's /api/maker_stats with graceful backward compatibility.

    The maker-stats endpoint was introduced in earlier bot versions and is now
    standard. When the dashboard is pointed at a bot that doesn't expose it (very
    old install), the bot returns 404 — this route then returns {"available": false}
    so the "Fees Saved" widget hides cleanly with no console error. Mirrors the IP
    validation + SSRF guards used by /proxy; auth rides on X-Bot-Password.
    """
    import re
    import ipaddress
    bot_ip = request.args.get("ip", "")

    # Same validation as /proxy: plain IPs only, no private/loopback/link-local.
    if not re.match(r'^[\d.]+$', bot_ip):
        return {"available": False}
    try:
        parsed_ip = ipaddress.ip_address(bot_ip)
        if parsed_ip.is_private or parsed_ip.is_loopback or parsed_ip.is_link_local or parsed_ip.is_reserved:
            return {"available": False}
    except ValueError:
        return {"available": False}

    headers = {}
    pw = request.headers.get("X-Bot-Password")
    if pw:
        headers["X-Bot-Password"] = pw

    try:
        resp = requests.get(f"http://{bot_ip}:8081/api/maker_stats",
                            headers=headers, timeout=15)
    except requests.exceptions.RequestException:
        # Bot unreachable / timed out — treat as unavailable, hide the widget.
        return {"available": False}

    # Older bot without the endpoint → 404. Hide the widget, no error surfaced.
    if resp.status_code == 404:
        return {"available": False}
    if resp.status_code != 200:
        return {"available": False}

    return Response(
        resp.content,
        status=200,
        headers={k: v for k, v in resp.headers.items()
                 if k.lower() not in ('transfer-encoding', 'content-encoding')}
    )


@app.route("/dash/update", methods=["POST"])
def dash_update():
    """Trigger a self-update of the dash server via the update watcher.
    Requires a non-empty secret token in the request body to prevent
    unauthenticated abuse — the dashboard JS sends the bot password.
    """
    body   = request.get_json(force=True, silent=True) or {}
    secret = body.get("secret", "").strip()
    if not secret or len(secret) < 6:
        return {"ok": False, "error": "Unauthorized"}, 401

    trigger = Path("/app/data/update.trigger")
    try:
        trigger.write_text("update")
        return {"ok": True, "message": "Dash update started. Page will reload in ~2 minutes."}
    except Exception as e:
        return {"ok": False, "error": "Update failed"}, 500


# ── Community Stats ──────────────────────────────────────────────────────

@app.route("/api/community-stats")
def community_stats():
    """Public endpoint — returns aggregate community stats, no personal data."""
    try:
        stats = _get_community_stats()
        return Response(
            json.dumps(stats),
            mimetype="application/json",
            headers={"Cache-Control": "public, max-age=60"}
        )
    except Exception as e:
        return {"active_bots": 0, "total_bots": 0, "total_installs": 0, "total_trades": 0}


# ── Ask BotCoin AI Chat ────────────────────────────────────────────────────────

@app.route("/chat", methods=["POST"])
def chat():
    """Stream an AI response using live bot data as context."""
    body       = request.get_json(force=True, silent=True) or {}
    question   = body.get("question", "").strip()
    bot_ip     = body.get("bot_ip", "").strip()
    password   = body.get("password", "").strip()

    if not question:
        return {"error": "No question provided"}, 400

    api_key = os.environ.get("GROK_API_KEY", "")
    if not api_key:
        return {"error": "Grok API key not configured"}, 500

    # ── Fetch live bot data for context ──────────────────────────────────────
    bot_context = ""
    if bot_ip and password:
        try:
            import re
            if re.match(r'^[\d.]+$', bot_ip):
                headers = {"X-Bot-Password": password}
                status_r = requests.get(f"http://{bot_ip}:8081/api/status",
                                        headers=headers, timeout=5)
                trades_r = requests.get(f"http://{bot_ip}:8081/api/trades",
                                        headers=headers, timeout=5)
                if status_r.ok:
                    s     = status_r.json()
                    port  = s.get("portfolio", {})
                    bot   = s.get("bot", {})
                    mood  = s.get("mood", {})
                    btc   = port.get("btc_balance", "unknown")
                    usd   = port.get("usd_balance", "unknown")
                    price = port.get("current_price", "unknown")
                    basis = port.get("avg_cost_basis", "unknown")
                    pnl   = port.get("pnl_pct", "unknown")
                    worth = port.get("portfolio_value", "unknown")
                    operating_regime  = bot.get("operating_regime", {}) or {}
                    op_regime_name    = operating_regime.get("operating_regime", "unknown")  # accumulate|neutral|harvest
                    ma_ratio          = operating_regime.get("ma_ratio")
                    ma_available      = operating_regime.get("ma_available", True)
                    harvest_state     = bot.get("harvest_state", {}) or {}
                    harvest_active    = harvest_state.get("active", False)
                    trades_count = bot.get("trade_count", "unknown")
                    ma200 = bot.get("ma200", None)
                    mood_label = mood.get("label", "unknown") if isinstance(mood, dict) else "unknown"
                    mood_detail = mood.get("detail", "") if isinstance(mood, dict) else ""
                    next_dca = s.get("next_dca", "unknown")

                    # Can it trade? USD must be > $5 min order
                    can_trade = "yes" if isinstance(usd, (int, float)) and usd >= 5 else "no (insufficient USD reserve — needs at least $5)"

                    bot_context += f"""
Live account data:
- BTC stack: {btc} BTC (worth ~${worth} USD at current price)
- USD reserve: ${usd} — can the bot make a trade right now? {can_trade}
- Current BTC price: ${price}
- Average cost basis: ${basis} per BTC
- P&L vs cost basis: {pnl}%
- Operating regime: {op_regime_name}  (v2 classification — accumulate/neutral/harvest; price vs 200MA ratio: {ma_ratio if ma_available else 'building'})
- Harvest rally active: {'yes — currently selling slices into a confirmed rally' if harvest_active else 'no'}
- Bot mood: {mood_label} — {mood_detail}
- Total trades executed: {trades_count}
- 200-day moving average: ${ma200 if ma200 else 'still building (needs 200 days of price data)'}
- Next scheduled DCA: {next_dca}"""

                    # Growth card numerics. Mirrors what the v2.2 "How you're doing" card computes
                    # so Grok can speak fluently about what the user sees: stack now vs deposits,
                    # BTC price appreciation (mark-to-market), and (when deposits are present)
                    # total USD growth vs total invested.
                    try:
                        if isinstance(price, (int, float)) and isinstance(basis, (int, float)) \
                           and isinstance(btc, (int, float)) and isinstance(worth, (int, float)):
                            appreciation = (price - basis) * btc
                            # Total growth = portfolio_today - total_net_deposits. We don't have
                            # deposit data here without an extra round-trip; the dashboard pulls
                            # /api/deposits separately. For now, fetch it and only compute
                            # bot_earnings if it returns; otherwise skip the bot_earnings line.
                            deposits_r = requests.get(f"http://{bot_ip}:8081/api/deposits",
                                                      headers=headers, timeout=5)
                            bot_earnings = None
                            deposit_days = None
                            if deposits_r.ok:
                                dep_payload = deposits_r.json() or {}
                                dep_list = dep_payload.get("deposits") or []
                                total_invested = sum(float(d.get("usd_value_at_time", 0) or 0) for d in dep_list)
                                if total_invested > 0:
                                    total_growth = worth - total_invested
                                    bot_earnings = total_growth - appreciation
                                # Days since earliest deposit (used for context on whether APY is ready)
                                from datetime import datetime, timezone
                                timestamps = []
                                for d in dep_list:
                                    ts = d.get("timestamp")
                                    if not ts:
                                        continue
                                    try:
                                        timestamps.append(datetime.fromisoformat(ts.replace("Z", "+00:00")))
                                    except Exception:
                                        pass
                                if timestamps:
                                    earliest = min(timestamps)
                                    if earliest.tzinfo is None:
                                        earliest = earliest.replace(tzinfo=timezone.utc)
                                    deposit_days = int((datetime.now(timezone.utc) - earliest).days)

                            bot_context += f"\n- Growth card · BTC Price Appreciation: ${appreciation:,.2f}  (mark-to-market on existing stack)"
                            if bot_earnings is not None:
                                bot_context += f"\n- Growth card · Bot Trading Earnings: ${bot_earnings:,.2f}  (everything the bot did that moved value)"
                            if deposit_days is not None:
                                bot_context += f"\n- Deposit history: {deposit_days} days since first deposit  (APY display unlocks at 90 days)"
                    except Exception:
                        pass  # best-effort context; never break Grok over a math error

                # Also fetch settings for DCA amount/frequency context
                settings_r = requests.get(f"http://{bot_ip}:8081/api/settings",
                                          headers=headers, timeout=5)
                if settings_r.ok:
                    cfg = settings_r.json()
                    dip1 = float(cfg.get('dip_tier1', 0.015)) * 100
                    dip2 = float(cfg.get('dip_tier2', 0.030)) * 100
                    dip3 = float(cfg.get('dip_tier3', 0.060)) * 100
                    recycler_sell = float(cfg.get('recycler_sell_threshold', 0.03)) * 100
                    recycler_pool = float(cfg.get('recycler_pool_percent', 0.55)) * 100
                    max_order = cfg.get('max_order_usd', '2000')
                    bot_context += f"""
- Configured mode: {cfg.get('mode','?')}
- Aggression level: {aggression_level_name(float(cfg.get('dip_tier1', 0.015)))}  (knob position on dashboard)
- DCA amount: ${cfg.get('dca_amount','?')} per {cfg.get('dca_frequency','?')}
- DCA time: {cfg.get('dca_time_utc','?')} UTC
- Dip buy thresholds: T1={dip1:.1f}%, T2={dip2:.1f}%, T3={dip3:.1f}%
- Recycler sell threshold: {recycler_sell:.1f}% above cost basis
- Recycler pool: {recycler_pool:.0f}% of USD reserve reserved for recycler
- Max single order: ${max_order}"""

                # Universal Recycler — always-on in v2. Surface open positions so Grok can
                # describe what cycles are currently in-flight.
                recycler_positions = bot.get("recycler_positions") if isinstance(bot, dict) else None
                if isinstance(recycler_positions, list):
                    open_count = len(recycler_positions)
                    if open_count:
                        bot_context += f"\n- Universal Recycler: {open_count} open position(s) waiting to close (sell-high-rebuy-low cycles in flight)"
                    else:
                        bot_context += "\n- Universal Recycler: no open positions right now (always-on; will fire when volatility presents a cycle)"

                # ── Tier 1 trade-quality engines — vol-adaptive thresholds, anti-thrash, maker-only. ──
                # All optional / best-effort; older bots that don't emit these fields just skip the line.
                # Volatility-adaptive thresholds: surface the regime + multiplier so
                # Grok can explain why thresholds are tighter/looser right now.
                volatility = bot.get("volatility") if isinstance(bot, dict) else None
                if isinstance(volatility, dict) and volatility.get("multiplier") is not None:
                    bot_context += (
                        f"\n- Volatility regime: {volatility.get('regime', 'normal')} "
                        f"(multiplier {volatility.get('multiplier')}× · "
                        f"14d ATR {volatility.get('atr_pct', '?')} vs baseline {volatility.get('baseline_pct', '?')})"
                    )

                # Anti-thrash throttle: cooldown + daily cap usage.
                throttle = bot.get("throttle") if isinstance(bot, dict) else None
                if isinstance(throttle, dict) and throttle.get("trades_today") is not None:
                    cooldown = throttle.get("seconds_until_next_allowed", 0) or 0
                    cooldown_note = (f"{int(cooldown)}s until next trade allowed"
                                     if cooldown > 0 else "no cooldown active")
                    bot_context += (
                        f"\n- Anti-thrash guard: {throttle.get('trades_today')}/"
                        f"{throttle.get('max_per_day', '?')} trades today, "
                        f"min-gap {throttle.get('min_gap_seconds', '?')}s, {cooldown_note} "
                        f"(Recycler cycle-closing trades bypass the guard)"
                    )

                # Maker stats: monthly fee savings + maker fill rate. New /api/maker_stats
                # endpoint; older bots 404 — best-effort, skip silently on any failure.
                try:
                    maker_r = requests.get(f"http://{bot_ip}:8081/api/maker_stats",
                                           headers=headers, timeout=5)
                    if maker_r.ok:
                        ms = maker_r.json() or {}
                        if ms.get("maker_fill_rate") is not None:
                            bot_context += (
                                f"\n- Maker stats ({ms.get('month', 'this month')}): "
                                f"saved ${ms.get('fees_saved_usd', 0)} on fees "
                                f"(paid ${ms.get('fees_paid_usd', 0)} vs "
                                f"${ms.get('fees_taker_baseline_usd', 0)} taker baseline), "
                                f"{round((ms.get('maker_fill_rate') or 0) * 100)}% maker fill rate "
                                f"across {ms.get('trades_closed', '?')} closed trades"
                            )
                except Exception:
                    pass  # maker_stats is best-effort context

                if trades_r.ok:
                    trades_list = trades_r.json()[:10] if isinstance(trades_r.json(), list) else []
                    if trades_list:
                        bot_context += "\n- Recent trades (last 10):\n"
                        bot_context += "\n".join(
                            f"  {t.get('side','?').upper()} {t.get('btc_amount','?')} BTC @ ${t.get('price_usd','?')} | ${t.get('usd_amount','?')} | {t.get('reason','?')} | {t.get('timestamp','?')[:16]}"
                            for t in trades_list
                        )

                # Days of bot-managed trading history. Lets Grok caveat statistics correctly
                # ("only 27 days of data — patterns aren't statistically meaningful yet").
                try:
                    if trades_r.ok:
                        all_trades = trades_r.json() if isinstance(trades_r.json(), list) else []
                        if all_trades:
                            from datetime import datetime, timezone
                            ts_strs = [t.get("timestamp") for t in all_trades if t.get("timestamp")]
                            parsed = []
                            for ts in ts_strs:
                                try:
                                    parsed.append(datetime.fromisoformat(ts.replace("Z", "+00:00")))
                                except Exception:
                                    pass
                            if parsed:
                                earliest = min(parsed)
                                if earliest.tzinfo is None:
                                    earliest = earliest.replace(tzinfo=timezone.utc)
                                trade_days = int((datetime.now(timezone.utc) - earliest).days)
                                bot_context += f"\n- Bot trading history: {trade_days} days since first bot-managed trade  ({len(all_trades)} trades total)"
                except Exception:
                    pass

                # Versions block — anchors Grok to "what's actually on screen" so it doesn't
                # describe stale UI layouts.
                dash_version_str = "unknown"
                try:
                    with open(os.path.join(os.path.dirname(__file__), "VERSION")) as vf:
                        dash_version_str = vf.read().strip()
                except Exception:
                    pass
                bot_version_str = s.get("version") or s.get("bot", {}).get("version") or "unknown"
                bot_context = f"Dashboard version: v{dash_version_str}\nBot version: v{bot_version_str}\n" + bot_context
        except Exception:
            pass  # context is best-effort; answer without it if bot unreachable

    system_prompt = f"""You are the myBotCoin Assistant — a sharp, opinionated, plain-talking guide built directly into the myBotCoin dashboard. You were created by the same team that built this bot. You know this system inside and out.

═══════════════════════════════════════════
PRIME DIRECTIVE
═══════════════════════════════════════════
The single mission: **stack sats for a future where BTC is worth 8-digits.** This is a 20-year, generational-wealth play — the user is building a financial asset to hand off to his kids. We measure success in BTC (satoshis), never in USD. A lower BTC price is good news — it means more sats per dollar. Every regime, every trade serves the prime directive. The bot is BTC-maximalist by design.

═══════════════════════════════════════════
WHAT myBotCoin IS
═══════════════════════════════════════════
myBotCoin is a self-hosted Bitcoin savings bot. It runs 24/7 on a private cloud server (Vultr) and trades automatically on the Kraken exchange. The user owns and controls everything — their server, their Kraken account, their keys. There is no middleman.

═══════════════════════════════════════════
PHILOSOPHY
═══════════════════════════════════════════
Bitcoin operates on roughly 4-year halving cycles: accumulation → bull run → correction → repeat. Long-term holders who consistently buy through bear markets — especially at prices that felt terrifying — end up with the most BTC.

The enemy of wealth building is emotion. People sell at bottoms and buy at tops. myBotCoin removes emotion entirely: it accumulates in cheap zones, harvests into confirmed rallies, and recycles volatility for extra sats year-round.

Key mindset:
- A dip is a discount, not a disaster.
- Consistency beats timing. Nobody calls the bottom.
- The 200-day moving average is the trend filter that defines the regime.
- Short-term USD value of the stack is a vanity metric. BTC quantity is what matters long-term.
- The bot's small fees on profitable cycles are a cost of accumulation, not a leak.

═══════════════════════════════════════════
HOW THE BOT WORKS — v2 ENGINE
═══════════════════════════════════════════

**THREE OPERATING REGIMES (auto-selected from price vs 200MA):**

- **Accumulate** — price is below the 200MA (especially deeper drawdowns). This is the cheap zone; DCA fires on schedule and dip-buys fire on drops. New USD is deployed into BTC.
- **Neutral** — price is hovering near the 200MA. No aggressive accumulation, no aggressive harvest. The Universal Recycler is the primary trader here.
- **Harvest** — price has cleared the 200MA × 1.15 threshold (real rally territory). The ONLY meaningful stack-reduction mechanism. Sells slices into the rally to grow USD dry-powder for the next dip. There's a hard cap on how much of the stack can be sold per rally.

Regime detection is **event-driven** — no multi-check hysteresis. Breakouts and crashes get a snap response. The dashboard's Market Position gauge shows where we are relative to the 200MA.

**MA-200 BUILDING STATE:** A freshly-installed bot may not yet have 200 days of its own price history. While it builds, the gauge falls back to a Kraken-derived 200MA and shows a small "Using historical 200MA data — updates live as your bot builds its own (X/200 days)" notice. This is expected; no action needed.

**DCA (Dollar Cost Averaging):**
A fixed USD amount bought on a fixed schedule, regardless of price — the baseline accumulation engine. Active in the Accumulate regime. **DCA only fires if the bot has dollars to spend.**

**DIP BUYING:**
Active in the Accumulate regime. The bot monitors drop from the 7-day high. Three escalating tiers (T1/T2/T3) fire at progressively bigger drops, deploying progressively more USD. Thresholds depend on the user's aggression level. Cooldown between dip buys.

**AGGRESSION LEVELS (5 detents — match the dashboard knob exactly):**
- **Conservative** 🐢 — T1=12%, T2=22%, T3=35%. Waits for major dips.
- **Balanced** ⚖️ — T1=7%, T2=15%, T3=22%. Sensible middle ground.
- **Moderate** 📈 — T1=5%, T2=10%, T3=16%. Tighter triggers, larger deployments.
- **Aggressive** 🚀 — T1=3%, T2=7%, T3=12%. Deploys on almost every move. (Current setting as of v2 launch.)
- **Ultra** ⚡ — T1=1.5%, T2=3%, T3=6%. Harvests small oscillations. Best in choppy markets; over-trades in strong trends.

The bot stores three raw `dip_tier1/2/3` decimals — it has no concept of a named "level." The dashboard maps those decimals to a name. If the values don't match any preset, the dashboard shows "Custom" and so should you.

**THE UNIVERSAL RECYCLER (always-on; cycle-based; the v2 successor to the v1 Sideways/USD Recycler split):**

The Recycler is the bot's volatility-harvest engine. It runs continuously in **all three regimes**. Replaces the v1 "Sideways Market" overlay and the binary BTC-Recycler / USD-Recycler split. Same mechanism, single direction:

- **Opening leg:** `spike_sell` — sells a slice when an open position rises +N% above its buy price.
- **Closing leg:** `recycler_rebuy` — rebuys lower; the BTC quantity recovered exceeds what was sold.
- **Net result:** same USD invested, MORE BTC banked.

**Never describe a Recycler trade as a one-sided action** — the other leg is either already done or coming next. A `spike_sell` without a matching `recycler_rebuy` means the cycle is OPEN; a `recycler_rebuy` means the cycle just CLOSED and extra sats were banked.

Reading recent trades:
- `spike_sell` alone → Recycler cycle OPEN; `recycler_rebuy` expected next.
- `recycler_rebuy` → Recycler cycle CLOSED; net more BTC in stack.
- `dca` / dip-buy → stack-adding (only in Accumulate regime).
- Harvest sells → stack-reducing (only in Harvest regime, only above 200MA × 1.15).

**Historical (v1) trade reasons — display-only:**
The trade history may contain rows from before the v2 cutover with these reasons. They will NEVER be emitted again by the live bot. If asked about one, describe it accurately as the v1-era behavior:
- `usd_spike_sell_tier1/2/3` and `usd_dca_sell` — v1 USD-mode stack-shrinking sells (replaced by Harvest in v2)
- `usd_recycler_buy` / `usd_recycler_resell` — v1 USD-mode round-trip legs (replaced by the Universal Recycler in v2)
- `range_recycler_buy` / `range_recycler_sell` — v1 Sideways Market overlay (replaced by the Universal Recycler in v2)
- `quick_buy` — manual-buy from the v1 dashboard (the button and endpoint were removed)

═══════════════════════════════════════════
TIER 1 TRADE-QUALITY ENGINES (always-on, behind-the-scenes)
═══════════════════════════════════════════

The bot runs three trade-quality engines underneath everything above. They don't change strategy — they make every trade cleaner. The dashboard intentionally does not surface them on screen (Andy's call: simple UI, smart bot). When asked, describe what the bot is doing using the live data below — don't tell the user to look for a card or badge.

**MAKER-ONLY ORDERS.**
Every order is placed as a post-only limit order that rests on the order book instead of crossing the spread. Resting fills pay Kraken's maker fee (0.16%) instead of the taker fee (0.26%) — roughly a 38% cut on trading costs. On hundreds of trades this compounds into measurable extra BTC, which directly serves the prime directive. Trade-off: some orders won't fill immediately; the bot re-evaluates on the next tick. We accept a few missed fills in exchange for cheaper ones.

**VOLATILITY-ADAPTIVE THRESHOLDS.**
Dip and spike thresholds adapt to 14-day ATR vs a 90-day baseline:
- **Calm market** → thresholds tighten; the bot reacts to smaller moves that are proportionally meaningful when the tape is quiet.
- **Stormy market** → thresholds loosen; bigger move required before triggering, filtering noise and avoiding falling-knife buys.

This is adaptive sensitivity, not a change in deployment size. The multiplier is clamped to ~0.7×–1.5× and degrades to 1.0× (no adjustment) if the calc ever fails. A "storm" reading is not a warning — it just means the bot is being more patient.

**ANTI-THRASH GUARD.**
A global dampener that prevents death-by-fees in choppy markets. Two limits sit above the per-strategy cooldowns:
- Minimum gap between trades — global cooldown so two trades can't fire back-to-back (default 1 hour).
- Maximum trades per day — hard daily cap across all strategies (default 8, resets at UTC midnight).

**Important reassurance:** cycle-closing trades (Recycler rebuy / Harvest follow-throughs) bypass the guard, so an open cycle always gets to finish. Only new, stack-opening activity is throttled.

═══════════════════════════════════════════
WHAT THE USER ACTUALLY SEES — DASHBOARD TOUR
═══════════════════════════════════════════

The dashboard is a single full-width page, top-to-bottom. Version numbers (bot vX.Y.Z · dash vX.Y.Z) appear in the header chip and footer — they update over time, so describe what's on screen rather than memorizing a version string.

1. **Header bar** — myBotCoin logo, current version chip.
2. **Portfolio card** — total portfolio value, BTC stack (e.g. "0.05000000 BTC"), USD reserve, current BTC price, average cost basis. All in one card.
3. **Market Position gauge** — needle showing price relative to the 200MA. Bear / Neutral / Bull zones. Shows the building-state notice when the bot is still accumulating its own 200 days of price history.
4. **How you're doing card** — the BTC-first growth view. Layout:
   - **Hero:** signed net stack delta — current stack vs the BTC you'd have if no cycle activity had ever run (BTC deposits + DCA-bought BTC). Format: `±X.XXXXXXXX BTC`, green or red. Negative right now because v1's USD-mode drained the stack before the v2 cutover.
   - **Hero sub-line:** the same delta translated to USD at the current price (`≈ ±$X,XXX at current price`).
   - **USD view sub-line:** `USD view: +$XXX (+X.X% vs total invested)` — kept honest but demoted. USD is not the headline anymore.
   - **APY line:** "X.X% APY" once the deposit ledger has converged; until then shows a status state (syncing / not enough history / etc.). This is intentional.
   - **Breakdown rows** (current vs started):
     - **Stack:** `X.XXXXXXXX BTC` · `started: X.XXXXXXXX BTC`
     - **USD reserve:** `$X,XXX` · `started: $X,XXX`
     - **Bot cycle wins:** cumulative net BTC moved by completed Recycler/Harvest cycles. Signed — orange when positive, red when negative, dash when zero. NOT the change in the stack; just what cycles have netted.
     - **BTC price move:** signed mark-to-market on the current stack from cost basis to spot. Green/red.
5. **Aggression knob (5-detent twirldown)** — full-width control with all 5 detents visible and short layman captions under each.
6. **Deposit acceleration drawer** — DCA amount, frequency, time. Note: DCA only fires when there's USD to spend.
7. **Recent trades table** — last few trades with price, reason badge, and a "view all →" link.
8. **Community stats** — total trades across all installs, number of bots installed.
9. **Footer** — current bot + dash version, last-updated time, "Update Bot" and "Update Dash" buttons.
10. **Grok chat (you)** — embedded chat panel with ask-chips for common questions.

**Terms to USE:**
- "Stack" / "BTC stack" — the user's BTC balance.
- "How you're doing" — the growth card. The hero number is the **net stack delta**, not USD growth.
- "Bot cycle wins" — cumulative cycle delta. Honest about being signed; can be negative.
- "BTC price move" — the mark-to-market row in the breakdown.
- "Operating regime" — Accumulate / Neutral / Harvest.
- "The Recycler" / "Universal Recycler" — never "Range Mode," never "Sideways Recycler," never "BTC Recycler vs USD Recycler" (those splits are gone).
- "Cost basis" / "Average cost basis" — the bot's average buy price.

**Terms that are GONE in v2 (don't volunteer them):**
- "BTC Mode" / "USD Mode" / "Auto" — replaced by the three regimes (Accumulate / Neutral / Harvest).
- "Sideways Market" / "Range Recycler" — replaced by the always-on Universal Recycler. If the user asks why their expert drawer still shows a "Sideways Market" section, explain it's a deprecated relic from v1 that's slated for removal — the controls don't drive any live behavior.
- "USD Recycler" / "BTC Recycler" — collapsed into the single Universal Recycler.
- "Paper trading" — removed. The bot is real-execution only.
- "House Money" / "Winnings" / "Bear Market Shield" / "Road to Break Even" / "Bot vs DCA comparison" — old breakdown rows and banners; not in the v2 dashboard.

═══════════════════════════════════════════
HOW TO ANSWER USERS
═══════════════════════════════════════════
- Be direct. Real answers, no hedging.
- Use the user's actual live data (see LIVE USER DATA below). Reference their specific numbers.
- Frame answers through the prime directive when it adds clarity: "stack more BTC long-term," "build the asset for the kids."
- Format with **bold**, bullet points, short sections. Keep it readable.
- 3-6 sentences or a short bullet list is ideal. Don't write essays.
- If asked about a trade in their history — look at Recent trades and explain exactly what happened (including v1-era reasons if the trade pre-dates the cutover).
- If asked "should I be more aggressive?" — explain what each level does given current USD balance and regime. Use the correct level NAMES.
- Explaining the bot's mechanics is NOT financial advice. Don't say "consult a financial advisor" for product questions.
- If you genuinely don't know, say so plainly.
- Tone: smart, direct, like a knowledgeable friend who actually understands crypto and this bot. Not a corporate chatbot.

═══════════════════════════════════════════
LIVE USER DATA
═══════════════════════════════════════════
{bot_context if bot_context else "(Live bot data unavailable — answer based on general myBotCoin context.)"}
"""

    # ── Call Grok (non-streaming — Cloudflare buffers SSE) ──────────────────
    try:
        resp = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            },
            json={
                "model":       "grok-3-mini",
                "stream":      False,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": question},
                ],
                "max_tokens":  400,
                "temperature": 0.7,
            },
            timeout=30,
        )
        data = resp.json()
        answer = data["choices"][0]["message"]["content"]
        return {"ok": True, "answer": answer}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


@app.route("/")
def index():
    # v2 is the only dashboard now (static/index.html). The /v1 route below is retained
    # as an emergency fallback only.
    return send_from_directory("static", "index.html")

@app.route("/v1")
def index_v1():
    # Legacy dashboard, preserved for users who prefer it or for debugging.
    return send_from_directory("static", "v1.html")

@app.route("/v2")
def index_v2():
    # Transitional redirect: v2 is now served at /, so keep old /v2 bookmarks working.
    return Response(status=301, headers={"Location": "/"})

@app.route("/about")
def about_page():
    return send_from_directory("static", "about.html")

@app.route("/setup-guide")
def setup_guide_page():
    return send_from_directory("static", "setup-guide.html")


@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
