# 部署文档（从零开始）

目标读者：第一次接触本项目、在 Debian NAS 上部署的运维。照着做就能跑起来；每一步都写了「应该看到什么」，不符合就停在这一步排查。

前置：NAS 已装 Docker 与 Docker Compose（`docker compose version` 可用），qBittorrent 已在运行，能出网访问 `sukebei.nyaa.si`（需要代理的话见第 7 节）。

---

## 1. 拿到代码

```bash
git clone git@github.com:milikii/fc2-harbor.git /opt/fc2-harbor
cd /opt/fc2-harbor
```

目录里应该有 `Dockerfile`、`compose.yaml`、`config/example.yaml`、`.env.example`。

## 2. 创建两个 Telegram Bot

1. 在 Telegram 里找 **@BotFather**，发 `/newbot`，按提示起名（两个 Bot 各建一次）：
   - 第一个：**订阅推送 Bot**（发卡片、下载/不感兴趣按钮）
   - 第二个：**手动投递 Bot**（收 magnet、`/status`、`/retry`、`/block` 等命令）
2. 每次建完 BotFather 会给一个 token，形如 `123456789:AA...`。**只填进 `.env`，不要提交到任何地方。**
3. 拿到自己的 Telegram 数字 user id：
   - 在 Telegram 找 **@userinfobot**，发任意消息，它回复里的 `Id` 就是；
   - 或者给 `https://api.telegram.org/bot<你的token>/getUpdates` 发请求，看 `message.from.id`。
4. **先给两个 Bot 各发一次 `/start`**（私聊）。Telegram 不允许 Bot 主动给从未对话过的用户发消息，不先发会收不到卡片。

## 3. 准备环境变量（`.env`）

```bash
cp .env.example .env
${EDITOR:-vi} .env
```

逐项说明（★ = 必填）：

| 变量 | 必填 | 用途 |
|---|---|---|
| `MANUAL_BOT_TOKEN` ★ | 是 | 手动投递 Bot 的 token（第 2 步建的那个） |
| `SUBSCRIPTION_BOT_TOKEN` | 建议 | 订阅推送 Bot 的 token。留空则只记录订阅、不发卡片（M4 起使用） |
| `TELEGRAM_ALLOWED_USER_IDS` ★ | 是 | 允许使用两个 Bot 的用户 id，逗号分隔，例如 `12345678` 或 `12345678,87654321`。**这是唯一的身份校验，别写错** |
| `QBITTORRENT_URL` ★ | 是 | qBittorrent Web API 地址，容器内要能访问，例如 `http://192.168.1.10:8080` |
| `QBITTORRENT_USERNAME` ★ | 是 | qB 用户名 |
| `QBITTORRENT_PASSWORD` ★ | 是 | qB 密码 |
| `TRANSMISSION_URL` / `_USERNAME` / `_PASSWORD` | 否 | 只有启用 Transmission 时才需要（默认关闭） |
| `TRANSLATION_BASE_URL` / `_API_KEY` / `_MODEL` | 仅 `openai_compatible` | OpenAI 兼容翻译端点。`translation.provider: openai_compatible` 且 `enabled: true` 时必填，否则启动会报错并列出缺的变量。默认的 `google_free` **不需要**这三个 |
| `JP_PROXY_URL` / `JP_BACKUP_PROXY_URL` | 视网络 | 日本出口代理（`socks5h://user:pass@host:1080`）。不需要日本代理时，把 `config.yaml` 里各 provider 的 `proxy` 改成 `direct` |
| `FC2_LOG_LEVEL` | 否 | 覆盖 `app.log_level`（DEBUG/INFO/WARNING/ERROR） |
| `FC2_DATABASE_URL` | 否 | 覆盖数据库路径（一般不用改） |
| `FC2_CONFIG` | 否 | 配置文件路径，默认 `/app/config/config.yaml`（compose 已设） |

## 4. 准备配置（`config/config.yaml`）

```bash
cp config/example.yaml config/config.yaml
${EDITOR:-vi} config/config.yaml
```

必须确认的几项：

1. **`storage.path_mappings`**：`remote` 是 **qBittorrent 看到的路径**，`local` 是 **容器内路径**。
   默认两边都是 `/downloads`（compose 把 NAS 的 `/mnt/storage/downloads` 挂到容器的 `/downloads`），一般不用改。
   如果你在 qB 里把下载目录设成了别的（例如 `/downloads2`），就在这里加一条映射；**未映射的路径会让任务失败，系统不会猜**。
2. **`storage.final_roots`**：整理后的媒体目录，容器内路径。默认 `/library/fc2` 等，对应 NAS 的 `/mnt/storage/library`。
3. **`downloaders.qbittorrent.save_path_template`**：每个任务独立的保存目录，默认 `/downloads/{category}/{job_id}`。`{category}`/`{job_id}` 两个占位符必须保留。
4. **`image_fetch.allowed_hosts`**：图片域名 allowlist。留空表示按各 provider 的注册域自动派生（`fc2.com`、`javdb.com`…）。想更严格就显式列出（示例配置里有注释好的列表）。
5. **`translation`**：默认 `provider: google_free`（免密钥 Google 端点），只需 `enabled: true` 和 `proxy: japan`——**必须走代理**，直连会超时。不想翻译就 `enabled: false`。
   想用 OpenAI 兼容端点就改成 `provider: openai_compatible` 并配好 `TRANSLATION_BASE_URL/_API_KEY/_MODEL`，缺一个启动就会 fail-fast。
   翻译失败（代理不通、被限流、返回 HTML「Sorry...」页）**不会**影响卡片发送：只记 `metadata.translate_failed`，卡片里没有 `🇨🇳` 那段，日文简介照常显示。
6. **`provider.sources.*.proxy`**：默认 `japan`。没有日本代理就全改成 `direct`，否则 provider 请求会失败。
7. **`fc2.code_min_digits` / `code_max_digits`**：FC2 编号位数，默认 5/8，一般不用动。
8. **`subscription.polling_enabled`**：`false` 时**完全不启动** Nyaa 轮询，只跑手动 Bot（适合先只用手动投递）。
9. **`subscription.max_push_per_round`**：每轮最多推几张卡片，默认 10。有积压时逐轮消化，不会一次刷屏。
10. **`nyaa.initial_backfill_silent`**：默认 `true`。首次启动回填 24h 的内容会被标记为「回填」，**不进推送队列**；只有第一轮成功之后新发现的内容才会推送。想连回填一起推就设 `false`（会一次性收到大量卡片）。

## 5. 目录与权限（最容易踩的坑）

容器以 **uid/gid 1000** 运行（非 root），并且要和 qBittorrent 共用下载目录：

```bash
sudo mkdir -p /mnt/storage/downloads /mnt/storage/library
sudo chown -R 1000:1000 /mnt/storage/downloads /mnt/storage/library
```

- 如果 qBittorrent 以别的用户运行，把这里的 `1000:1000` 换成 qB 的 uid/gid，并相应修改镜像的 `USER`（或给这两个目录 `chmod g+w` 并加组）。
- 判据：`docker compose exec app sh -c 'touch /downloads/.probe && rm /downloads/.probe'` 不报错。
- SQLite 必须放在本地盘（`./data`），**不要放到 NFS/SMB**，WAL 在网络上会出问题。

## 5b. 构建时需要代理时

镜像构建阶段要访问 PyPI（`files.pythonhosted.org`）。如果 NAS 或构建机直连不通，把代理作为**构建参数**传进去，不要写进 `.env`：

```bash
docker compose build \
  --build-arg HTTP_PROXY=http://192.168.1.220:7890 \
  --build-arg HTTPS_PROXY=http://192.168.1.220:7890 \
  --build-arg NO_PROXY=localhost,127.0.0.1,192.168.0.0/16
docker compose up -d
```

或者取消 `compose.yaml` 里 `build.args` 的注释后直接 `docker compose up -d --build`。

- 参数为空时不影响直连构建（Dockerfile 里声明为空字符串）。
- 这些变量只存在于构建阶段；运行中的容器仍按 `config.yaml` 显式路由（不读环境里的 `HTTP_PROXY`）。
- 若用私有 PyPI 镜像，同样在这里加 `--build-arg PIP_INDEX_URL=...`（需在 Dockerfile 里补对应 ARG）。

## 6. 启动

```bash
docker compose up -d --build
docker compose logs -f app
```

首次启动会依次看到：

1. `db.migrate.start` → `db.migrate.done`：建表/升级数据库
2. `downloader.registry.ready`：qB 适配器就绪（此时还没连 qB）
3. `monitor.reconcile`：扫描未完成任务并对账
4. `app.ready`：任务列表与 Bot 分工。会看到
   `polling_bots={"manual-bot":"manual","subscription-bot":"subscription"}`——
   **两个 Bot 各自 poll 自己的 update**（卡片按钮的回调只会投递给发卡片的那个 Bot），
   `sends_cards=["subscription-bot"]`、`deletes_messages=["subscription-bot"]`。
5. `subscription.started`：开始按 180s + 抖动轮询 Nyaa
6. `telegram.bot_identity`：两个 Bot 各一条，带 `username` 和 `telegram_id`——**这条不出现就说明 Bot 连不上 Telegram**，会同时看到 `telegram.bot_unreachable`（提示检查 `telegram.proxy` 与网络）

第一轮会回看最近 24 小时，`subscription.round` 日志里 `fetched`/`recorded` 应该大于 0（站点上确实有 FC2 资源时）。

## 7. 需要代理时

应用**不读**环境里的 `HTTP_PROXY`（这是有意的，见规格 §8.2）。代理只在配置里显式指定：

```yaml
proxy_profiles:
  direct: {type: direct}
  japan: {type: socks5h, url: "${JP_PROXY_URL}", region: JP}
  japan_backup: {type: http, url: "${JP_BACKUP_PROXY_URL}", region: JP}
```

- provider 用 `proxy: japan`；日本源只会在日本出口之间故障转移，**不会自动回退直连**。
- **两个 Telegram Bot 也走 `telegram.proxy`**：把它指向一个可用的 profile（例如 `japan`），Bot 的 aiohttp session 就会带代理；设 `direct` 则直连。容器内 `api.telegram.org` 若被解析到不可达地址，直连会一直失败，而进程仍然「健康」。
- 图片按 provider 继承代理，可用 `image_fetch.cdn_routes` 按 CDN 域名覆盖。
- qBittorrent、Transmission、NAS 内网地址永远直连。

## 8. 验证健康

```bash
# 存活（不查数据库）
docker compose exec app curl -fsS http://127.0.0.1:8080/healthz
# 就绪（查数据库）
docker compose exec app curl -fsS http://127.0.0.1:8080/readyz
# 状态：任务数、内容数、provider 命中率、代理失败率
docker compose exec app curl -fsS http://127.0.0.1:8080/status | python3 -m json.tool
```

`/status` 里的 `tasks` 字段显示每个后台任务的状态（`running / restarting / dead / stopped`）；
某个 polling 任务挂掉后会自动重启（最多 3 次，指数退避），仍失败则标 `dead` 并打 ERROR 日志。

`/status` 里的 `metrics.providers` 每个源给出 `hit / miss / structure / error / average_latency_ms / hit_rate`：

- `miss` 多但 `structure=0`：站点没有这个编号，正常；
- `structure` 涨：页面结构变了，selector 要修（日志 `metadata.provider_unhealthy`）；
- `error` 涨：网络/403/超时，先查代理与站点可达性。

`metrics.proxies` 给出每个代理的 `region / calls / failures / failure_rate / last_error`。

Telegram 侧：给手动 Bot 发 `/status`、`/providers`，应能收到回复。

## 9. 备份与恢复

备份（建议 cron 每天一次）：

```bash
cd /opt/fc2-harbor
./scripts/backup.sh /mnt/storage/backups
```

- 数据库用 SQLite **在线备份 API**（`src.backup(dst)`），运行中也能安全备份；
- 同时打包 `config/`；日志在 `data/logs/`（滚动文件），随 `data/` 一起保留；
- 每类保留最近 14 份。

恢复：

```bash
cd /opt/fc2-harbor
docker compose down
cp /mnt/storage/backups/app-<时间戳>.db data/app.db
tar -xzf /mnt/storage/backups/config-<时间戳>.tar.gz -C .
docker compose up -d
docker compose logs -f app   # 应看到 db.migrate.done 且没有报错
```

`data/app.db-wal` / `-shm` 在恢复前先删掉，避免和旧 WAL 混用。

## 9b. 从旧版本升级

`docker compose up -d --build` 时 entrypoint 会自动执行 `alembic upgrade head`。

- 0002 迁移会给 `contents` 加 `backfilled` 列，并**把所有已存在的内容标记为回填**，避免升级后把积压的几百条一次性推给用户。
- 升级前建议先备份（`./scripts/backup.sh`）；迁移只前进，回退需要手动 `alembic downgrade` 并恢复数据。
- 升级后看日志 `app.ready` 里的 `subscription_polling`、`max_push_per_round`、`initial_backfill_silent` 三项，确认与配置一致。

## 9c. 接管旧系统数据（一次性导入）

旧系统（fc2bot / torrentino）的状态可以用 `scripts/import_legacy.py` 接管。**先停应用**，避免导入期间轮询写库：

```bash
docker compose stop app
docker compose run --rm app python scripts/import_legacy.py --dry-run
```

`--dry-run` 走的是同一套代码，只是最后回滚事务，所以打印的数字就是真实结果。确认后去掉 `--dry-run` 再跑一次：

```bash
docker compose run --rm app python scripts/import_legacy.py
docker compose start app
```

默认读取 NAS 上的 `/home/docker/torrentino-fc2/config/` 与 `/home/docker/fc2-shared/`（可用 `--state/--blacklist/--dead/--done/--claims/--pending` 覆盖）。各文件去向：

| 文件 | 去向 |
|---|---|
| `fc2_state.json`（`messages` 非空） | `contents` + `notifications`（`bot_name='legacy:torrentino'`、`status='legacy'`），不会再推 |
| `fc2_dead.jsonl` | `notifications`（`status='legacy:dead'`），**不进黑名单**——用户没否掉它们 |
| `fc2_blacklist.json` | `blacklist_entries`（`reason='legacy_import'`） |
| `fc2_done.jsonl` | `accepted_entries`（`origin='legacy_import'`）+ 有 hash 时写 `releases`（`source='legacy'`） |
| `fc2_claims.jsonl` | `accepted_entries` |
| `fc2_done.jsonl.pending` | **不导入**：它是旧系统「删卡片」队列（`DoneDeletionQueue`），不是下载记录，导进去会误挡内容；只统计行数写进报告 |
| `retry` 记录 | **不导入**：临时失败还会重试，不能当已处理 |
| `--since 5007844` | 只导入编号 >= 该值的记录 |

报告写在 `import-report.json`：按来源分组（`torrentino_pushed` / `torrentino_dead` / `torrentino_done` / `torrentino_claims` / `torrentino_pending` / `torrentino_blacklist` / `transmission`）给出创建数、跳过数、冲突数，另有 `conflicts` 明细和 `created_content_keys`（回滚用）。

## 9d. 接管 tr-bt 的 Transmission 任务

先让运维导出 JSON（每项至少要有 `name`、`hash`；`code`、`download_dir`、`size_bytes`、`status`、`qb_save_path` 按需）：

```json
[{"name": "FC2-PPV-5007851 标题", "hash": "2538176d…", "code": "5007851",
  "download_dir": "/9KG", "size_bytes": 1234567, "status": "finished"}]
```

**先看计划**（只统计、不写库）：

```bash
docker compose run --rm app python scripts/import_legacy.py \
  --transmission-migrate finished \
  --transmission-export /tmp/tr-export.json --dry-run
```

计划里每个任务给出：编号、分类、目标路径、大小、动作（`organize` / `takeover` / `skip`），以及总计的「完成/跳过/冲突/总字节数」和按分类的目录汇总。确认后去掉 `--dry-run` 执行。

- `--mode finished`：建 `download_jobs`，`status=DOWNLOADED`、`downloader=transmission`。随后**监控与整理器走真实代码路径**：按分类移动到 `/library/<category>/`、校验大小、成功后才删 tr 任务（`deleteFiles=false`）。需要 `downloaders.transmission.enabled: true`，否则监控取不到任务。
- `--mode incomplete`：这些已由运维加进 qB，只建行：`status=DOWNLOADING`、`downloader=qbittorrent`、`remote_save_path` 取导出里的 `qb_save_path`（**不猜**）。之后监控接管，下完自动整理。
- 分类：导出里给了 `category` 就用它；FC2 编号默认 `fc2`；**提取不到 FC2 编号的任务一律进 `review`（`_review` 目录）**，绝不猜分类（见下一条）。
- **没有 FC2 编号的任务**（例如 `(HD1080P)(美女遊記)(fc4306363.45qi7skt)…`）不再被跳过：
  - `canonical_key = infohash:<sha1>`、`content_type = generic`、`display_code` 用清洗后的种子名（去掉 `(HD1080P)`、`(fc4306363.45qi7skt)` 这类分辨率/站点 id 噪声，去掉扩展名，最长 120 字）；
  - 分类固定 `review`，`releases.source_item_id` 用 hash，同时写 `accepted_entries`（`origin='legacy:transmission'`），门禁按 info hash 就能拦住重复下载；
  - 报告里单独归一组 `transmission_generic`（`by_group`），不和 FC2 编号混在一起；
  - 既没有编号**也没有 hash** 的任务无法标识，跳过并记 `missing_hash`。
- 导出里的 `error` / `percent_done` 会参与判定：
  - `error=1`（tracker 拒绝该 hash）且 `percent_done=1.0` → 数据完整，按 finished 正常整理；
  - `error=3`（Piece is corrupt）→ 先让运维在 tr 里 force recheck；recheck 后仍 `percent_done=1.0` 才按 finished，掉到 1.0 以下按 incomplete。按 finished 处理时报告会写一条 `error_3_piece_corrupt_verify_recheck` 警告；
  - `percent_done` 缺失时退回 `status` 字段，两者都没有则跳过（`unknown_status`）。
- 分批处理：`--only-hashes hashes.txt`（每行一个 hash，`#` 开头为注释）只处理指定任务，避免一次全跑。
- 幂等：已有同 hash 的 job、或该编号已 accepted 的，跳过并记入冲突清单（例如用户正在下的 `fc2_ppv:4744486` 不会被碰）。
- 报告写在 `migration-report.json`。

## 9e. 回滚导入

导入的每一行都有标记，按标记删除即可（先删 job，再删被引用的行）：

```sql
-- 在容器里执行：docker compose exec app python -c "import sqlite3; ..."
DELETE FROM download_jobs
 WHERE release_id IN (SELECT id FROM releases WHERE source IN ('legacy','transmission'));
DELETE FROM notifications WHERE bot_name = 'legacy:torrentino';
DELETE FROM blacklist_entries WHERE reason = 'legacy_import';
DELETE FROM accepted_entries WHERE origin LIKE 'legacy%';
DELETE FROM releases WHERE source IN ('legacy','transmission');
```

上面这一步是安全的：只删导入产生的行，不动用户自己的数据。导入时**新建的 contents** 不会自动删除（删错会丢用户数据），需要时用报告里的编号清单精确清理：

```bash
docker compose run --rm app python scripts/import_legacy.py \
  --rollback-contents import-report.json
```

它只删「没有任何 accepted/blacklist/notification/release/job 引用」的内容，被引用的会保留并计数。

## 10. 首次冒烟

按 `docs/runbook-smoke.md` 走一遍：订阅有记录、卡片能收到、点下载能进 qB、下载完能整理、点不感兴趣能删消息并拉黑。那份文档是逐步验收脚本，不是概念说明。

## 11. 常见故障

| 现象 | 排查 |
|---|---|
| 启动即退出，日志 `configuration error` | 按提示补 `.env` 变量；`translation.provider: openai_compatible` + `enabled: true` 时必须配三个 `TRANSLATION_*`；`google_free` 只需要代理 |
| `downloader_auth_failed` | qB 用户名/密码错（401 或 4.x 的 `Fails.`），或 qB 开了「本地认证绕过」而地址不对 |
| `downloader_banned` | qB 因多次失败封了这个 IP（403），去 qB 设置里解封或换地址 |
| `downloader_login_unexpected` | 登录返回了既不是 204 也不是 `Ok.` 的响应，日志里有 `status` 与 `body_bytes`，按 qB 版本核对 |
| `downloader_unreachable` | 容器访问不到 `QBITTORRENT_URL`；用 `docker compose exec app wget -qO- <url>` 试 |
| 卡片收不到 | 两个 Bot 都先 `/start`；`TELEGRAM_ALLOWED_USER_IDS` 是否含你的 id；`SUBSCRIPTION_BOT_TOKEN` 是否填了 |
| 卡片收到但按钮点了没反应 | 确认 `app.ready` 里有 `subscription-bot` 任务、`polling_bots` 包含它；按钮回调只会投递给发卡片的 Bot，订阅 Bot 没 poll 就永远收不到 |
| 容器 healthy 但 Bot 完全没反应 | 看启动日志有没有 `telegram.bot_identity`；有 `telegram.bot_unreachable` 就是连不上（多半是 `telegram.proxy` 没配或代理不通）；`/status` 的 `tasks` 里若 polling 任务是 `dead` 也在这里体现 |
| `organize_failed`（stage `conflict`） | 目标目录已有同名但**大小不同**的文件。系统不会覆盖它：确认是要保留旧文件（改名/删掉旧文件）还是放弃这次整理，然后 `/retry <job_id>` |
| `organize_failed`（其它 stage） | 目标目录权限/磁盘满/跨盘复制失败；日志有 stage 与路径，源文件与任务都还在，修好后 `/retry <job_id>` |
| 整理很慢、日志有 `organizer.rename_fallback reason=exdev` | `/downloads` 与 `/library` 是同一块盘但不同的 bind mount，内核拒绝 rename，已自动回退复制（慢但正确）。想让 rename 生效就改成单一 mount + `/media` 前缀 |
| `content_path_unmapped` | `storage.path_mappings` 没覆盖 qB 报的路径，补映射 |
| provider 全 `error` | 代理不通或站点 403；看 `/providers` 与 `metrics.proxies` |
| `structure` 计数上涨 | 站点改版，按 `docs/deviations.md` §19 的流程补 fixture + 改 selector |
| 卡片里没有 `🇨🇳` 译文 | 看 `metadata.translate_failed` 的 `error_code`：`non_json` 说明出口被 Google 拦（返回 HTML「Sorry...」页，换出口）；`network` 是代理不通；`length_mismatch`/`bad_response` 是端点换了格式。卡片本身不受影响 |
| 卡片发出去但 Telegram 报 `can't parse entities` | 只可能来自我们自己拼的 HTML：检查是不是有人改了 `app/services/cards.py` 却没走 `escape_html()`；测试 `test_markup_in_content_is_escaped_before_sending` 会挡住这种回归 |
