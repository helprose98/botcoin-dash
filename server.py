"""
server.py — BotCoin Remote Dashboard Server.

A lightweight Flask server that serves the dashboard UI.
All bot data is fetched client-side directly from the user's bot server IP.
This server holds NO credentials and NO bot data — it's purely a UI host.
"""

from flask import Flask, send_from_directory
from pathlib import Path

app = Flask(__name__, static_folder="static")

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
