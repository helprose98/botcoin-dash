# Repo Map

This file helps Claude build a mental map of this repository before editing.

## Purpose

Use this file as the first place to summarize how the repo is organized once Claude has inspected it.

## What Claude should do with this file

- Read this file before deep code work.
- Treat it as the high-level map of the repository.
- Update or suggest updates if the map is incomplete or inaccurate.

## Sections to fill in

### 1. Entry points

- Server entry: `server.py` — single Flask app, serves both the API routes and the static frontend. Runs on container port 8080 (mapped to host port 80 in `docker-compose.yml`).
- Frontend entry: `static/index.html` — the current dashboard, a self-contained HTML/CSS/JS page (no build step, no framework, no `src/`).
- Legacy frontend: `static/v1.html` — older dashboard version, still served at `/v1`; unclear if still linked from production (see Unknowns).
- Static content pages: `static/about.html` (`/about`), `static/setup-guide.html` (`/setup-guide`).
- Deploy-time entry: `update.sh` — runs on the host (not in the container), triggered by cron or a trigger file; pulls new dash code and rebuilds.

### 2. Important folders

- `static/` — the entire frontend: dashboard UI, legacy UI, and static informational pages. Served directly by Flask (`send_from_directory`), no bundler/build pipeline.
- `docs/` — project documentation for Claude (this folder).
- No `bot/` folder here — trading/runtime logic lives entirely in the sibling `botcoin-bot` repo. This repo only *talks to* a bot over HTTP via `/proxy`; it contains no trading logic itself.
- No `data/` folder in the repo — `docker-compose.yml` mounts `./data` as a volume for the SQLite community-stats DB and the update trigger file; it's runtime state, not source.

### 3. High-risk areas

- `server.py` `/install/start` (~line 262) — accepts a remote server IP and **root password** from the browser, opens an SSH connection with `paramiko`, and runs a bootstrap script as root. Treat it as the highest-risk surface in the repo: it handles a raw root password and runs commands as root on whatever host is supplied. Related open item: `docs/private/security-notes.md`.
- `server.py` `/chat` (~line 462) — reads the `GROK_API_KEY` secret from the environment, and also accepts a bot IP + bot password from the client to pull live trade/status data as chat context.
- `server.py` `/proxy` and `/dash/maker_stats` — forward requests to the user's bot server using an IP and (for maker_stats) an `X-Bot-Password` header. Has SSRF guards (blocks private/loopback/link-local IPs, restricts to a path allowlist) but is still a credential-carrying pass-through.
- `server.py` `/dash/update` (~line 425) — self-update trigger; reuses the bot password as an ad hoc auth "secret", then writes a trigger file that host-side tooling picks up.
- `update.sh` — runs on the **host**, outside the container. Does `git fetch` + `git reset --hard origin/main` and a full `docker compose down/build --no-cache/up -d`. This is deployment behavior with real production impact even though it contains no trading logic.
- `Dockerfile` / `docker-compose.yml` — deployment/env config: `env_file: .env` (not in repo, holds `GROK_API_KEY` etc.), port mapping, volume mounts for `VERSION` and `data/`.

### 4. Safe areas

- `static/about.html`, `static/setup-guide.html` — static informational content, no backend wiring.
- `server.py` community-stats code (`_init_stats_db`, `_record_bot_seen`, `_get_community_stats`, `/api/community-stats`) — aggregate, anonymous counters only, no personal data or credentials.
- Cosmetic/copy/layout portions of `static/index.html` — anything not touching the install-SSH panel, the chat panel, or the `/proxy` and `/dash/*` fetch calls.

### 5. Unknowns

- Whether `static/v1.html` is still reachable/linked in production or is fully legacy and safe to remove.
- Where `.env` (holding `GROK_API_KEY` and any other dash-side secrets) is actually populated on the production host — not present in this repo.
- What triggers `update.sh` in practice (cron entry vs. `install-update-watcher.sh`-style watcher) — the watcher script itself lives in `botcoin-bot`, not here, so the exact host-side wiring for this repo's own self-update isn't visible from the repo alone.
- **2026-07-19 — Known security item, parked:** an exposure affecting fresh installs is tracked privately in `docs/private/security-notes.md` with its agreed fix direction. Revisit before this gets wider use beyond the current small group.

## Operating rule

If this file is missing details, Claude should inspect the repo and propose an updated version before making major changes.
