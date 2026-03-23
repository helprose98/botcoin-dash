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
import requests
from flask import Flask, send_from_directory, request, Response
from pathlib import Path

app = Flask(__name__, static_folder="static")

ALLOWED_PATHS = (
    "/api/health",
    "/api/status",
    "/api/trades",
    "/api/settings",
    "/api/buy",
    "/api/update",
    "/api/version",
    "/api/setup/",
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
    """Trigger a self-update of the dash server via the update watcher."""
    trigger = Path("/app/data/update.trigger")
    try:
        trigger.write_text("update")
        return {"ok": True, "message": "Dash update started. Page will reload in ~2 minutes."}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


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

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return {"error": "OpenAI API key not configured"}, 500

    # ── Fetch live bot data for context ──────────────────────────────────────
    bot_context = ""
    if bot_ip and password:
        try:
            import re
            if re.match(r'^[\d.]+$', bot_ip):
                headers = {"X-Dashboard-Password": password}
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
                    bot_context += f"""
- DCA amount: ${cfg.get('dca_amount','?')} per {cfg.get('dca_frequency','?')}
- Dip thresholds: tier1={float(cfg.get('dip_threshold','0.015'))*100:.1f}%, tier2={float(cfg.get('dip_tier2_threshold','0.03'))*100:.1f}%, tier3={float(cfg.get('dip_tier3_threshold','0.06'))*100:.1f}%
- Recycler: {'enabled' if cfg.get('recycler_enabled') else 'disabled'}"""

                if trades_r.ok:
                    trades_list = trades_r.json()[-5:] if isinstance(trades_r.json(), list) else []
                    if trades_list:
                        bot_context += "\n- Recent trades (last 5): "
                        bot_context += ", ".join(
                            f"${t.get('usd_spent','?')} on {t.get('timestamp','?')[:10]} ({t.get('reason','?')})"
                            for t in trades_list
                        )
        except Exception:
            pass  # context is best-effort; answer without it if bot unreachable

    system_prompt = f"""You are BotCoin Assistant — a knowledgeable, opinionated guide built into the BotCoin dashboard.

ABOUT BOTCOIN:
BotCoin is a self-hosted Bitcoin DCA (dollar-cost averaging) savings bot. It runs on a personal Vultr server and trades on Kraken.
The entire philosophy of BotCoin is: accumulate as much Bitcoin as possible over the long term. We measure success in BTC (satoshis), NOT in USD.
BotCoin users are long-term holders who believe in Bitcoin's 4-year cycle and want to stack sats consistently regardless of short-term price.

BOTCOIN SETTINGS EXPLAINED:
- DCA Amount: Fixed USD amount invested on a schedule (daily/weekly/monthly). This is separate from aggression.
- Aggression levels control dip-buying thresholds:
  * Conservative: buys on 3%/6%/12% dips
  * Moderate: buys on 2%/4%/8% dips
  * Aggressive: buys on 2%/4%/8% dips with larger allocations
  * Ultra: buys on 1.5%/3%/6% dips — best in sideways/choppy markets
- Auto mode: bot decides whether to accumulate BTC or hold USD based on market conditions
- BTC Accumulate mode: always buying, never selling
- Recycler: sells a small portion of BTC at a profit target to recycle back into USD for more dip-buying
- Break-even price: the BTC price per coin at which you'd be at zero profit/loss (NOT your total account value)
- Cost basis: your average purchase price per BTC across all trades

BOTCOIN PHILOSOPHY & METHODOLOGY:
- DCA (dollar-cost averaging) is the core strategy: invest consistently regardless of price
- Volatility is an OPPORTUNITY, not a threat. Dips = chance to buy more sats at a discount
- The goal is to accumulate more BTC over time. If you end a month with more BTC than you started, the bot did its job
- Short-term USD value of your stack is irrelevant. A lower USD price just means you can buy more sats for the same dollar
- Bitcoin has historically followed 4-year cycles tied to halving events. Long-term holders who DCA'd through every bear market came out ahead
- For small budgets ($50-100/mo), consistent DCA + higher aggression is better than trying to time the market
- The aggression slider and DCA amount are SEPARATE controls. You can DCA $50/mo but still have Ultra aggression for dip-buying from your USD reserve

HOW TO ANSWER:
- You are explaining how BotCoin works and what its settings do — that is factual product explanation, not financial advice.
- When users ask "should I be more aggressive?" — explain what each aggression level does mechanically and what kind of market conditions each is designed for. That is a feature explanation, not a personal recommendation.
- Never refuse to explain how a BotCoin setting works. That is your entire purpose.
- Keep answers plain-English and specific to BotCoin. Do not redirect users to a financial advisor for questions about how BotCoin's own settings function.
- Format responses using markdown: use **bold** for setting names, bullet points for lists, and line breaks between sections. This improves readability.
- Frame everything around BTC accumulation: more sats = good, fewer sats = not the goal.
- For small budgets ($50/mo): explain that BotCoin is designed for exactly this — small consistent amounts work well with DCA, and the aggression setting is independent of the DCA amount so they can still respond to dips aggressively.
- When someone asks about current market conditions in relation to settings: explain what the bot's Auto mode does to adapt, and what each aggression level is optimized for (e.g. Ultra is best in sideways/choppy markets, Conservative is better in clear uptrends).

{bot_context if bot_context else "(Live bot data unavailable — answer based on general BotCoin context.)"}\n"""

    # ── Call OpenAI (non-streaming — Cloudflare buffers SSE) ─────────────────
    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            },
            json={
                "model":       "gpt-4o-mini",
                "stream":      False,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": question},
                ],
                "max_tokens":  500,
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


@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
