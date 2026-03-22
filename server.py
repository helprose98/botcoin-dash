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
docker compose up -d --build -q
bash install-update-watcher.sh
echo 'BOTCOIN_INSTALL_COMPLETE'
"""


@app.route("/install/run", methods=["GET"])
def install_run():
    """
    SSE endpoint — SSHes into the target server and streams install output
    to the browser in real time.
    Query params: ip, password
    Credentials are used once and never stored.
    """
    import re
    import paramiko
    import threading
    import queue

    ip       = request.args.get("ip", "").strip()
    password = request.args.get("password", "").strip()

    if not ip or not re.match(r'^[\d.]+$', ip):
        return {"error": "Invalid IP"}, 400
    if not password:
        return {"error": "Password required"}, 400

    def generate():
        q = queue.Queue()

        def ssh_thread():
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                q.put(("status", f"Connecting to {ip}..."))
                client.connect(ip, username="root", password=password, timeout=15)
                q.put(("status", "Connected. Starting installation..."))

                transport = client.get_transport()
                channel   = transport.open_session()
                channel.get_pty()
                channel.exec_command(f"bash -c '{INSTALL_SCRIPT}'")

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
                                    q.put(("done", "BotCoin installed successfully!"))
                                else:
                                    q.put(("log", line))
                    elif channel.exit_status_ready():
                        code = channel.recv_exit_status()
                        if code != 0:
                            q.put(("error", f"Install failed (exit code {code})"))
                        break
                client.close()
            except paramiko.AuthenticationException:
                q.put(("error", "Authentication failed — check your root password"))
            except Exception as e:
                q.put(("error", str(e)))
            finally:
                q.put(("end", ""))

        t = threading.Thread(target=ssh_thread, daemon=True)
        t.start()

        while True:
            try:
                kind, msg = q.get(timeout=120)
                yield f"data: {kind}|{msg}\n\n"
                if kind in ("done", "error", "end"):
                    break
            except queue.Empty:
                yield "data: error|Installation timed out\n\n"
                break

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


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
