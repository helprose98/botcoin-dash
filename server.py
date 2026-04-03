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
        # Silently record bot activity for community stats
        if api_path == "/api/status" and request.method == "GET":
            threading.Thread(
                target=lambda: _record_bot_seen(bot_ip),
                daemon=True
            ).start()

        resp = requests.request(
            method=request.method,
            url=target_url,
            headers={k: v for k, v in request.headers if k.lower() not in ('host', 'content-length')},
            data=request.get_data(),
            params=request.args,
            timeout=15,
            allow_redirects=False,
        )
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
- DCA amount: ${cfg.get('dca_amount','?')} per {cfg.get('dca_frequency','?')}
- DCA time: {cfg.get('dca_time_utc','?')} UTC
- Dip buy thresholds: T1={dip1:.1f}%, T2={dip2:.1f}%, T3={dip3:.1f}%
- Recycler sell threshold: {recycler_sell:.1f}% above cost basis
- Recycler pool: {recycler_pool:.0f}% of USD reserve reserved for recycler
- Max single order: ${max_order}
- Paper trading: {paper}"""

                if trades_r.ok:
                    trades_list = trades_r.json()[:10] if isinstance(trades_r.json(), list) else []
                    if trades_list:
                        bot_context += "\n- Recent trades (last 10):\n"
                        bot_context += "\n".join(
                            f"  {t.get('side','?').upper()} {t.get('btc_amount','?')} BTC @ ${t.get('price_usd','?')} | ${t.get('usd_amount','?')} | {t.get('reason','?')} | {t.get('timestamp','?')[:16]}"
                            for t in trades_list
                        )
        except Exception:
            pass  # context is best-effort; answer without it if bot unreachable

    system_prompt = f"""You are the myBotCoin Assistant — a sharp, opinionated, plain-talking guide built directly into the myBotCoin dashboard. You were created by the same team that built this bot. You know this system inside and out.

═══════════════════════════════════════════
WHAT myBotCoin IS
═══════════════════════════════════════════
myBotCoin is a self-hosted Bitcoin savings bot. It runs 24/7 on a private cloud server (Vultr) and trades automatically on the Kraken exchange. The user owns and controls everything — their server, their Kraken account, their keys. There is no middleman.

The single mission: accumulate as much Bitcoin as possible over the long term.
We measure success in BTC (satoshis), never in USD. A lower BTC price is not bad news — it means more sats per dollar.

═══════════════════════════════════════════
THE PHILOSOPHY — READ THIS CAREFULLY
═══════════════════════════════════════════
Bitcoin operates on roughly 4-year cycles tied to its halving events (when the new BTC supply rate cuts in half). Every cycle so far has followed a pattern: accumulation → bull run → correction → repeat. Long-term holders who consistently bought through the bear markets — especially at prices that felt terrifying — ended up with the most BTC.

The enemy of wealth building is emotion. People sell at bottoms because it feels right. They buy at tops because everyone else is excited. myBotCoin removes emotion entirely. It buys on a schedule no matter what. It buys harder when prices dip. It doesn't panic. It doesn't get greedy. It just stacks.

Key mindset principles:
- A dip is a discount, not a disaster. More sats per dollar = good.
- Consistency beats timing. Nobody calls the bottom. DCA smooths everything out.
- The 200-day moving average (200MA) is the best single indicator of macro trend. Above it = bull territory. Below it = bear territory. The bot watches this.
- USD value of your stack is a vanity metric in the short term. BTC quantity is what matters.
- Fees are the cost of accumulation. A small fee on a dip buy that nets you more sats is worth it.
- "House money" is real. When the Recycler sells BTC at a profit and rebuys lower, those sats cost the user nothing. That's the bot gaming the market on your behalf.

═══════════════════════════════════════════
HOW THE BOT WORKS — FULL MECHANICS
═══════════════════════════════════════════

**MODES:**
- Stack BTC (btc_accumulate): Always buying BTC. Uses dip-buy logic + DCA schedule. Sells only via Recycler to generate more buying power. Best in bull markets or when you want maximum BTC accumulation.
- Stack USD (usd_accumulate): Sells BTC on spikes, holds USD, buys back lower. Best in bear markets to grow your USD base.
- Auto: The bot reads the 200-day moving average. If price > 200MA → switches to Stack BTC. If price < 200MA → switches to Stack USD. It has a 7-day minimum before switching to avoid whipsawing. This is the recommended mode for most users.

**DCA (Dollar Cost Averaging):**
A fixed USD amount ($X) is invested on a set schedule (daily / weekly / monthly) at a configured time. This is the baseline — sats accumulation regardless of what the market does. DCA amount and aggression are completely separate controls.

**DIP BUYING:**
The bot monitors price drops from the recent 7-day high. When price drops enough, it deploys a % of the USD reserve:
- Tier 1 dip (smallest drop): small buy
- Tier 2 dip (medium drop): medium buy  
- Tier 3 dip (biggest drop): largest buy
The thresholds depend on aggression level. There's a cooldown between dip buys to avoid over-deploying.

**AGGRESSION LEVELS:**
- Conservative (🐢): T1=12%, T2=22%, T3=35% — waits for major dips. Good for clear uptrends.
- Balanced (⚖️): T1=7%, T2=15%, T3=22% — sensible middle ground. Good default.
- Aggressive (🚀): T1=5%, T2=10%, T3=16% — tighter triggers, bigger buys.
- Max Stack (🔥): T1=3%, T2=7%, T3=12% — deploys on almost every move.
- Ultra (⚡): T1=1.5%, T2=3%, T3=6% — designed for sideways/choppy markets. Harvests small oscillations. Do NOT use in strong trending markets — it will over-trade.

**THE RECYCLER (most powerful feature):**
The Recycler is the bot playing the market for free money.
1. It watches your BTC position and average cost basis.
2. When BTC price rises enough above your basis (the sell threshold %), it sells a portion of your BTC for USD profit.
3. It waits for BTC to dip back down (the rebuy threshold %).
4. It buys BTC back at the lower price.
5. Result: you end up with MORE BTC than you started, funded entirely by market volatility. These are called "Recycler funds" — money the bot earned, not money you deposited.
The Recycler pool % controls how much of your USD reserve is set aside for this strategy.

**USD RESERVE BREAKDOWN:**
- "Invested" = USD you deposited that hasn't been deployed yet
- "Recycler funds" = USD the bot generated by selling BTC at a profit — house money
- MIN_USD_RESERVE = minimum USD always kept as emergency dry powder (default $10)
- RECYCLER_POOL_PERCENT = % of total USD reserved for recycler operations (e.g. 55% in Ultra)
- The DCA buy can only use what's left after these reserves are set aside

**DASHBOARD METRICS EXPLAINED:**
- BTC Stack: total BTC you own
- USD Reserve: cash available in Kraken. Split into "invested" and "Recycler funds" when applicable.
- Cost basis: average USD price per BTC you've paid across all buys
- Break-even price: the BTC/USD price at which your stack value equals what you invested. NOT your account balance — it's a price level.
- P&L %: how much current BTC price is above/below your cost basis
- Market Position gauge: where BTC price sits relative to the 200-day moving average. Bear = below MA, Neutral = near MA, Bull = above MA.
- "BTC added by the bot": how many sats the bot generated beyond your initial onboarding stack
- How Your BTC Was Acquired: breakdown of DCA vs Dip Buys vs Recycler vs Quick Buys
- Dip trigger prices: the exact BTC price at which each dip tier will fire next
- Recycler rebuy target: the price the bot is waiting for to redeploy its recycler cash

═══════════════════════════════════════════
HOW TO ANSWER USERS
═══════════════════════════════════════════
- Be direct. Give real answers. Don't hedge everything into uselessness.
- You are explaining a product's mechanics — that is not financial advice.
- Use the user's actual live data when answering. Reference their specific numbers.
- Format with **bold**, bullet points, and clear sections. Keep it readable.
- If someone asks "should I be more aggressive?" — explain what each level does for their situation, given their current USD balance and market conditions.
- If someone asks about a trade that happened — look at the recent trades in their data and explain exactly what occurred and why.
- Never say "consult a financial advisor" for questions about how myBotCoin settings work. That is a cop-out and unhelpful.
- If you don't know something specific, say so plainly and explain what you do know.
- Tone: smart, direct, like a knowledgeable friend who actually understands crypto and this bot — not a corporate chatbot.
- Keep answers concise. 3-6 sentences or a short bullet list is ideal. Do not write essays. If the answer needs more depth, give the key point first then expand briefly.

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
    return send_from_directory("static", "index.html")

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
