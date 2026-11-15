# fc2-harbor

FC2 / Nyaa BT automation for a Debian NAS: subscribe to Nyaa Sukebei FC2
releases, push them to a private Telegram chat, download with qBittorrent and
organize the finished files into a media library — idempotently, and without ever
deleting data that has not been safely moved.

The implementation follows [`docs/BT_AUTOMATION_SPEC.md`](docs/BT_AUTOMATION_SPEC.md);
that repository copy is the authoritative specification. Every deliberate
deviation is recorded in [`docs/deviations.md`](docs/deviations.md).

## Status

| Milestone | Scope | State |
|---|---|---|
| M1 | Skeleton: config, logging, DB + migrations, gate, state machine, qBittorrent, manual bot, organizer, monitor | **done** |
| M2 | Nyaa site-wide subscription: RSS + search catch-up, cursor, overlap re-check, content/release dedupe | **done** |
| M3 | Nine metadata providers, proxy pool, image cache, translation | **done** |
| M4 | Cards, full Telegram loop, message group deletion, push pipeline | **done** |
| M5 | Transmission, admin commands, metrics, full integration tests, backup/deploy docs | **done** |

### What M1 does

- **Configuration** (`app/config.py`): YAML + `${ENV_VAR}` expansion, pydantic v2
  validation, fail-fast errors that name the missing variable, secret redaction.
  The nine-provider order from spec 7.1 is validated at startup and cannot be
  reordered.
- **Logging** (`app/logging.py`): structlog JSON/console output with the spec 21
  fields, plus a redaction processor for tokens, passwords, cookies, API keys,
  URL credentials and Telegram bot tokens.
- **Database** (`app/db/`): SQLAlchemy 2.x models for all ten spec tables, Alembic
  migration, SQLite WAL, foreign keys on, `BEGIN IMMEDIATE` write serialisation.
- **Content gate** (`app/services/gate.py`): blacklist → accepted → info hash →
  source item, in that order, in one transaction with the accepted-entry write.
- **State machine** (`app/services/state.py`): the spec 13 lifecycle with
  compare-and-set updates, per-state timestamps and an audit trail.
- **qBittorrent adapter** (`app/downloaders/qbittorrent.py`): add/get/list/remove,
  explicit save path, AutoTMM disabled, idempotent add, `deleteFiles=false`
  enforced in code.
- **Manual bot** (`app/bots/manual.py`): magnet parsing, FC2 normalisation, the
  shared gate, category buttons, `_review` fallback, `/status`, `/retry`.
- **Organizer** (`app/organizer/`): explicit path mapping, sanitising with
  traversal protection, atomic same-disk rename, cross-disk `.partial` + `fsync` +
  verification + atomic rename, failure keeps sources and task.
- **Monitor**: completion = progress 1.0 **and** a finished state **and** two
  identical filesystem observations; organize first, then remove the task.

### What M2 adds

- **Nyaa source adapter** (`app/sources/nyaa.py`): site-wide FC2 RSS first, HTML
  search page as catch-up when one round overflows the RSS window. No uploader
  filter can be configured (spec 6.1).
- **Explicit HTTP client** (`app/network/clients.py`): per-domain concurrency,
  retry only idempotent GETs, `Retry-After` honoured, conditional requests
  (`If-None-Match` / `If-Modified-Since`), `trust_env=False`.
- **Cursor and overlap re-check**: `source_cursors` stores ETag, Last-Modified and
  the last item ids; each round re-evaluates the whole page and the database
  decides what is new — never `pubDate > cursor` alone.
- **First-run window**: only items within `initial_lookback_hours` (default 24) are
  processed on the first round.
- **Release registrar** (`app/services/releases.py`): gate **before** the release
  write; an already-accepted FC2 number still stores a new uploader's release as a
  candidate download source, without job, accepted entry or push.
- **Poller** (`app/services/subscription.py`): interval + jitter, failure counter,
  per-candidate isolation, spec 21 round metrics.
- **Bounded resubmission**: startup reconciliation retries `ACCEPTED` jobs, stops
  at `download.max_retries` and reports instead of looping forever.
- **Push queue query** (`app/services/push_queue.py`): read-only selection of
  contents that have a release but are not accepted, not blacklisted and not yet
  notified — M4 will drive cards from it without touching the gate.

### What M3 adds

- **Nine provider adapters** (`app/providers/`): Fc2, fc2_hub, Javdb, Mmtv,
  freejavbt, miss_av, Javlibrary, avsox, Madou, plus the Nyaa view-page fallback.
  Each has its own URL builder and selectors; a page that no longer matches raises
  `ProviderError("structure_changed")` instead of returning an empty success.
- **Field-level aggregation** (`app/providers/coordinator.py`): fixed order, no
  overwrite, gap filling, preview dedupe and cap, early stop, failure isolation,
  censorship conflicts become `UNKNOWN`, Nyaa fallback only for what is missing.
- **Proxy pool and routing** (`app/network/proxy.py`): per-provider profile,
  CDN-domain override (exact host or parent domain), and a JP failover chain that
  never falls back to `direct`.
- **Image pipeline** (`app/network/image_fetcher.py`): routed download, size cap,
  content-type allowlist (SVG/HTML rejected), Pillow decode + pixel-bomb guard,
  public-IP and per-redirect SSRF checks, content-addressed cache.
- **Asset storage and Telegram file_id cache** (`app/services/metadata.py`,
  `app/services/telegram_assets.py`): `assets` rows with dimensions/hash/proxy,
  dedupe by normalised URL and SHA-256, `file_id` cached per bot and per hash.
- **Translation** (`app/services/translate.py`): OpenAI-compatible, non-fatal on
  failure, result cached in `contents.description_zh`.

### What M4 adds

- **Push pipeline** (`app/services/publisher.py`): for every discovered content —
  enrich once, send the cover card (code / tag / title / size / time / Nyaa link),
  send the description and translation as follow-up messages when they do not fit a
  caption, send previews as media groups of 2–10, and record every message id.
  Resumable after a crash: a pending notification continues from the last recorded
  step.
- **Cards** (`app/services/cards.py`): the legacy layout — `<b>FC2-PPV-<number></b>`,
  `🏷️ tag`, title with `💾 size · 🕒 time`, `📝` description, `🇨🇳` translation and
  `<a href="…">nyaa</a>`. HTML parse mode, so every user-supplied string is escaped.
  A card that fits is sent complete; when it does not fit, the caption is compacted
  (title ≤200 chars) and the **complete** description/translation follow as text
  messages — no body text is lost. Short `dl:`/`blk:` callback data.
- **Buttons** (`app/bots/subscription.py`): download re-checks the gate, writes the
  accepted entry and the job in one transaction, submits to qBittorrent and settles
  the button to 已下载 / 重试; a second click is idempotent. 不感兴趣 writes the
  blacklist first and only then deletes the whole message group.
- **Two dispatchers** (`app/bots/wiring.py`): Telegram delivers a callback only to
  the Bot that sent the message, so the subscription Bot polls its own dispatcher
  (card callbacks) while the manual Bot polls commands and magnet ingest. Startup
  logs name which Bot polls and which only sends.
- **Bot factory and identity check** (`app/bots/factory.py`): both Bots are built
  with `telegram.proxy` (via `AiohttpSession`) and `getMe` is called once at startup,
  logging `telegram.bot_identity` or a loud `telegram.bot_unreachable`. A Bot that
  cannot reach Telegram is visible in seconds instead of looking idle.
- **Task supervisor** (`app/services/supervisor.py`): every background task is
  wrapped, failures are logged with the task name, restarts are bounded and
  exponential, and `/status` reports `running / restarting / dead / stopped`.
- **Group deletion** (`app/services/notifications.py`): cover, description and all
  preview groups are deleted together; Telegram failures are retried in the
  background and never roll back the blacklist (spec 9.4).

### What M5 adds

- **Transmission adapter** (`app/downloaders/transmission.py`): same protocol as
  qBittorrent (session handshake, add, get, list, remove with
  `delete-local-data: false`), disabled by default and selectable per route.
- **Admin commands** (`/block`, `/unblock`, `/pause`, `/resume`, `/providers`):
  blacklist management, a runtime pause switch for the poller, and a provider health
  report. `/unblock` keeps the accepted record on purpose.
- **Metrics** (`app/services/metrics.py`): per provider `hit / miss / structure /
  error` plus average latency and hit rate — a miss and a broken selector are
  different counters — and per proxy profile calls, failures, failure rate, region
  and last error. Exposed through `/status` and `/providers`.
- **Spec 22.3 integration tests** (`tests/integration/test_spec_22_3.py`): the seven
  end-to-end guarantees, one test per sentence of the specification.
- **Legacy import** (`scripts/import_legacy.py`): one-off adoption of the old
  fc2bot/torrentino state (pushed cards, dead records, blacklist, done/claims,
  pending log) and of existing Transmission tasks, with dry-run, idempotency,
  conflict reporting and a precise rollback.
- **Acceptance and deployment docs**: [`docs/acceptance.md`](docs/acceptance.md)
  maps all 19 checklist items to evidence; [`docs/deploy.md`](docs/deploy.md) walks
  through a from-zero installation.

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
.venv/bin/pytest              # unit + integration, no network
.venv/bin/pytest --run-live   # manual live smoke; needs LIVE_PROXY_URL on filtered networks
.venv/bin/ruff check .
.venv/bin/mypy app
```

Tests never touch the real network: qBittorrent is a fake (or a mocked HTTP
transport via `respx`), Telegram is a fake, Nyaa responses come from static
fixtures, and all paths live in `tmp_path`. `live` tests are skipped unless
`--run-live` is passed. The schema used by tests is created once per session by
running the real Alembic migration.

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
- First real run on the NAS: follow [`docs/runbook-smoke.md`](docs/runbook-smoke.md).

## Safety rules encoded in the code

- Gate order is fixed: blacklist → accepted → info hash → source item, and the
  check plus the `accepted_entries` write share one transaction. The gate is always
  evaluated **before** a release row is written.
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
  network/     HTTP client, proxy routing, image fetcher (SSRF + decode checks)
  organizer/   paths, mover, stability, monitor
  providers/   base plumbing + nine adapters + coordinator (field-level merge)
  schemas/     release / metadata / download models
  services/    normalize, gate, state, jobs, releases, subscription, manual ingest,
               metadata enrichment, translation, push queue, telegram assets
  sources/     source protocol + Nyaa adapter
config/example.yaml   configuration template
docker/entrypoint.sh  migrate, then exec the app (PID 1, graceful stop)
docs/                 specification, deviations log, NAS smoke runbook
scripts/backup.sh     daily backup helper
tests/                unit + integration + live (opt-in), all offline by default
```

## Configuration highlights added in M2

| Key | Default | Meaning |
|---|---|---|
| `fc2.code_min_digits` / `code_max_digits` | 5 / 8 | FC2 number width; outside this range nothing is treated as a content id |
| `nyaa.rss_path` / `search_path` | site-wide FC2 | Neither may carry an uploader filter |
| `nyaa.rss_page_size` | 75 | A full RSS page triggers the search catch-up |
| `nyaa.search_max_pages` | 5 | Pagination cap |
| `nyaa.overlap_item_ids` | 200 | Item ids remembered for the overlap re-check |
| `nyaa.trackers` | `[]` | Appended to magnets built from the RSS info hash |
| `provider.sources.<id>.proxy` | `japan` | Per-provider proxy profile |
| `provider.strict_enabled` | true | False allows disabling a provider (logged + audited) |
| `download.max_retries` | 5 | Automatic resubmission cap |
| `image_fetch.cache_dir` | `/app/data/cache/images` | Content-addressed image cache |
| `image_fetch.allowed_hosts` | `[]` | Strict image host allowlist; empty derives from provider registered domains |
| `translation.enabled` | true | Requires `TRANSLATION_BASE_URL` / `_API_KEY` / `_MODEL` |
| `image_fetch.max_previews` / `min_preview_images` | 9 / 3 | Card preview budget and early-stop threshold |
| `app.log_file` | `/app/data/logs/app.log` | Rotating log file on the data volume |
| `subscription.polling_enabled` | true | false = manual bot only, no Nyaa polling task |
| `subscription.max_push_per_round` | 10 | Cards per round, so a backlog drains gradually |
| `nyaa.initial_backfill_silent` | true | First-run backfill is recorded but never pushed |
| `downloaders.transmission.save_path_template` | `/downloads/{category}/{job_id}` | Same contract as the qBittorrent template |

`FC2_LOG_LEVEL` and `FC2_DATABASE_URL` are the only environment overrides
(env > yaml).

## Documentation

- [`docs/BT_AUTOMATION_SPEC.md`](docs/BT_AUTOMATION_SPEC.md) — the specification.
- [`docs/deviations.md`](docs/deviations.md) — every deliberate deviation, with reasons.
- [`docs/deploy.md`](docs/deploy.md) — from-zero deployment, backup and recovery.
- [`docs/runbook-smoke.md`](docs/runbook-smoke.md) — step-by-step acceptance run on the NAS.
- [`docs/acceptance.md`](docs/acceptance.md) — the spec 24 checklist with evidence.

## Known deviations

See [`docs/deviations.md`](docs/deviations.md). The short version:

1. pydantic-settings is not used; the YAML loader plus two env overrides is
   deliberate (nested validation stays traceable).
2. The spec's section 18 YAML example is not valid YAML; placeholders are quoted.
3. FC2 digit width is configuration, not a hardcoded constant.
4. `provider.strict_enabled` is the explicit, audited escape hatch for a broken
   source.
5. The gate runs before the release write; an already-accepted number still stores
   later uploaders as candidate sources.
6. `ACCEPTED` jobs are reconciled at startup with a retry cap.
7. Nyaa fixtures are constructed samples, never captured real pages.
8. Image host allowlist derives from provider registered domains unless set.
9. Translation is enabled by default and fails startup without credentials.
10. Five providers were verified against real pages (Fc2, Mmtv, Javdb, freejavbt,
    Madou); fc2_hub, Javlibrary, miss_av and avsox answer 403 / a JS challenge from
    the build environment and remain constructed samples.
11. Cards are pushed to every allowed user id; 不感兴趣 deletes that chat's group only.
12. Without a usable cover the card degrades to a text message instead of not being
    pushed at all.

## Licence

See [LICENSE](LICENSE).
