# NAS 真实环境冒烟 Runbook（M2 之后）

目的：在真实 NAS 上验证「订阅发现 → 门禁 → qBittorrent 下载 → 稳定判定 → 整理 → 删任务」这条链路，不使用真实 FC2 内容，不把任何凭据写进仓库。

执行人：运维。预计 20 分钟（不含下载等待）。

---

## 0. 前置条件

- NAS 上已装 Docker + Docker Compose。
- qBittorrent 已在运行，且能从 NAS 本机访问 Web UI。
- 下载目录 `/mnt/storage/downloads`、媒体目录 `/mnt/storage/library` 已存在。
- 一个 Telegram 私聊 Bot（@BotFather 创建），以及自己的 Telegram user id。
- 出网：能访问 `sukebei.nyaa.si`（M2 只用 direct；日本代理是 M3 的事）。

## 1. 准备配置

```bash
git clone git@github.com:milikii/fc2-harbor.git /opt/fc2-harbor
cd /opt/fc2-harbor
cp .env.example .env && ${EDITOR:-vi} .env
cp config/example.yaml config/config.yaml && ${EDITOR:-vi} config/config.yaml
```

`.env` 必填：

| 变量 | 说明 |
|---|---|
| `MANUAL_BOT_TOKEN` | 手动投递 Bot token |
| `TELEGRAM_ALLOWED_USER_IDS` | 允许使用的 Telegram 用户 id，逗号分隔 |
| `QBITTORRENT_URL` / `QBITTORRENT_USERNAME` / `QBITTORRENT_PASSWORD` | NAS 上的 qB Web API |
| `TRANSLATION_BASE_URL` / `TRANSLATION_API_KEY` / `TRANSLATION_MODEL` | OpenAI 兼容翻译端点；没有就设 `translation.enabled: false` |
| `SUBSCRIPTION_BOT_TOKEN` | 订阅推送 Bot（M4 起使用；留空则只跑手动 Bot 与订阅记录） |

`config/config.yaml` 必改：

- `storage.path_mappings`：`remote` 是 qBittorrent 看到的路径，`local` 是容器内路径（compose 里都是 `/downloads`，一般不用改）。
- `storage.final_roots`：媒体目录，默认 `/library/<category>`。
- `image_fetch.cache_dir`：图片缓存，默认 `/app/data/cache/images`（随 `./data` 卷持久化）。
- `downloaders.qbittorrent.url`：容器内能访问到的 qB 地址（NAS 局域网 IP）。

权限：容器以 uid/gid 1000 运行，下载目录必须与 qBittorrent 的属主一致：

```bash
sudo chown -R 1000:1000 /mnt/storage/downloads /mnt/storage/library
```

## 2. 起容器

```bash
docker compose up -d --build
docker compose logs -f app
```

期望看到：`db.migrate.done` → `downloader.registry.ready` → `monitor.reconcile` → `app.ready` → `subscription.started`。

健康检查：

```bash
docker compose exec app curl -fsS http://127.0.0.1:8080/readyz
# {"status":"ready","database":"ok"}
docker compose exec app curl -fsS http://127.0.0.1:8080/status
```

## 3. 订阅冒烟（M2 核心）

> 如果 NAS 出网需要代理：在 `config/config.yaml` 里把 `nyaa.proxy` 指向一个
> `http`/`socks5h` profile（socks5h 已内置 `httpx[socks]`），不要依赖容器环境里的
> `HTTP_PROXY`——应用按规格 §8.2 显式路由，`trust_env=False`。
>
> 想在本机先验证解析器：`LIVE_PROXY_URL=http://<proxy>:7890 .venv/bin/pytest tests/live --run-live`。
> 没有 `LIVE_PROXY_URL` 时 live 用例自动 skip。

- 等一轮（默认 180 秒 + 0～20 秒抖动）。日志出现 `subscription.round`，首次启动应满足 `fetched > 0`、`recorded > 0`（首次回看 24 小时）。
- 再等一轮，若出现 `nyaa.not_modified`，说明 ETag/Last-Modified 生效。
- `curl -s http://127.0.0.1:8080/status`（容器内）确认 `contents` 增长。
- 失败判据：`subscription.round_failed` 连续出现、`consecutive_failures` 递增 → 先查 NAS 出网，不要反复重启容器。

## 4. 手动投递冒烟（不碰真实内容）

本地造一个 1 MiB 测试种子：

```bash
mkdir -p /tmp/smoke && head -c 1048576 /dev/urandom > /tmp/smoke/payload.bin
docker run --rm -v /tmp/smoke:/data -w /data alpine:3 sh -c \
  'apk add -q mktorrent && mktorrent -a udp://tracker.opentrackr.org:1337/announce -o smoke.torrent payload.bin'
```

取 info hash（`mktorrent` 会打印，或用 `transmission-show /tmp/smoke/smoke.torrent`），然后对 Bot 私聊发送：

```
magnet:?xt=urn:btih:<INFO_HASH>&dn=FC2-PPV-9999999
```

期望结果：

1. Bot 回复「已加入下载队列：FC2-PPV-9999999」。
2. qBittorrent 出现任务，保存路径为 `/downloads/fc2/<job_id>`，分类 `fc2-harbor`。
3. 日志依次出现 `job.submitted` → `monitor.downloaded`（两次稳定观测）→ `monitor.organized` → `monitor.task_removed`（`delete_files=False`）。
4. `/mnt/storage/library/fc2/FC2-PPV-9999999/payload.bin` 存在；`/mnt/storage/downloads/fc2/<job_id>` 已清空；qB 任务消失但文件保留。

> 公网 tracker 可能没人做种。要稳定跑完，可在 NAS 的 qB 里添加同一个种子并保持做种，或直接在该 qB 上做种后再投递磁链。

## 4b. 卡片冒烟（M4）

1. 订阅轮询发现新内容后（或手动投递一条后等下一轮），Telegram 收到：
   封面图 + caption（有码/无码标签、标题、`FC2-PPV-<编号>`、Nyaa 链接），
   随后是完整日文/中文简介（若 caption 放不下）与预览图组（2–10 张一组）。
2. 点「下载」→ 按钮变「已下载」，qB 出现任务；再点一次 → 提示已处理，qB 不新增任务。
3. 换一条内容点「不感兴趣」→ 封面、简介、全部预览图组被删除，编号进入黑名单；
   再用手动 Bot 发同编号 → 拒绝。
4. 重启容器 → 已发送的卡片不会重复推送（`notifications` 里是 sent）。

## 5. 门禁冒烟

- 再发一次同一磁链 → 回复「该内容此前已接受过」，qB 不出现第二个任务。
- 加黑名单（`/block` 在 M5，这里直接改库）：

```bash
docker compose exec app python - <<'PY'
import sqlite3, uuid, datetime
conn = sqlite3.connect('/app/data/app.db')
conn.execute(
    "insert into blacklist_entries (id, canonical_key, reason, created_by, created_at) "
    "values (?, ?, ?, ?, ?)",
    (str(uuid.uuid4()), 'fc2_ppv:9999999', 'smoke', 'ops', datetime.datetime.now(datetime.UTC).isoformat()),
)
conn.commit()
PY
```

再发同一编号（换一个 info hash）→ 回复「该编号在黑名单中，已拒绝投递」。

## 6. 失败路径冒烟（可选，但建议做一次）

1. `sudo chmod 555 /mnt/storage/library/fc2`，再投递一个新的测试磁链。
2. 下载完成后日志出现 `organize_failed`；确认：qB 任务仍在、源文件仍在、Telegram 收到管理通知。
3. `sudo chmod 755 /mnt/storage/library/fc2`，在 Bot 里 `/retry <job_id>` → 任务继续整理并最终 `TASK_REMOVED`。

## 7. 清理

```bash
docker compose down
sudo rm -rf /mnt/storage/library/fc2/FC2-PPV-9999999
rm -rf data/app.db data/app.db-wal data/app.db-shm
```

qB 里的测试任务请手动删除（**不要**勾选「删除文件」）。Telegram 里的测试消息自行删除。

## 8. 通过标准

- [ ] `/readyz` 返回 200。
- [ ] 至少一轮 `subscription.round` 有 `recorded > 0`，无 `round_failed`。
- [ ] 手动投递走完「下载 → 稳定 → 整理 → 删任务」全链路。
- [ ] 整理后 qB 任务消失、媒体文件保留。
- [ ] 重复投递被 `accepted_entries` 拦截，qB 无第二个任务。
- [ ] 黑名单编号被拒绝。
- [ ] `docker compose logs` 中无明文 token、密码、代理认证。
- [ ] `docker compose restart app` 后 `monitor.reconcile` 不重复投递已有任务。

## 9. 回滚

```bash
docker compose down
# 需要回到上一个镜像时
docker tag fc2-harbor:previous fc2-harbor:latest && docker compose up -d
```

数据库迁移只前进不自动回滚；如需回滚 schema：`docker compose run --rm app alembic downgrade -1`（会丢该版本新增的表数据，先备份 `data/`）。
