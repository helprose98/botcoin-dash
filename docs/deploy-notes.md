# Deploy Notes

Use normal Git and SSH-based operational workflows.

Current known patterns:

- Botcoin uses two separate codebases with different update behavior: `botcoin-dash` and `botcoin-bot`.
- Dashboard deployment is Vultr-hosted, not Vercel.
- Dashboard updates auto-deploy from GitHub through the existing server-side update path.
- Bot updates are manually triggered through the dashboard workflow.
- Do not use browser automation or cloud-browser deployment shortcuts for Botcoin.
- Treat pushes to the dashboard's production-connected branch as production events.

## How the dashboard actually deploys (measured 2026-09-21)

- `/etc/cron.d` on the dash server runs `update.sh` every minute. Server addresses are in `docs/private/security-notes.md`.
- `update.sh` compares the server's `VERSION` with `VERSION` on GitHub `main`, and exits if they are equal.
- So a merge to `main` goes live within about a minute **only if it changes `VERSION`**. A merge that doesn't bump `VERSION` is not deployed.
- `update.sh` runs `docker compose down` before `build --no-cache`. A failed build leaves the dashboard down until someone fixes it by hand. The bot fixed this pattern in v2.2.1; the dash has not.

## Branching workflow

We now use feature branches for changes instead of committing directly to `main`.
Work happens on a branch (e.g. `test/verify-deploy-pipeline`), gets pushed to
GitHub for review, and merging into `main` is the deliberate "go live" step for
the dashboard — nothing reaches production until that merge happens.

## Rollback point

- Tag `pre-optimization-baseline` → `b16e0d0d9c80c6f26184c1e3b44921315da79152` (v2.8.6, production as of 2026-09-21). Permanent: never move or delete it.
- To roll back: check out the tag on the server and redeploy.

## Superseded branches

- `feature/aggression-level-tracking` (`12c0851`, v2.8.7): sent the dial's preset name to the bot. Closed unmerged 2026-09-21, superseded by Strategy v3, which replaces the 5-preset dial with one knob. Kept on GitHub for reference; do not merge.

## Note on these docs

`docs/` and `CLAUDE.md` are not committed to git. In July, switching away from a branch that had committed this file deleted it from disk. It was recovered from commit `786bd68` on 2026-09-21. Until these files are committed, a branch switch can delete them again.
