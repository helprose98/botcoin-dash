# Commands

This file tells Claude how to run, test, inspect, and validate this repository.

## Purpose

Claude should use this file to understand the normal commands for development and verification.

## What Claude should do with this file

- Read this file before trying to run or validate code.
- Prefer these commands over guessing.
- If commands are missing, inspect `package.json`, scripts, Docker files, and related config, then propose an update.

## Sections to fill in

### 1. Install

- `pip install -r requirements.txt` — only three dependencies: `flask`, `requests`, `paramiko`.
- No `package.json`/Node tooling — this is a plain Python/Flask app with a hand-written static frontend, no separate frontend install step.

### 2. Dev run

- `python server.py` — runs the Flask app directly. Confirmed in code: `server.py` ends with `app.run(host="0.0.0.0", port=8080, debug=False)`, so it's always port 8080 regardless of environment.
- `docker compose up` — matches production more closely: builds the image from `Dockerfile`, maps host port 80 → container port 8080, requires a `.env` file (not in repo) for secrets like `GROK_API_KEY`.

**Local preview workflow (validated, use this before proposing any merge to `main`):**

The `python server.py` route is the right one for quick local/branch previews — no Docker rebuild, no `.env` required (only `/chat` needs `GROK_API_KEY`; everything else, including the dashboard UI and `/proxy` to a real bot, works without it). Steps:

1. One-time setup (already done as of 2026-07-19, reusable for any future branch): create a venv and install deps.
   ```
   python -m venv .venv
   .venv\Scripts\python.exe -m pip install -r requirements.txt      # Windows
   ./.venv/bin/python -m pip install -r requirements.txt            # macOS/Linux
   ```
2. Check out the branch you want to preview: `git checkout <branch-name>`
3. Run the server: `.venv\Scripts\python.exe server.py` (Windows) or `./.venv/bin/python server.py` (macOS/Linux)
4. Open **http://127.0.0.1:8080/** in a browser.
5. To point the dashboard at a real bot for a realistic check, use the normal "connect" flow with a real bot IP + dashboard password — `/proxy` will reach the actual bot server, same as production.
6. Stop the server (Ctrl+C, or kill the process) when done.

### 3. Build

- `docker compose build` (or `docker compose up --build`) — the only "build" step; there is no JS/CSS bundler, transpiler, or separate frontend build. `static/*.html` is served as-is.

### 4. Test

- None found. No test files, no test framework/config anywhere in the repo.

### 5. Lint / typecheck

- None found. No `flake8`/`ruff`/`mypy`/`pylint` config, no linting for the static JS embedded in the HTML files.

### 6. Deploy-related checks

- Check `VERSION` — compared against `https://raw.githubusercontent.com/helprose98/botcoin-dash/main/VERSION` by `/dash/version` to decide if a self-update is available; bump it on release.
- Review `update.sh` — runs on the host (not containerized), does `git reset --hard origin/main` then a full `docker compose down/build --no-cache/up -d`. Confirm this is expected before merging anything to `main`, since a merge can trigger a live rebuild.
- Confirm `.env` / environment variable usage — `GROK_API_KEY` is required for `/chat` to work; not present in the repo, must exist on the host.
- Review `Dockerfile` and `docker-compose.yml` for port mapping (`80:8080`) and volume mounts (`VERSION`, `data/`) before changing container behavior.

### 7. Botcoin-specific warnings

- Do not guess production deploy steps.
- Do not assume Vercel.
- Do not assume bot changes go live automatically.
- Treat trading-logic validation as separate from UI validation.

## Operating rule

If this file is incomplete, Claude should inspect the repo and fill it in before attempting non-trivial development work.
