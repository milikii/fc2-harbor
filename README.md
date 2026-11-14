# fc2-harbor

FC2 / Nyaa BT automation for a Debian NAS: subscribe to Nyaa Sukebei FC2
releases, push them to a private Telegram chat, download with qBittorrent and
organize the finished files into a media library — idempotently, and without ever
deleting data that has not been safely moved.

The implementation follows [`docs/BT_AUTOMATION_SPEC.md`](docs/BT_AUTOMATION_SPEC.md);
that repository copy is the authoritative specification.

## Status

| Milestone | Scope | State |
|---|---|---|
| M1 | Skeleton: config, logging, DB + migrations, gate, state machine, qBittorrent, manual bot, organizer, monitor | **done** |
| M2 | Nyaa site-wide subscription (RSS + search fallback) | planned |
| M3 | Nine metadata providers, proxy pool, image cache, translation | planned |
| M4 | Cards, full Telegram loop, message group deletion, reconciliation | planned |
| M5 | Transmission, admin commands, metrics, full integration tests, backup/deploy docs | planned |

### What M1 already does

- **Configuration** (`app/config.py`): YAML + `${ENV_VAR}` expansion, pydantic v2
  validation, fail-fast errors that name the missing variable, secret redaction.
  The nine-provider order from spec 7.1 is validated at startup and cannot be
  reordered.
- **Logging** (`app/logging.py`): structlog JSON/console output with the spec 21
  fields, plus a redaction processor that masks tokens, passwords, cookies, API
  keys, URL credentials and Telegram bot tokens.
- **Database** (`app/db/`): SQLAlchemy 2.x models for all ten spec tables, Alembic
  migration, SQLite WAL, foreign keys on, `BEGIN IMMEDIATE` write serialisation.
- **Content gate** (`app/services/gate.py`): blacklist → accepted → info hash →
  source item, in that order, in one transaction with the accepted-entry write.
- **State machine** (`app/services/state.py`): the spec 13 lifecycle with
  compare-and-set updates, per-state timestamps and an audit trail in
  `decision_events`.
- **qBittorrent adapter** (`app/downloaders/qbittorrent.py`): add/get/list/remove,
  explicit save path, AutoTMM disabled, idempotent add, `deleteFiles=false`
  enforced in code.
- **Manual bot** (`app/bots/manual.py`, `app/services/manual_ingest.py`): magnet
  parsing, FC2 normalisation, the shared gate, category buttons for ambiguous
  content, `_review` fallback, `/status`, `/retry`.
- **Organizer** (`app/organizer/`): explicit path mapping, directory-name
  sanitising with traversal protection, atomic same-disk rename, cross-disk
  `.partial` + `fsync` + size/SHA-256 verification + atomic rename, failure keeps
  sources and task.
- **Monitor**: completion = progress 1.0 **and** a finished state **and** two
  identical filesystem observations; organize first, then remove the task; startup
  reconciliation re-submits accepted-but-unsubmitted jobs.
- **Health API** (`app/api/health.py`): `/healthz`, `/readyz`, `/status`, bound
  inside the container network only.

## Requirements

- Docker + Docker Compose on the NAS (runtime).
- Python 3.12 and [uv](https://github.com/astral-sh/uv) for development.
- qBittorrent reachable over the NAS LAN (it may be an existing service).
- A private Telegram bot for manual magnet submission (the subscription bot is
  wired in M4).

## Quick start (development)

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
cp .env.example .env            # fill in real values, never commit it
cp config/example.yaml config/config.yaml
.venv/bin/alembic upgrade head
.venv/bin/python -m app.main
```

Checks:

```bash
.venv/bin/pytest        # 185 tests, no network access
.venv/bin/ruff check .
.venv/bin/mypy app
```

Tests never touch the real network: qBittorrent is a fake (or a mocked HTTP
transport via `respx`), Telegram is a fake, and all paths live in `tmp_path`. The
schema used by tests is created once per session by running the real Alembic
migration.

## Deployment

```bash
cp .env.example .env
cp config/example.yaml config/config.yaml   # adjust paths, users, proxies
docker compose up -d --build
```

The compose file mirrors spec section 19:

- one container, `restart: unless-stopped`, no published ports (Telegram uses
  long polling);
- `./config` read-only, `./data` for SQLite + cache, `/downloads` and `/library`
  bind-mounted from the NAS;
- non-root user (uid/gid 1000), no Docker socket, healthcheck on `/healthz`,
  `stop_grace_period: 60s`;
- `no-new-privileges`, rotated json-file logs.

Operational notes:

- The container user must be able to read/write the same download directories as
  qBittorrent (uid/gid 1000 by default).
- SQLite must stay on local disk (never NFS/SMB) because of WAL.
- `storage.path_mappings` must describe the downloader path as the container sees
  it; an unmapped path fails the job instead of guessing.
- Daily backup: `./scripts/backup.sh /path/to/backups` uses the SQLite online
  backup API and archives `config/`, keeping 14 copies.

## Safety rules encoded in the code

- Gate order is fixed: blacklist → accepted → info hash → source item, and the
  check plus the `accepted_entries` write share one transaction.
- A qBittorrent task is removed only after the organizer reports every file
  moved; removal always uses `deleteFiles=false`.
- Files are never moved before the download is complete and file counts/sizes
  have been stable for two consecutive observations.
- Cross-device moves copy to `.partial`, `fsync`, verify, then atomically rename;
  on failure the source files and the torrent task are kept.
- Provider order is fixed by configuration validation, never by hit rate.
- Secrets come from the environment, are redacted in logs, and never enter the
  repository.
- qBittorrent and NAS traffic ignores global proxy environment variables
  (`trust_env=False`); proxy routing is explicit and per-provider (M3).

## Layout

```text
app/
  config.py  logging.py  main.py
  api/         health endpoints
  bots/        manual bot (subscription bot in M4)
  db/          models, session, Alembic migrations
  downloaders/ downloader protocol + qBittorrent
  organizer/   paths, mover, stability, monitor
  schemas/     release / metadata / download models
  services/    normalize, gate, state, jobs, manual ingest, notify
  sources/     source adapter protocol (Nyaa in M2)
  providers/   metadata provider protocol (nine adapters in M3)
config/example.yaml   configuration template
docker/entrypoint.sh  migrate, then exec the app (PID 1, graceful stop)
docs/                 versioned specification
scripts/backup.sh     daily backup helper
tests/                unit + integration, all offline
```

## Known deviations and open questions

Kept explicit so they can be reviewed rather than silently assumed:

1. `config/example.yaml` quotes every `${VAR}` placeholder. The spec's section 18
   example puts `${JP_PROXY_URL}` inside a YAML flow mapping, which is not valid
   YAML (`{` starts a mapping).
2. FC2 numbers are accepted at 5–8 digits. The spec lists formats but not the
   digit range; shorter numbers in prose are not treated as content ids.
3. `provider.sources` must keep the nine canonical hosts and stay `enabled`. This
   enforces spec 7.1/25.4 at startup; a temporarily broken provider is handled by
   runtime health in M3, not by disabling it in configuration.
4. `translation.enabled=true` is rejected until M3 instead of being silently
   ignored.
5. `gate.admit` writes the release row, so the gate must be evaluated **before** a
   release is persisted. The M2 poller will follow that contract.
6. The manual bot's "choose a category" step keeps the pending submission in
   memory; a restart drops it (nothing was written yet, so re-sending is safe).

## Licence

See [LICENSE](LICENSE).
