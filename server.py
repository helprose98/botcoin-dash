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
    "/api/buy",
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

    The maker-stats endpoint is new in bot v1.5.0. When the dashboard is pointed
    at an older bot that doesn't have it, the bot returns 404 — in that case (and
    on any connection error) this route returns {"available": false} so the
    "Fees Saved" widget hides cleanly with no console error. Mirrors the IP
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
                    mode  = bot.get("active_mode", "unknown")
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
- Bot active mode: {mode}
- Bot mood: {mood_label} — {mood_detail}
- Total trades executed: {trades_count}
- 200-day moving average: ${ma200 if ma200 else 'still building (needs 200 days of price data)'}
- Next scheduled DCA: {next_dca}"""

                    # Growth card figures (mirror of the dashboard 2-bucket model in v1.12.7+).
                    # These let Grok speak fluently about what the user sees in the GROWTH card.
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
                    paper = cfg.get('paper_trading', 'false')
                    bot_context += f"""
- Configured mode: {cfg.get('mode','?')}
- Aggression level: {aggression_level_name(float(cfg.get('dip_tier1', 0.015)))}  (knob position on dashboard)
- DCA amount: ${cfg.get('dca_amount','?')} per {cfg.get('dca_frequency','?')}
- DCA time: {cfg.get('dca_time_utc','?')} UTC
- Dip buy thresholds: T1={dip1:.1f}%, T2={dip2:.1f}%, T3={dip3:.1f}%
- Recycler sell threshold: {recycler_sell:.1f}% above cost basis
- Recycler pool: {recycler_pool:.0f}% of USD reserve reserved for recycler
- Max single order: ${max_order}
- Paper trading: {paper}"""

                # Sideways Market data
                sideways = bot.get("sideways", {})
                if sideways.get("active"):
                    bot_context += f"""
- Sideways Market: ACTIVE (14d range: {sideways.get('range_pct', '?')}%, threshold: {sideways.get('threshold_pct', 12)}%)
- Range Recycler positions: {sideways.get('positions', 0)}/{sideways.get('max_positions', 5)}
- Range Recycler thresholds: buy at {sideways.get('buy_threshold_pct', -4)}%, sell at +{sideways.get('sell_threshold_pct', 6)}%"""
                elif sideways:
                    bot_context += f"\n- Sideways Market: inactive (14d range: {sideways.get('range_pct', '?')}%)"

                # ── Tier 1 (bot v1.5.0) live blocks — all optional / best-effort ──
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
The single mission: end up with MORE BTC over the long run. We measure success in BTC (satoshis), never in USD. A lower BTC price is not bad news — it means more sats per dollar. Every strategy, every mode, every trade serves the prime directive — including modes where the bot temporarily focuses on USD. USD grown in those modes is dry powder for future BTC purchases when the trend turns. The bot is BTC-maximalist by design.

═══════════════════════════════════════════
WHAT myBotCoin IS
═══════════════════════════════════════════
myBotCoin is a self-hosted Bitcoin savings bot. It runs 24/7 on a private cloud server (Vultr) and trades automatically on the Kraken exchange. The user owns and controls everything — their server, their Kraken account, their keys. There is no middleman.

═══════════════════════════════════════════
PHILOSOPHY
═══════════════════════════════════════════
Bitcoin operates on roughly 4-year halving cycles: accumulation → bull run → correction → repeat. Long-term holders who consistently buy through bear markets — especially at prices that felt terrifying — end up with the most BTC.

The enemy of wealth building is emotion. People sell at bottoms and buy at tops. myBotCoin removes emotion entirely: it buys on a schedule, buys harder on dips, doesn't panic, doesn't get greedy. It just stacks.

Key mindset:
- A dip is a discount, not a disaster.
- Consistency beats timing. Nobody calls the bottom.
- The 200-day moving average is the best single trend filter. Above = bull. Below = bear. The bot trades differently in each.
- Short-term USD value of the stack is a vanity metric. BTC quantity is what matters long-term.
- The bot's small fees on profitable trades are a cost of accumulation, not a leak.

═══════════════════════════════════════════
HOW THE BOT WORKS — MECHANICS
═══════════════════════════════════════════

**MODES (auto-managed via the 200MA trend filter):**

- **BTC Accumulation Mode** — active when price is above the 200MA. DCA fires on schedule. Dip-buy tiers fire on drops. The BTC Recycler harvests volatility for extra BTC.
- **USD Accumulation Mode** — active when price is below the 200MA. DCA and dip-buys halt — the bot does not "catch falling knives" during sustained downtrends. The USD Recycler harvests volatility for extra USD (dry powder for future BTC buys).
- **Auto** — the bot reads the 200MA and selects the mode itself. 7-day minimum hold prevents whipsawing. Recommended.

The 200MA is a **trend filter**, not a valuation gauge. Below it, the bot defends capital. Above it, the bot deploys aggressively.

**MA-200 BUILDING STATE:** A freshly-installed bot may not yet have 200 days of its own price history. While it builds, the dashboard's Market Position gauge falls back to a Kraken-derived 200MA and shows a small "Using historical 200MA data — updates live as your bot builds its own (X/200 days)" notice. This is a graceful, expected state; no action needed.

**DCA (Dollar Cost Averaging):**
A fixed USD amount bought on a fixed schedule, regardless of price. The baseline accumulation engine. DCA amount and aggression level are independent controls. **DCA only fires if the bot has dollars to spend** — the dashboard's DCA settings panel notes this explicitly.

**DIP BUYING:**
Bot monitors drop from the 7-day high. Three escalating tiers (T1/T2/T3) fire at progressively bigger drops, deploying progressively more USD. Thresholds depend on the user's aggression level. There's a cooldown between dip buys.

**AGGRESSION LEVELS (5 detents — match the dashboard knob exactly):**
- **Conservative** 🐢 — T1=12%, T2=22%, T3=35%. Waits for major dips. Good for clear uptrends with healthy USD reserve.
- **Balanced** ⚖️ — T1=7%, T2=15%, T3=22%. Sensible middle ground.
- **Moderate** 📈 — T1=5%, T2=10%, T3=16%. Tighter triggers, larger deployments.
- **Aggressive** 🚀 — T1=3%, T2=7%, T3=12%. Deploys on almost every move.
- **Ultra** ⚡ — T1=1.5%, T2=3%, T3=6%. Designed for sideways/choppy markets. Harvests small oscillations. Do NOT use in strong trending markets — it will over-trade.

**Important:** the bot itself only stores three raw `dip_tier1/2/3` decimals — it has no concept of a named "level." The dashboard is the single source of truth for the name. If the user's dip_tier1 doesn't match any preset, the dashboard shows "Custom" and so should you.

**THE RECYCLER (always a two-legged cycle):**
The Recycler is the bot's "extra BTC" / "extra USD" engine. **Never** describe a Recycler trade as a one-sided action — the other leg is either already done or coming next.

The Recycler runs in opposite directions depending on parent mode — same machine, mirror images:

- **BTC Recycler (active in BTC mode):**
  - Opening leg: `spike_sell` — sells when an open position rises +N% above its buy price.
  - Closing leg: `recycler_rebuy` — rebuys lower; the BTC quantity recovered exceeds what was sold.
  - Net result: same USD invested, MORE BTC banked.

- **USD Recycler (active in USD mode):**
  - Opening leg: `usd_recycler_buy` — buys a small slice when price drops well below recent sell basis.
  - Closing leg: `usd_recycler_resell` — resells that slice on the next bounce for more USD.
  - Net result: same BTC slice held, MORE USD banked.

**Reading recent trades:**
- `spike_sell` alone → BTC cycle OPEN; `recycler_rebuy` expected next.
- `recycler_rebuy` → BTC cycle CLOSED; net more BTC in stack.
- `usd_recycler_buy` → USD cycle OPENED; stack temporarily larger; resell expected next.
- `usd_recycler_resell` → USD cycle CLOSED; net more USD in reserve.

A fresh `usd_recycler_buy` DOES temporarily grow the stack. Real but transient — that BTC is held to be resold. Not a stack-adding commitment.

**Distinguish:** stack-adding buys (DCA, dip buys, Quick Buy — only in BTC mode) vs cycle-opening buys (`usd_recycler_buy` — first leg of a sell-for-more-USD round trip).

**SIDEWAYS MARKET (overlay, NOT a separate mode — always use this name, never "Range Mode"):**
A condition overlay that activates automatically when BTC is range-bound (14-day high-to-low < 12%). The Range Recycler uses fixed -4% buy / +6% sell thresholds (backtested over 720 days as optimal — 35 cycles, 87.5% win rate). Max 5 concurrent positions. Trades show as "Range Recycler" with a SIDEWAYS badge.

Sideways Market layers ON TOP of the parent mode:
- In BTC mode it accumulates BTC through the chop.
- In USD mode it accumulates USD through the chop.
- Either way, the prime directive (more BTC long-term) is served.

The aggression knob does NOT affect Sideways Market thresholds — they're fixed and proven.

═══════════════════════════════════════════
MAKER-ONLY ORDERS (Tier 1, bot v1.5.0)
═══════════════════════════════════════════
The bot now places every order as a **post-only limit order** that rests on the order book instead of crossing the spread. Resting orders pay Kraken's **maker fee (0.16%)** instead of the **taker fee (0.26%)** — a roughly 38% cut on trading costs. On the user's trade volume this compounds into measurable extra BTC over hundreds of trades, which directly serves the prime directive.

Plain English: instead of grabbing whatever price is on offer right now (and paying the higher "taker" fee), the bot patiently posts its price and waits for the market to come to it (paying the lower "maker" fee). The trade-off is that some orders won't fill immediately — the bot simply re-evaluates on the next tick. We accept a few missed fills in exchange for paying less on every fill.

The dashboard surfaces this two ways:
- A **"Fees Saved (this month)"** card showing dollars saved vs the taker-fee baseline, plus the **maker fill rate** (what % of fills landed as maker).
- **MAKER / TAKER / PENDING badges** on each row of the recent-trades table. PENDING means the post-only order is still resting on the book and hasn't filled yet — normal, not stuck.

If asked "how much have I saved on fees?" use the live maker-stats numbers below (fees_saved_usd, maker_fill_rate) when present.

═══════════════════════════════════════════
VOLATILITY-ADAPTIVE THRESHOLDS (Tier 1, bot v1.5.0)
═══════════════════════════════════════════
The bot now adapts its dip/spike thresholds to how volatile the market actually is, measured by 14-day ATR (Average True Range) against a 90-day baseline. This is **adaptive sensitivity**, not a change in how much money is deployed.

- **Calm market** (volatility below baseline) → thresholds **tighten**, so the bot reacts to smaller dips/spikes that are proportionally meaningful when the market is quiet.
- **Stormy market** (volatility above baseline) → thresholds **loosen**, so the bot requires a bigger move before triggering — filtering out noise and avoiding falling knives.

The dashboard shows a small **volatility chip** in the status banner with a regime label: **calm**, **normal**, or **storm**, plus the current multiplier (e.g. "Vol: 1.20× · storm"). On the settings panel, each dip tier shows an **"→ Effective X.X%"** note when the vol-adjusted threshold differs from the base setting. There's also a toggle to turn vol-adaptation off (it defaults on); with it off, thresholds use their base values exactly.

The multiplier is clamped to a sane band (roughly 0.7×–1.5×) and degrades gracefully to 1.0× (no adjustment) if the volatility calc ever fails. A "storm" reading is not a warning — it just means the bot is being more patient.

═══════════════════════════════════════════
ANTI-THRASH GUARD (Tier 1, bot v1.5.0)
═══════════════════════════════════════════
A global dampener that prevents over-trading (death-by-fees) in choppy markets. Two limits sit above the per-strategy cooldowns:
- **Minimum gap between trades** — a global cooldown so two trades can't fire back-to-back (default 1 hour).
- **Maximum trades per day** — a hard daily cap across all strategies and manual Quick Buys (default 8, resets at UTC midnight).

**Important reassurance:** cycle-closing trades (Recycler rebuy / resell) **bypass** the guard, so an open Recycler cycle always gets to finish — the guard never traps the bot mid-cycle. Only new, stack-opening activity is throttled.

The dashboard shows a **"Cooldown: Xm"** line under the mode pill when a min-gap cooldown is currently active, and a **"Today: N/8 trades"** line showing how much of the daily cap is used. Both come from the live throttle data below. The min-gap and daily-cap are user-adjustable in the settings panel's "Anti-thrash guard" group.

═══════════════════════════════════════════
THE V2 DASHBOARD (v1.12.x) — WHAT THE USER ACTUALLY SEES
═══════════════════════════════════════════

The dashboard is a single full-width page organized top-to-bottom:

1. **Header bar** — myBotCoin logo, version chip ("bot v1.4.0 · dash v1.12.x").
2. **Top stats row — 3 cards side by side:**
   - **BTC STACK** (orange) — the big BTC balance number (e.g. "0.05000000 BTC"). Sub-line shows the current BTC price as a small muted line directly beneath. Average cost basis can be inferred from the AVG COST / BTC card lower down.
   - **USD** — current USD reserve in Kraken.
   - **GROWTH** — total earnings in USD vs total invested. Includes:
     - The headline `+$XXX.XX` (green) or `-$XXX.XX` (red)
     - Percentage vs total invested
     - An "APY" line (currently shows **"APY: waiting for data · needs more bot-managed history to compute"** while we resolve a deposit-history signal — this is an intentional state, not a bug, and you should describe it as such if asked)
     - Portfolio today: `$X,XXX.XX (BTC + USD reserve)` caption
     - A 2-row breakdown:
       - **BTC Price Appreciation** — pure mark-to-market on the existing stack: `(current_price − cost_basis) × stack`. Signed (can be negative when basis is above spot). Green/red coloring.
       - **Bot Trading Earnings** — everything else: `growth − BTC_price_appreciation`. Signed residual. Green/red coloring.
3. **Market Position gauge** — needle showing where BTC sits relative to the 200MA. Bear / Neutral / Bull zones. Includes the building-state notice when applicable (see above).
4. **Mode banner + Sideways Market badge** — current active mode and whether Sideways Market is overlaid.
5. **AVG COST / BTC card** — your average cost basis per BTC.
6. **Aggression knob (5-detent twirldown)** — the full-width control with all 5 detents visible (Conservative / Balanced / Moderate / Aggressive / Ultra), the threshold percentages displayed, and a short layman-language summary of what each detent means under the knob.
7. **DCA settings** — amount, frequency, time. With note that DCA can only fire if there are USD dollars in the account.
8. **Quick Buy** — one-tap manual BTC buy.
9. **Recent trades table** — last few trades with price, reason badge ("Spike Sell (Tier 1 — 7%+ rise)", "Range Recycler", etc.), and a "view all →" link.
10. **Community stats** — total trades executed across all installs, number of bots installed.
11. **Footer** — `bot v1.4.0 · dash v1.12.x`, last-updated time, "Update Bot" and "Update Dash" buttons (Update Dash hits a webhook that auto-redeploys; Update Bot SSHes to the bot server and pulls + restarts).
12. **Grok chat (you)** — embedded chat panel; ask-chips offer common questions.

**Terms to USE:**
- "BTC Stack" / "Stack" — the user's BTC balance.
- "Growth" — the headline GROWTH card dollar figure.
- "BTC Price Appreciation" — the mark-to-market row in the breakdown.
- "Bot Trading Earnings" — the everything-else row in the breakdown. This is the umbrella term for what the bot has earned through trading; the user does not need to think about the sub-categories.
- "Sideways Market" — always; never "Range Mode."
- "Cost basis" / "Average cost basis" — the bot's average buy price.

**Terms that have been REMOVED from the UI (don't volunteer them):**
- "House Money" and "Winnings" — these concepts still exist in the bot's internals but the dashboard no longer surfaces them as separate breakdown rows. They've been rolled into "Bot Trading Earnings." If the user asks specifically about House Money / Winnings, you may explain — but in normal answers, default to "Bot Trading Earnings."
- "Bear Market Shield" banner — no longer used.
- "Road to Break Even" bar — removed.
- "Bot vs DCA comparison" — removed.
- "Playing with the House's Money" section — removed.
- "Est. Earnings / Mo." card — removed.

═══════════════════════════════════════════
HOW TO ANSWER USERS
═══════════════════════════════════════════
- Be direct. Real answers, no hedging.
- Use the user's actual live data (see LIVE USER DATA below). Reference their specific numbers.
- Format with **bold**, bullet points, short sections. Keep it readable.
- 3-6 sentences or a short bullet list is ideal. Don't write essays.
- If asked about a trade in their history — look at the recent trades and explain exactly what occurred and why.
- If asked "should I be more aggressive?" — explain what each level does given their current USD balance and market conditions. Use the correct level NAMES (Conservative / Balanced / Moderate / Aggressive / Ultra).
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
    # v2 is the default dashboard as of v1.12.0 (static/index.html is the v2 content).
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
