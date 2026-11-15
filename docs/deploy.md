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
| `TRANSLATION_BASE_URL` / `_API_KEY` / `_MODEL` | 视配置 | OpenAI 兼容翻译端点。`config.yaml` 里 `translation.enabled: true` 时必填，否则启动会报错并列出缺的变量 |
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
5. **`translation.enabled`**：有翻译端点填 `true` 并配好三个 `TRANSLATION_*` 变量；**不想用翻译就显式改成 `false`**，否则启动会因为缺凭据失败（这是有意的 fail-fast）。
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

## 10. 首次冒烟

按 `docs/runbook-smoke.md` 走一遍：订阅有记录、卡片能收到、点下载能进 qB、下载完能整理、点不感兴趣能删消息并拉黑。那份文档是逐步验收脚本，不是概念说明。

## 11. 常见故障

| 现象 | 排查 |
|---|---|
| 启动即退出，日志 `configuration error` | 按提示补 `.env` 变量；`translation.enabled: true` 时必须配三个 `TRANSLATION_*` |
| `downloader_auth_failed` | qB 用户名/密码错，或 qB 开了「本地认证绕过」而地址不对 |
| `downloader_unreachable` | 容器访问不到 `QBITTORRENT_URL`；用 `docker compose exec app wget -qO- <url>` 试 |
| 卡片收不到 | 两个 Bot 都先 `/start`；`TELEGRAM_ALLOWED_USER_IDS` 是否含你的 id；`SUBSCRIPTION_BOT_TOKEN` 是否填了 |
| 卡片收到但按钮点了没反应 | 确认 `app.ready` 里有 `subscription-bot` 任务、`polling_bots` 包含它；按钮回调只会投递给发卡片的 Bot，订阅 Bot 没 poll 就永远收不到 |
| `organize_failed` | 目标目录权限/磁盘满/跨盘复制失败；日志有 stage 与路径，源文件与任务都还在，修好后 `/retry <job_id>` |
| `content_path_unmapped` | `storage.path_mappings` 没覆盖 qB 报的路径，补映射 |
| provider 全 `error` | 代理不通或站点 403；看 `/providers` 与 `metrics.proxies` |
| `structure` 计数上涨 | 站点改版，按 `docs/deviations.md` §19 的流程补 fixture + 改 selector |
