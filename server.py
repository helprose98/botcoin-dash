"""
server.py — BotCoin Remote Dashboard Server.

Serves the dashboard UI and proxies API calls to the user's bot server.
The proxy allows the browser to call the bot API over HTTPS (same origin)
without mixed content issues, regardless of the bot server's IP.

Security: The proxy only forwards to port 8081 and only passes through
allowed API paths. It holds NO credentials — the password travels in the
request body/headers from the browser, same as before.
"""

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


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
