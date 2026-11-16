# 与规格书的偏离与决策记录

本文件记录实现过程中对 `docs/BT_AUTOMATION_SPEC.md` 的**有意偏离**、规格书未覆盖处的**保守取舍**，以及每条的理由。凡是与规格书冲突的地方，以本文件的记录为准，并应在此处新增条目而不是默默改代码。

格式：`条目 / 规格书位置 / 实现 / 理由`。

## 规格书版本记录

| 规格书版本 | 入库日期 | 说明 |
|---|---|---|
| v1.0 | 2026-11-14 | 从 `fc2-harbor-docs/BT_AUTOMATION_SPEC.md` 复制入库，共 1019 行 |

约定：规格书更新时**直接覆盖**仓库内的 `docs/BT_AUTOMATION_SPEC.md`，并在上表追加一行版本号与日期，不静默替换。

---

## 1. 未使用 pydantic-settings

- 规格书：§3「Pydantic v2 + pydantic-settings：配置校验」
- 实现：pydantic v2 模型 + 自定义 YAML 加载器（`app/config.py`），`${ENV_VAR}` 递归展开后交给 `model_validate`。
- 理由：配置是嵌套结构（provider 顺序、路由、代理引用），双来源（YAML + 通用 env）会让「改顺序启动失败」这类校验难以追溯。仅保留两个容器部署常用覆盖项，见第 2 条。
- 结论：pydantic-settings 不引入；如后续需要更多 env 覆盖，只加白名单，不加通用层。

## 2. 只允许两个环境变量覆盖

- 实现：`FC2_LOG_LEVEL` → `app.log_level`，`FC2_DATABASE_URL` → `app.database_url`，优先级 env > yaml，启动日志记录 `env_overrides`。
- 理由：容器部署常改这两项；其余仍以 YAML 为唯一结构来源。

## 3. §18 示例 YAML 本身不可解析

- 规格书：§18 示例中 `japan: {type: socks5h, url: ${JP_PROXY_URL}, region: JP}`
- 问题：YAML flow mapping 里未加引号的 `${...}` 会被当作嵌套映射，`yaml.safe_load` 直接报错（文档 bug）。
- 实现：`config/example.yaml` 中所有占位符加引号，例如 `url: "${JP_PROXY_URL}"`。
- 结论：修正示例，占位符语义不变。

## 4. FC2 编号位数做成配置

- 规格书：§6.3 列出四种格式，未写位数范围
- 实现：`fc2.code_min_digits` / `fc2.code_max_digits`，默认 5 / 8；范围外不认作编号，也不猜测。
- 理由：规格书没给范围，写死会让运维无法在不改代码的情况下适配新编号。

## 5. provider.strict_enabled

- 规格书：§7.1「全部 enabled」、§25.4「顺序不得按命中率改写」
- 实现：默认 `provider.strict_enabled: true`，禁用任一 provider 启动即失败；显式设为 `false` 才允许禁用，且启动打 WARNING、把被禁用的 provider 列表写入 `decision_events`（`event_type=provider.disabled`）。
- 理由：运维需要能临时关掉一个坏源，但默认不放松；顺序与主机名的校验不受该开关影响。

## 6. 门禁必须先于 release 写入（§11.1 第 4 步与 §6.4 的张力）

- 规格书：§11.1 第 4 步「source + source_item_id 是否已经处理」；§6.4「后续 release 只补充候选下载源，不再次通知」
- 实现：
  - 门禁永远在写 `releases` **之前**评估（M1 契约，M2 遵守）。
  - 同一 `canonical_key` 已在 `accepted_entries` 时，新的 source item 仍写 `releases`（候选下载源），但不建 job、不写 accepted、不推送，门禁返回 `ALREADY_ACCEPTED`，由 `ReleaseRegistrar` 区分「拒 job 但记 release」。
  - 黑名单编号不写任何 content/release，只写审计事件，避免通过新上传者「复活」。
- 理由：这是两节规格唯一的冲突点，按「内容层去重、发布层留档」解。

## 7. ACCEPTED 纳入启动对账 + 重投上限

- 规格书：§13 启动扫描 SUBMITTED / DOWNLOADING / DOWNLOADED / ORGANIZING
- 实现：额外重投 `ACCEPTED`（覆盖「accepted 已提交、qB 未调用」的崩溃窗口）；每次自动重投 `retry_count += 1`，达到 `download.max_retries`（默认 5）标 `FAILED`（`error_code=max_retries_exceeded`）并发管理通知。
- 理由：不无限重投「永远加不上的任务」，同时保留 accepted 记录（§11.3 不重复推送）。

## 8. translation.enabled=true 在 M3 之前直接拒绝启动

- 规格书：§18 示例 `translation.enabled: true`
- 实现：M1/M2 期间若为 true 直接 `ConfigError`，示例配置默认 false。
- 理由：宁可启动失败，也不静默跳过翻译。

## 9. 手动 Bot 的待选分类只存内存

- 规格书：§10.8「分类不明确时展示分类按钮；未确认前进入 `_review`」
- 实现：待确认提交存在进程内（TTL 15 分钟），重启丢失；因为未确认前不写库，重发即可。
- 理由：不为一个未确认的临时状态引入持久化；M5 再评估。

## 10. Nyaa fixture 是构造样本

- 规格书：§22.2「每个 provider 使用静态 HTML fixture」
- 实现：`tests/fixtures/nyaa/` 下的 RSS/HTML 均为按真实页面结构手写的构造样本，未从真实站点抓取保存，文件头 README 已注明。
- 理由：不在仓库里存放站点内容；结构变化时新增 fixture，而不是改旧 fixture 让坏解析器通过。

## 11. 只实现 GET 的 HTTP 客户端

- 规格书：§8.3「仅对幂等 GET 请求自动重试」
- 实现：`app/network/clients.py` 只提供 `get()`；需要其他方法时另写，不在通用客户端里开放重试。
- 理由：从接口上保证「非幂等请求不会被自动重试」。

## 12. 订阅轮询在 M2 只写库不推送

- 规格书：§23 M2 范围不含卡片推送
- 实现：M2 的轮询把候选写入 `contents` / `releases`（`DISCOVERED` 语义），推送在 M4 由「有 release、无 notification、未被 accepted」的内容驱动。
- 理由：按里程碑顺序，避免半成品推送。

## 13. Nyaa 条目 ID 统一抽数字

- 规格书：§11.2 `releases(source, source_item_id)` 唯一
- 问题：同一个种子在 RSS 里是 `https://sukebei.nyaa.si/download/4761220.torrent`，在搜索页里是 `https://sukebei.nyaa.si/view/4761220`。按「取路径最后一段」会得到 `4761220.torrent` 与 `4761220` 两个 ID，唯一约束和门禁第 4 步都拦不住，同一内容会被写两条 release（info hash 唯一索引会兜底成 IntegrityError，但不是设计路径）。
- 实现：`item_id_from_url` 用正则只抽数字 ID，忽略 `view/`、`download/` 前缀与 `.torrent` 后缀；用例覆盖两种链接形式必须得到同一 ID。
- 理由：item ID 是去重主键，必须与 URL 的展示形式无关。

## 14. live 冒烟必须显式给代理

- 规格书：§8.2「不依赖全局 HTTP_PROXY，使用应用内显式 HTTP 客户端」
- 实现：`HttpClient` 保持 `trust_env=False`；live 测试从环境变量 `LIVE_PROXY_URL` 读代理，有值就构造 `http` proxy profile 传给 `NyaaSource`，没有就 skip。生产配置里对应 `proxy_profiles.<name>` + `nyaa.proxy`。
- 理由：部署环境（NAS 与开发机）出网需要代理且 DNS 可能被污染；显式传参既满足 §8.2，又能在受限网络里跑真实页面校验。socks5h profile 依赖 `httpx[socks]`（已声明）。

## 15. 图片域名 allowlist 默认按注册域派生

- 规格书：§20「图片和页面 URL 需要域名 allowlist」
- 实现：`image_fetch.allowed_hosts` 显式配置时严格按它（含子域）；未配置时按「各 provider 主机、Nyaa 主机、cdn_routes 的注册域（eTLD+1 启发式）」派生，例如 `adult.contents.fc2.com` → `fc2.com`，因此 `storage201000.contents.fc2.com` 这类真实 CDN 可通。
- 理由：九个 provider 的 CDN 域名各不相同且会变；用注册域派生既拦住无关主机，又不至于让图片全被 allowlist 误杀。真正的 SSRF 防线是「解析结果必须是公网地址 + 每一跳重定向都重新校验」，不依赖 allowlist 单独承担。

## 16. 图片缓存目录进配置

- 规格书：§19 要求 `/app/data` 持久化
- 实现：新增 `image_fetch.cache_dir`，默认 `/app/data/cache/images`，内容寻址（`<sha256[:2]>/<sha256>.<ext>`），原子写入。
- 理由：规格书没写缓存路径；放在 `/app/data` 下才能随 compose 的卷持久化并被备份脚本覆盖。

## 17. 翻译默认开启，缺凭据启动失败

- 规格书：§2.1 中文翻译属于必须实现；§18 示例 `translation.enabled: true`
- 实现：示例配置 `enabled: true`，缺 `base_url`/`api_key`/`model` 直接启动失败；没有翻译端点时显式设 false。
- 理由：翻译是必须项，默认开启并在缺配置时快速失败，比默默不翻译好；运行期翻译失败只记日志，不影响卡片。

## 18. FC2 官方 provider 的标题来源

- 规格书：§7.4 独立 selector
- 实现：优先 `meta[property="og:title"]`（真实页面有），去掉前缀番号；不使用站点通用的 `h1`/`h2` 兜底（真实页面里它们是「コンテンツマーケット」「サービス情報」）。
- 理由：用真实页面验证时发现通用标签会抓到站点导航文字，必须显式排除。图片 src 在真实页面是协议相对（`//cdn/…`），统一按页面 URL 补全。

## 19. provider selector 的线上验证状态

- 规格书：§7.4「线上结构变化时标记 provider 不健康」
- 已验证（真实页面，经代理）：
  - `Fc2`：文章页 HIT，标题/简介/封面/9 张预览图全部正确。
  - `Mmtv`：搜索页 HIT（`FC2-PPV-5006408`），标题与封面正确；真实卡片是 `div.video` + `h3.video-title > a`。
  - `Javdb`：真实 200 响应的结构已确认（`.movie-list .item > a.box`，标题在 `.video-title > strong`，短格式 `FC2-5000075`），据此重写 selector；随后该站对本机返回 403（限流），自动化 live 检查不稳定。
  - `freejavbt`：真实页面结构已确认（`.video-list-item > .card`，`h5.card-title` + `p.card-text`，懒加载 `data-src`），但 `?s=` 搜索不返回 FC2 条目，因此真实结果是「未命中」而非解析失败。
  - `Madou`：无结果时返回 200 + 404 面板，已按「未命中」处理并加 fixture。
  - `avsox`：返回 1.5KB 的 JS 挑战页，等同不可用。
- 仍无法验证（403 / 挑战页）：`fc2_hub`(javten.com)、`Javlibrary`、`miss_av`(missav123.com)、`avsox`。这四家的 fixture 仍是构造样本。
- 处理：解析不到锚点一律抛 `ProviderError("structure_changed")`；HTTP 404 与「页面里没有该编号」都返回未命中，不计入 provider 健康度（只有真正的结构/网络错误才累计，连续 3 次记 `metadata.provider_unhealthy`）。

## 20. 编号匹配按数字而非完整 display code

- 规格书：§6.3 canonical display 是 `FC2-PPV-1234567`
- 问题：真实站点标题普遍写 `FC2-5000075`、`fc2-ppv 5006408`、`FC2PPV1234567`，用完整 display code 做子串比较会大面积「未命中」，而日志看起来一切正常。
- 实现：公共 `code_matches(title, content)`，按数字编号匹配并容忍分隔符与 PPV 有无：`(?<![0-9A-Za-z])FC2[\s\-_.]*(?:PPV)?[\s\-_.]*<number>(?![0-9])`；七个按标题匹配的 provider 全部改用它，测试覆盖四种写法与「不同编号不匹配」。
- 理由：匹配口径必须与真实数据一致；卡片展示仍用规范的 `FC2-PPV-<number>`。

## 21. 推送目标与卡片续传

- 规格书：§9.1 私聊推送；§12.8 notifications 保存消息 ID
- 实现：
  - 推送给 `telegram.allowed_user_ids` 里的每个用户，每人一条 `notifications` 行（`chat_id` 区分）；「不感兴趣」只删该用户所在会话的整组消息。
  - 卡片发送前先建 `notifications` 行，每发一条就记录消息 ID；中途失败时该行保持 `pending`，下一轮从断点续发（海报已发就只补简介与图组），不会重复发卡片。
  - 只有 `sent` / `deleted` 的 notification 会把内容移出待推送队列，`pending` 仍留在队列里等待续传。
- 理由：at-least-once + 幂等副作用；宁可续传也不要静默丢卡。

## 22. 卡片没有封面时退化为纯文本卡

- 规格书：§9.1 第一条消息使用 sendPhoto
- 实现：九源都拿不到可用封面（或图片全部被校验拒绝）时，用 `sendMessage` 发同样的文字内容（标签/标题/编号/Nyaa 链接/简介），并记录日志。
- 理由：宁可少一张图，也不要整条内容不推送；封面失败不影响编号、链接和简介的送达。

## 23. 首次回填静默 + 单轮推送上限

- 规格书：§6.2「首次启动只处理 initial_lookback_hours 范围内的资源」；§9 推送卡片
- 问题：按字面实现时，首次回填会把 24 小时内的全部内容写进库，而推送队列的条件是「未接受 + 有 release + 无 notification」——一开订阅 Bot 就会把几百张卡片一次性推给用户。
- 实现：
  - `nyaa.initial_backfill_silent`（默认 true）：首轮回填写入的 content 标记 `contents.backfilled=1`，推送队列永久排除；只有第一轮成功之后新发现的内容才推送。
  - 0002 迁移给已有内容补标记为回填，避免升级时把积压一次性推出去。
  - `subscription.max_push_per_round`（默认 10）取代硬编码的 50。
- 理由：首次安装/升级不应该造成刷屏；需要回填内容时显式设 false。

## 24. Telegram 会话的 socks5h 写法转换

- 规格书：§8.1 代理类型包含 `socks5h`
- 问题：`aiohttp-socks` / `python-socks` 只接受 `socks5://`，把 `socks5h://` 直接交给 `AiohttpSession` 会抛 `Invalid scheme component: socks5h`。而项目其余部分（httpx）用 `socks5h` 表示「由代理解析 DNS」。
- 实现：`app/bots/factory.py::aiogram_proxy_url` 在构造 Bot 会话时把 `socks5h://` 转成 `socks5://`；python-socks 的 SOCKS5 连接器默认远程解析主机名，语义一致。
- 理由：配置里保持统一的 `socks5h` 写法，只在 aiogram 边界做一次转换。

## 25. 外部服务成功判定按版本兼容，不写死单一响应

- 规格书：§14.1 添加任务、§14.2 完成判断、§15 Transmission
- 问题：qBittorrent 5.2.3 登录成功返回 **204 + 空 body**，而 4.x 返回 **200 + `Ok.`**。按 4.x 判定会把成功当失败（实机表现为 `status=204` 的登录错误，任务全部投递失败）。
- 实现：
  - 登录：403 → `downloader_banned`；>=400（含 401）→ 凭据错误；204 空 body 或 200 + `Ok.` → 成功；200 + `Fails.` → 凭据错误（4.x 写法）；其它 → `downloader_login_unexpected`，报错带 `status` 与 `body_bytes`。
  - 添加任务：只有显式 `Fails.` 才算拒绝；空 body（5.x）按「已接受」处理，任务是否可见交给 info hash 回查。
  - provider 抓取：2xx 都算成功，不再要求恰好 200。
  - Transmission 会话握手允许 409 连续出现（会话 id 会轮换），最多 3 次。
  - 翻译：`message.content` 同时接受字符串与分段数组两种形态。
- 理由：版本差异属于正常情况，判定条件应表达「语义成功」，而不是「某个版本的字节」。

## 26. CookieCloud 未实现

- 现状：本仓库没有 CookieCloud 相关代码或配置（`grep -i cookiecloud` 为空），因此没有可修的版本兼容问题。若后续要接入，按第 25 条的写法处理响应判定。

## 27. 旧系统接管的口径

- 背景：从 fc2bot / torrentino / tr-bt 一次性接管状态，导入器 `scripts/import_legacy.py`。
- 口径（与用户逐条确认）：
  - `seen` 里 `messages` 非空的记录 → `contents` + `notifications`（`bot_name='legacy:torrentino'`、`status='legacy'`），不再推送。
  - `dead` 记录 → `notifications`（`status='legacy:dead'`），**不进黑名单**：用户没否掉它们，只是当时没下成；以后有新发布仍可被门禁放行。
  - `fc2_done.jsonl` / `fc2_claims.jsonl` → `accepted_entries`（`origin='legacy_import'`），有 hash 的补 `releases`（`source='legacy'`）。
  - `fc2_done.jsonl.pending` **不导入**：它是旧系统「删卡片」队列（`DoneDeletionQueue`），不是下载记录；导入会误挡内容。
  - `retry` 记录 **不导入**：临时失败还会重试，不能当已处理。
  - Transmission 任务分两路：`finished` 建 `DOWNLOADED` job 交给现有整理器（成功后才删 tr 任务，`deleteFiles=false`），`incomplete` 建 `DOWNLOADING` + `qbittorrent` job 交给监控接管；**不建** tr 的下载 job（那些任务还在 tr 里做种，我们只做登记）。
- 需要的枚举值（迁移 0003）：`accepted_origin` 增加 `legacy_import`、`legacy:transmission`；`notification_status` 增加 `legacy`、`legacy:dead`。推送队列把 `legacy` / `legacy:dead` 视为「已处理」，所以旧卡片不会再推。
- 回滚按标记删除（`bot_name` / `origin LIKE 'legacy%'` / `source IN ('legacy','transmission')` / `reason='legacy_import'`），导入时新建的 content 用报告里的 `created_content_keys` 精确清理，见 `docs/deploy.md` 9e。

## 28. 卡片排版改用旧系统模板（覆盖 §9.1 的字段顺序）

- 背景：用户实测卡片「没有按钮、排版很差」，并给出旧系统（fc2bot / torrentino）的原始模板作为基准，原话「像之前的那样就还可以用」。
- 实现（`app/services/cards.py`）：
  - `<b>FC2-PPV-{编号}</b>` 在第一行加粗，**标题不再在第一行**；
  - `🏷️ {有码/无码/薄码/未知}` 单独一行；
  - 标题（空白折叠，≤200 字，超出加 `…`），下一行 `💾 {大小} · 🕒 {YYYY-MM-DD HH:MM}`，大小取 `releases.raw_payload_json["size"]`，时间按 `app.timezone` 渲染；
  - `📝 {简介}`（空白折叠，≤250 字），`🇨🇳 {译文}`（≤250 字）；
  - `<a href="{nyaa}">nyaa</a>` 作为最后一行，可见文字只有 `nyaa`。
- 截断口径（与用户确认的折中，既不违反 §9.1，也满足紧凑排版）：
  - **整张卡放得进 caption（1024 内）时发完整内容，不截断**；
  - 放不下时才压缩 caption：编号 / 标签 / 标题（≤200 字）/ 大小时间 / `📝` 简介预览（≤250 字）/ `🇨🇳` 译文预览（≤250 字）/ 链接，截断处一律带 `…`；
  - 同时把**完整的简介与译文**作为紧随其后的文本消息发出去（`📝` 一条、`🇨🇳` 一条，各自按 4096 上限分块）。预览只是预览，不替代正文，所以没有丢任何字符；
  - 极端情况（链接或大小串本身就把 caption 撑满）连预览也放不下时，caption 只留编号/标签/标题/大小时间/链接，完整正文仍在后续消息里。

## 29. 卡片使用 HTML parse_mode，所有用户来源文本转义

- 背景：旧系统用 HTML parse_mode 发送；标题/简介里的 `<`、`&` 会让 Telegram 直接报错，卡片发不出去。
- 实现：`PARSE_MODE = "HTML"`；`escape_html()`（`html.escape(quote=False)`）作用于标题、简介、译文、大小；链接用 `html.escape(quote=True)` 放进 `href`。只有编号那一行是真实标签。
- 测试：`tests/unit/test_cards.py` 的转义组用例 + `tests/integration/test_publisher.py::test_markup_in_content_is_escaped_before_sending`。

## 30. 翻译改用免密钥 Google 端点（`google_free`）

- 背景：用户要求「直接用谷歌翻译」。实测可用端点是 `GET https://clients5.google.com/translate_a/t?client=dict-chrome-ex&sl=ja&tl=zh-CN&q=…`（支持多个 `q` 批量、顺序对应），必须走代理；`translate.googleapis.com/translate_a/single?client=gtx` 从同一出口被 Google 判滥用并返回 HTML「Sorry...」页面。
- 实现（`app/services/translate.py::GoogleFreeTranslator`）：
  - `translation.provider` 支持 `google_free`（默认）与 `openai_compatible`（原实现保留）；`translation.proxy` 默认 `japan`，用代理 profile 而不是环境变量；
  - 长文本按句子边界切到 ≤1000 字符一块，同一张卡片的多个块用多个 `q` **一次请求**发出；
  - 响应必须是 JSON 数组且条数与块数一致；HTML「Sorry...」页（`non_json`）、空数组（`bad_response`）、长度不匹配（`length_mismatch`）都判失败；
  - 限流每秒最多 1 次请求；`network` / `http_429` / `5xx` 重试 2 次带指数退避；其它错误不重试；
  - 简介本身是中文（有汉字、无假名）时跳过翻译；
  - 失败只写 `metadata.translate_failed` 日志并把 `description_zh` 留空，卡片照发（日文简介仍在）。
- 端点固定为常量：`base_url` 属于 OpenAI 后端，混用会误导，因此 `google_free` 不接受 `base_url` 覆盖。

## 31. 按钮文案带 emoji，状态更新保留官网按钮

- 按钮：`⬇️ 下载` / `🚫 不感兴趣` / `🔗 FC2 官网`（旧系统文案）。
- 状态更新（`已下载` / `重试` / `已拉黑`）改为基于**消息上已有的 markup** 原地替换第一个按钮（`settled_keyboard`），不再用 `card_keyboard_after(content_id, None, …)` 重建——后者会把「🔗 FC2 官网」按钮弄丢。

## 32. 运行镜像包含 `scripts/`

- 问题：Dockerfile 只 COPY `app/`、`docker/`、`docs/`，运维必须挂载源码才能跑导入器。
- 实现：`COPY --chown=app:app scripts ./scripts`；`docker compose run --rm app python scripts/import_legacy.py …` 直接可用。

## 33. 整理器：绝不覆盖目标文件 + EXDEV 回退

- 背景：`os.replace` 的语义是**覆盖**。同一编号重新下载（不同画质版本）落到同一目录时会静默毁掉用户已有文件；容器里 `/downloads` 与 `/library` 是两个 bind mount（同一块盘、`st_dev` 相同）时 `rename(2)` 返回 `EXDEV`，原实现直接判 job 失败。
- 实现（`app/organizer/mover.py`）：
  - **移动前检查目标**（rename 与 copy 两条路径都做；copy 在 `os.replace(partial, dst)` 之前）：目标不存在 → 正常移动；存在且大小相同 → 跳过并记 `organizer.already_present`（源文件**保留**，因为我们没写过那个目标文件，删源等于赌）；存在但大小不同 → `OrganizeError(stage=CONFLICT)`，任务进 FAILED 报人，目标文件一个字节都不动。
  - 目录种子逐文件判断，允许部分移动 + 部分跳过，结果里 `moved` / `skipped` 分开列出。
  - **乐观 rename**：`st_dev` 在容器里不可靠（bind mount 同盘仍 EXDEV），因此先尝试 `os.replace`；捕获 `errno.EXDEV` 时记 `organizer.rename_fallback {reason: "exdev"}` 并回退到复制路径（复制 + fsync + 校验 + 原子改名 + 删源）。其它 `OSError` 仍然按失败处理。
  - 新增 `MoveStage.CONFLICT`（只出现在错误对象与日志里，不落库，无需迁移）。
- 理由：任何部署形态（单 mount / 多 bind mount / 真跨盘）都能工作，且慢路径可观测；数据安全优先于「少留一个目录」。

## 34. Fc2 简介：官网只有摘要，且不抓 widget（方案已回退）

- 实测（2026-11-15，经日本代理）：`og:description` 被官网截到约 70 字（`FC2-PPV-5007844` 以 `…` 结尾）；`meta[name=description]` 稍长（约 180 字）但同样是摘要。文章页本身**没有**正文，正文在 iframe `/widget/article/<number>/description?ac=<token>` 里。
- 曾实现过 widget 抓取（解析 iframe `src`、带 cookie 二次请求、跳过广告块与 base64 追踪节点），实测能拿到 1673 字全文。**用户明确否掉**（「不需要用官网的，其他源就非常好抓」），已整体撤除，`BaseProvider.fetch_extra()` 钩子保留作为通用能力。
- 现状：Fc2 只取 `meta[name=description]` → `og:description`，标 `description_quality="summary"`，`raw["description_source"]` 记 `"meta" | "og" | null`。

## 35. 简介质量语义：全文可以覆盖高优先级的摘要

- 背景：§7.2 规定「按 provider 优先级逐字段合并，已有值不被覆盖」。如果 Fc2 用 70 字摘要占住 `description_ja`，后面 javdb/mmtv 即便有正文也补不进来。
- 实现：
  - `MetadataResult.description_quality: "full" | "summary" | None`；
  - Fc2 的 meta/og 摘要 → `summary`；搜索结果卡片片段（avsox / fc2_hub / freejavbt / javlibrary / madou / miss_av / mmtv）→ `summary`；Nyaa 种子详情页的 `.panel-body` 上传者说明 → `full`；
  - 合并规则（`MetadataCoordinator._merge`）：空槽直接填；**`full` 可以替换已有的 `summary`，与 provider 优先级无关**；`summary` 永不替换 `full`；同为 `full` 或同为 `summary` 时按原优先级，先到先得。替换时打 `provider.description_upgraded` 日志并在 `sources["description_ja"]` 记下真正的来源。
- 对 §7.2 的补充说明：唯一允许「低优先级覆盖高优先级」的情形就是「高优先级只给了摘要、低优先级给了正文」。标题、封面、官网地址、日期等其它字段仍然严格按优先级，绝不被覆盖。
- 提前停止：默认 `provider.seek_full_description: false`——一旦核心字段与预览图齐了就停，不为一个大概率不存在的全文多打 8 次请求。设为 `true` 时，只有拿到 `full` 才允许提前停止。
- 实测结论（见 §34 与交付报告）：**当前配置的九个源里没有任何一个能给 FC2 内容的完整简介**（javdb 详情页需登录、javten/javlibrary/missav 403、avsox 是 JS 空壳、mmtv/freejavbt/madou 不索引 FC2 编号）。因此线上卡片显示的是摘要，属预期行为。

## 36. provider 文本统一解码 HTML 实体（只解一次）

- 问题：站点在 meta/标题里给的是 `&quot;` / `&amp;` / `&#39;`，`clean_text()` 只做空白归一化，原样留在 `description_ja` 里；卡片层 `escape_html()` 又把 `&` 转成 `&amp;`，最终显示 `&amp;quot;`（实测 `FC2-PPV-5008574` 简介末尾 `&amp;quot;&amp;amp;gt;`）。
- 实现（`app/providers/base.py::clean_text`）：`html.unescape()` **一次**，再折叠空白（`&nbsp;` 解成 U+00A0，`split()` 会当空白折叠掉）。不用循环解码：`&amp;quot;` 一次解码成 `&quot;` 是对的，循环会把用户内容里的字面量 `&amp;amp;` 吃成 `&`。
- 展示链路因此是「provider 解码一次 → 卡片层按 HTML 再编码一次」，两边各做一次，互不叠加。
- 影响面：所有 provider 的标题/简介（`meta_content`、`first_text`、`first_attr`、`_result`）都经过 `clean_text`。
- 原始值仍然保留：`releases.raw_payload_json` 由 `app/sources/nyaa.py` 直接从 RSS/搜索页字段构造，**不经过** `clean_text`，所以 RSS 里的原始 `description`、`guid`、`title` 等排查用字段不受影响；`provider_results.result_json` 存的是解析后的值（没有另存一份未解码副本，也没有任何逻辑依赖未解码文本）。

## 37. 非 FC2 的 Transmission 任务用 info hash 标识（规格书 10.6 / 10.7）

- 问题：`--transmission-migrate` 原先对提取不到 FC2 编号的任务记 `missing_code` 跳过，导致 qB 里正在下载的非 FC2 任务**没有 job 行**，下完不会被整理（孤儿）。
- 实现（`scripts/import_legacy.py`）：
  - `canonical_key = "infohash:<sha1>"`、`content_type = "generic"`、`display_code = clean_torrent_name(name)`（去掉分辨率/编码/站点 id 括号噪声与扩展名，最长 120 字）；`infohash:` 前缀与 `fc2_ppv:` 天然不撞；
  - 分类固定 `review`（`_review` 目录），**不看导出里的 `category`**：非 FC2 就是分类不明，不能猜；
  - `releases.source_item_id` 与 `info_hash` 都用 hash，并写 `accepted_entries`（`origin='legacy:transmission'`），门禁的 info hash 检查因此能挡住重复下载；
  - 报告新增 `by_group`（`transmission` / `transmission_generic`）与每条 action 的 `group` 字段，两类任务不混在一起统计；
  - 无 hash 的任务无法标识 → `missing_hash` 跳过（记在 `transmission_generic` 组）。
- 迁移 0001 的 `content_type` CHECK 已是 `fc2/jav/generic`，无需新增迁移。

## 38. 有码/无码标签：扩充词表、看标题、记录依据

- 问题：`_censorship_from_text()` 只认 `無修正` / `uncensored` / `有修正` / `修正あり` / `censored`，而 FC2 与 Nyaa 标题大量用 `【無】` / `【有】` / `【薄】`、`モザイク`、`薄修正`，且旧实现只看官网页面正文，命中率极低，全部落到 `UNKNOWN`。
- 实现（`app/providers/base.py::infer_censorship`）：
  - 词表：无码 `【無】`/`【无】`/`無修正`/`无修正`/`無碼`/`无码`/`uncensored`；有码 `【有】`/`有修正`/`修正あり`/`censored`/`有碼`/`有码`；薄码 `【薄】`/`薄修正`/`薄码`/`薄碼`；`モザイク` 判 **CENSORED**（拿不准时不用 THIN）；
  - 匹配前做 NFKC（全角→半角）+ 小写归一化；
  - 判定范围：FC2 标题、简介、**Nyaa 种子标题**（新增 `ProviderContext.release_title` → `ProviderPage.release_title`）与页面正文，四个字段一起看；
  - 依据写进 `raw["censorship_evidence"]`：`{"matches": [{field, token, verdict, excerpt}], "conflict": bool}`，排查时能看到是哪个字段的哪个词导致的；
  - 同一文本里同时出现「无码」和「有码/薄码」措辞 → 判 `UNKNOWN` 并置 `conflict: true`（与跨 provider 冲突的既有逻辑一致，都是宁可标未知也不猜）。
- 注意：`contents.censorship` 只在当前为 `UNKNOWN` 时才会被 enrich 覆盖，因此**已经入库的历史内容不会被自动重判**，需要重新 enrich 才会更新（统计口径见交付报告）。

## 39. 删除卡片永久失败要落库并停止重试

- 背景：Telegram 对超过 48 小时的消息、已被删除的消息、无权限的 Bot 会永久拒绝删除；旧实现把它当普通失败重试 3 次后只打日志，`notifications` 表里看不出原因。
- 实现：
  - `notifications` 新增 `delete_error`（迁移 `0004`）保存 Telegram 给的原因；
  - `is_permanent_delete_error()` 按错误文本识别永久失败（`message to delete not found`、`message can't be deleted`、`message is too old`、`not enough rights`、`chat not found`、`bot was blocked by the user` 等）——aiogram 把这类错误都包装成 `TelegramBadRequest`，只看异常类型区分不出来；
  - `message to delete not found` 单独归类：消息本来就不在了，目标已达成 → 记 `status=deleted` + `deleted_at`、**不写 `delete_error`**、不重试，日志 `notifications.delete_already_gone`；
  - 其它永久失败：**立即停止重试**、`report.dropped += 1`、`status=failed` 且写入 `delete_error`，日志 `notifications.delete_permanent_failure`；
  - 普通失败仍然重试，3 次后放弃并写入 `delete_error = "gave up after N attempts: <类型>"`。
- 删除用的 Bot 仍然是发卡片的订阅 Bot（`main.py` 里 `MessageDeleter(bot=subscription_bot)`），没有改动。

## 40. 历史内容标签重算脚本（`scripts/recensor.py`）

- 背景：词表扩充（§38）只影响之后 enrich 的内容，历史 200 多条仍是 `unknown`，等于白改。
- 实现：一次性脚本，**只用库里已有的文本**（`contents.title_ja`、`description_ja`、`releases.raw_title`）跑 `infer_censorship`，不发 HTTP。
- 边界（有意收窄）：
  - 只写 `contents.censorship`，并把 `censorship_evidence` **合并**进已有 `provider_results.result_json["raw"]`（保留其它键）；不动 assets、`metadata_status`、翻译、卡片，也不更新 `updated_at`（那个字段描述的是元数据抓取时间）；
  - `updated_at` 带 ORM `onupdate`，普通 ORM 赋值会顺带改掉它，所以更新走 Core `update(Content).values(..., updated_at=Content.updated_at)`（把值钉成自身，`onupdate` 不触发）并 `synchronize_session=False`。**修复前跑过的批次已经把这批行的 `updated_at` 污染成重算时间**，报告里 `notes` 会写明这一点，不回滚；
  - 默认只改 `unknown`；`--force` 才覆盖已有值，避免冲掉人工修正；
  - 无任何措辞依据的行保持 `unknown`（`no_evidence`），不猜。
- 幂等：第二次运行 `changed=0`（判定与现值相同的行计入 `unchanged`）。
- 报告 `recensor-report.json` 含每条变化（编号/旧值/新值/依据字段/命中词）与前后分布。

## 41. 日志必须带异常原文（`error_detail`）

- 问题：多处 catch 只记 `type(exc).__name__`，线上只看到 `error_code: TelegramBadRequest`，拿不到 `message to delete not found` 这句；这次是靠运维手动调 API 复现才定位到。
- 实现：`app/logging.py::error_detail(exc)` —— `str(exc)` 折叠空白、按日志规则脱敏（URL 凭据、Bot token）、截断到 300 字；空消息回退成异常类型名。
- 已补的日志点：`notifications.*`（retry / gave up / permanent failure）、`notify.telegram_failed`、`subscription.candidate_failed`、`supervisor.task_failed`、`health.db_unavailable`、`nyaa.pagination_failed`、`metadata.fallback_failed`、`telegram.bot_unreachable`、`bot.subscription.keyboard_failed` / `reply_failed`、`bot.edit_failed`、`http.request_failed`。`HttpNetworkError` 的异常文本也带上了原文。
- 约定：以后新增的外部调用 catch，日志里 `error_code`（类型/错误码）+ `error_detail`（原文）成对出现。

## 42. 记账性写入不移动 `updated_at`

- 背景：`contents.updated_at` 是列级 `onupdate=utcnow`，任何 ORM 赋值都会刷新它。除 `recensor.py`（§40）外还有两处纯记账写入被顺带刷了时间：
  - `ContentGate.ensure_content()` 给已存在但缺 `display_code` / `title_ja` 的 content 补标签；
  - `ReleaseRegistrar` 首次回填时把 `content.backfilled` 置真——这个每次回填都会动，一改就把 `updated_at` 变成「回填时间」，字段彻底失去参考价值。
- 实现：两处都改成 Core `update(Content).values(..., updated_at=Content.updated_at).execution_options(synchronize_session=False)`，并用 `set_committed_value()` 同步内存对象（不标脏，避免 ORM flush 再触发 `onupdate`）。
- 保持不变：`MetadataService._persist_metadata`（enrich 写完）与 `_translate`（写入译文）仍然显式 `updated_at = utcnow()`——那确实是元数据变了。
- 守卫测试：`tests/integration/test_updated_at.py` 覆盖「补标签/回填/普通登记不改时间」「enrich 与翻译改时间」两条方向。

## 43. 测试里禁止硬编码当前年份的日期

- 背景：连续两次因为测试 fixture 里写死 `2026-11-14` 这类绝对日期，越过 24h 回填窗口后测试开始随机失败（`rss_fc2.xml` 的 `pubDate`、`rss_with_items()` 生成器）。
- 实现：`tests/unit/test_no_hardcoded_dates.py` 扫描 `tests/` 下所有 `.py/.xml/.html/.json/.txt/.yaml/.md`，凡出现 `YYYY-MM-DD` 或 `datetime(YYYY,` 且年份 ≥ 2026 一律失败，要求改成 `datetime.now(UTC) - timedelta(...)` 或明确的历史年份。
- 配套改动：nyaa / provider fixtures 的日期统一改成 2020（时间戳字段同步），解析类断言跟着改；守卫测试自己用常量拼字符串，避免自命中。

## 44. 有码/无码：括号内前缀匹配（`【無・…】`）

- 问题：词表只认完整的 `【無】`，而 FC2 标题里更常见的是 `【無・ごっくん＆中出し】` 这种「括号内以 `無` 开头、后面还有内容」的写法，实测被判成 `unknown`。
- 实现（`app/providers/base.py`）：先用 `[【\[]([^】\]]{0,40})[】\]]` 取出括号内文字，再看是否以 `無/无`、`有`、`薄` 开头；命中即判对应标签。
- 防误判（宁可漏判也不错判）：`無` 后面跟 `料/制限/断/償/垢/名/敵/条件/許可/料金/添加/香料` 时不判（`【無料】`、`【無制限】`、`【無断】` 之类与审查无关）；`有` 后面跟 `料/料金/名/限/効/益/用/罪/機/志` 时不判（`【有料】`）。
- 裸词规则（`無修正` / `有修正` / `モザイク` / `uncensored` / `censored` 等）保持不变；证据按 `(field, token)` 去重。

## 45. 没有封面不推卡片（`publisher.require_cover`）

- 背景：官网查不到的编号会推出「无封面、无简介」的降级卡，用户明确不满意。
- 实现：
  - 新配置 `publisher.require_cover`（默认 **true**）与 `publisher.cover_retry_limit`（默认 3）；
  - publisher 每轮先 enrich，取不到 `assets.role=cover` 就**不发送**，返回 `deferred`；`contents.cover_status` 记 `pending_cover`，`cover_retry_count` 累加；
  - 达到上限 → `cover_status=give_up` 并打 `publisher.cover_give_up`，之后**不再重试**（推送队列用 SQL 排除 `give_up`，避免它每轮占用名额）；
  - 拿到封面后标 `cover_status=ready`；
  - `require_cover: false` 时退回旧行为（无封面发纯文字卡）。
- 迁移 `0005` 增加 `contents.cover_status`（VARCHAR，**不带 CHECK**）与 `cover_retry_count`。不给它加 CHECK 是有意的：SQLite 上补约束要重建 `contents` 表，而 `releases`/`assets`/`notifications`/`download_jobs`/`provider_results`/`accepted_entries`/`blacklist_entries` 都通过外键指向它，重建风险高于收益；取值由 `app.db.enums.CoverStatus` 在代码里约束。
- 重试语义：内容没有 notification 行就会一直留在推送队列里，所以「重试」不需要额外调度器；`give_up` 是唯一的终止条件。

## 46. Nyaa fallback 救回来：下载链接、标题兜底、描述配图

- 问题：实机报 `metadata.fallback_failed error_detail="nyaa: no torrent title"`。根因不是站点，是我们请求错了 URL：RSS 的 `link` 是 `/download/<id>.torrent`（种子文件，`application/x-bittorrent`），fallback 直接抓它，解析不到任何 HTML 标题。
- 修法（`app/providers/nyaa_fallback.py`）：
  - `url_for_release()` 把 `/download/<id>.torrent` 重写成 `/view/<id>`；
  - 标题兜底链：`#torrent-title` / `h3.panel-title` / `h1` → `og:title`（去掉 ` :: Sukebei` 后缀）→ **`ProviderContext.release_title`**（我们库里的 `releases.raw_title`）。有种子就一定有标题，所以这条兜底让卡片不会因为官网下架而彻底没标题；
  - 描述取 `#torrent-description`（上传者正文），不再误取第一个 `.panel-body`（那是 `Category/Date/Info hash` 的元数据表）；
  - 配图：`#torrent-description img` 的 `src` + 正文里裸的图片 URL（`https?://…(jpg|png|webp|gif)`）；**第一张当 cover**、其余当 previews。`og:image` 在 Nyaa 上永远是站点默认头像（`/static/img/avatar/default.png`），不再拿来当封面；
  - coordinator 调 fallback 时把 `ctx.release_title` 传进 `ProviderPage`。
- 真实页面验证（代理，`sukebei.nyaa.si/view/4760732`）：标题正常、描述 304 字、cover = `kr-av.com/upload/…jpg`、preview 1 张。

## 47. provider 请求伪装：浏览器头 + cookie 预热 + 限速

- 背景：旧系统基线 fc2_hub（javten）25.6%、Mmtv 14.0%、miss_av 5.3%，现在这些站点返回 403，怀疑请求被识别成爬虫。
- 实现：
  - `app/network/browser_headers.py::provider_headers(host, referer=…)` 统一构造 Chrome 导航头：`User-Agent`（Chrome 131）、`Accept`、`Accept-Language: ja,…`、`Accept-Encoding: gzip, deflate`（不写 `br`，httpx 无额外依赖时解不了）、`Upgrade-Insecure-Requests`、`Cache-Control`、`Sec-Fetch-{Site,Mode,User,Dest}`（`Sec-Fetch-Site` 按 referer 与目标 host 是否同源推导）；
  - 每个 provider 只写一次（`BaseProvider.fetch`），不在 9 个适配器里重复；
  - `provider.sources.<name>` 新增 `headers`（覆盖）、`warmup_url`（先访问一次首页：拿 cookie + 让 referer 同源）、`min_interval_seconds` / `jitter_seconds`（每站限速，10s ≈ 6/min）；
  - cookie 复用同一个 `HttpClient` 的 cookie jar（按代理分池），预热拿到的 `Set-Cookie` 自动带到后续请求。
- **实测结论（2026-11-15，经日本代理）**：
  - javten.com、missav123.com、javlibrary.com：**403 + `cf-mitigated: challenge` + `server: cloudflare` + `cf-ray`**，即 Cloudflare 的 JS/managed challenge。补齐浏览器头、cookie 预热、referer 链之后仍然是 challenge；
  - 另外用 `curl_cffi`（`impersonate="chrome"`，即模拟 Chrome TLS 指纹）试过，同样 403 challenge → **不是 TLS 指纹问题**，是 JS 挑战；
  - avsox.click：200 但只有 1520 字节的 JS 空壳（`_csrf` cookie 有返回），页面内容靠前端渲染。
- 因此这三家（javten / missav / javlibrary）在当前架构下拿不回来，需要以下之一：无头浏览器（Playwright + Chromium，镜像体积和内存代价大）、FlareSolverr 之类的挑战求解 sidecar，或换站点。**等用户决定**，代码里的头/预热/限速仍然保留（对非 Cloudflare 站点有效，且能降低被风控的概率）。

## 48. 配置驱动的 custom provider

- 背景：加一个元数据源要改代码、发版；运维实测到能用的站点应该改 YAML 就能上。
- 实现（`app/config.py::CustomProviderConfig` + `app/providers/custom.py`）：
  - `provider.custom: [{name, enabled, search_url, detail_url?, proxy, headers?, warmup_url?, min_interval_seconds?, jitter_seconds?, selectors{card,title,link,cover?,description?,previews?}}]`；
  - `search_url` 必须含 `{code}`（替换为 `FC2-PPV-1234567`），`detail_url` 必须含 `{id}`（取匹配卡片链接的最后一段）；
  - 名字不能与内置九个或 `Nyaa` 冲突，不能重名；`proxy` 参与启动时的 profile 校验；
  - 直接解析搜索页；配了 `detail_url` 时用 `fetch_extra()` 跟随匹配卡片的链接抓详情页，补齐简介/封面/预览；
  - 与内置源共用 `provider_headers`、cookie 预热、限速；
  - `card` 选择器一个都匹配不到 → `ProviderError("structure_changed")`（进 `/providers` 的 structure 计数），不静默返回空；卡片存在但没有编号匹配 → `None`（正常的 miss）。
  - custom 源在九个内置源之后运行，不改动规格书固定顺序。
- 顺带把 `BaseProvider` 的 `id`/`host`/`proxy_key`/`referer` 从 `ClassVar` 改成普通类属性，否则配置驱动的实例没法带自己的名字。

## 49. `scripts/retry_covers.py`

- 背景：`publisher.require_cover` 会把重试超限的内容标 `give_up` 并永久跳过；Nyaa fallback 修好后这些内容本可以出卡。
- 实现：把 `cover_status='give_up'` 重置为 `pending`、`cover_retry_count=0`，只动这两列（`updated_at` 钉住不动，见 §42）。支持 `--dry-run`、`--since DAYS`（按 `created_at` 过滤——`updated_at` 曾被污染过，不适合做时间依据）、`--only CANONICAL_KEY`；幂等（第二次 `reset=0`）；报告 `retry-covers-report.json`。
- `--min-cover-dimension PX`（默认关闭）：额外重置**已有封面短边小于 PX** 的内容（`assets.role=cover` 的 `width`/`height`），用于清掉 300x250 广告封面这类脏数据；同一内容同时满足两个条件只重置一次，报告里 `reason` 记 `give_up` 或 `small_cover`。
- 推送队列的配合：SQL 只排除 `cover_status='give_up'`，`pending` / `pending_cover` 仍留在队列里，这样 publisher 每轮才有机会重试。

## 50. 标签：聚合结束后用标题兜底

- 问题：`_merge` 只在 `result.censorship is not None` 时采用 provider 的值，而 9 个 provider 对 FC2 内容**一个都不返回 censorship**（`nyaa_fallback` 也从不设），所以标签永远是 `UNKNOWN`——即使 Nyaa 标题里写着 `【無】`。
- 实现（`app/providers/coordinator.py::_apply_censorship_title_fallback`，在聚合结束、`previews` 截断前调用）：
  - 用 `aggregated.title_ja`（或 `content.title_ja`）+ `ctx.release_title`（触发这次查询的 Nyaa 标题）+ `aggregated.description_ja` 跑 `infer_censorship`；
  - **只在 `aggregated.censorship is UNKNOWN` 时填充**，`sources["censorship"] = "title_fallback"`，日志 `metadata.censorship_title_fallback`（带 evidence）；
  - provider 明确给过值就绝不覆盖；与标题推断不一致时只记 `metadata.censorship_conflict`（含两边的值），保留 provider 的值。
- 用 `ctx.release_title` 而不是查全部 releases：协调器不做数据库访问（分层约束），而触发查询的那条 release 就是标题里带标签的那条。

## 51. 翻译默认开启 + 缓存判断收紧

- 问题：`TranslationSection.enabled` 默认 `False`，生产配置一旦没显式写 `true`（或模板合并冲掉），翻译完全不调用——这是「卡片没有中文」最直接的原因。
- 实现：
  - 默认值改 `True`（`google_free` 免密钥，失败只记日志、不影响卡片，默认关没有理由）；
  - `main.py` 启动打 `translation.configured {enabled, provider, proxy, available}`，一眼能看出翻译有没有装配上；
  - `_translate` 的缓存判断改成「非空字符串才算缓存」：`if content.description_zh and content.description_zh.strip()`，空串/纯空白是上次失败的残留，必须重翻；写库前同样要求目标为空；
  - 空白描述直接跳过（`not description.strip()`），不再对 `"   "` 发请求；
  - fallback 提供的描述本来就会进入 `aggregated.description_ja`（`_merge` 填空槽），`_translate` 拿到的就是这个值，因此 Nyaa fallback 的描述同样会被翻译。

## 52. 标签改为「官网商品标签 → 标题方括号 → 默认有码」

- 背景：用户旧项目 `milikii/torrentmate` 的规则是只看官网商品标签（`data-tag="…"`），没有码制标签就默认**有码**——所以旧系统从来没有「未知」。我们原来只靠 provider 返回值和标题词表，且把卖家简介也算进去，结果大量 `unknown`。
- 实现（`app/providers/fc2.py`）：
  - `_official_tags(html)` 提取 `data-tag="…"`（HTML 实体解码、去重、保序），写进 `raw["tags"]`；
  - `censorship_from_tags(tags, title, release_title)`：标签含 `無修正` → 无码；含 `薄モザイク` / `薄消し` → 薄码；否则看标题方括号（`【無】`/`【有】`/`【薄】`）；都没有 → **默认有码**；
  - 证据写进 `raw["censorship_evidence"]`，带 `source`（`official_tags` / `title` / `default`）和 `tags`；
  - **只有官网页面抓取成功才会走到这里**：404/下架走 `not_found`，不会凭空给默认值；
  - 卖家简介/页面正文**不再参与**判定（torrentmate 明确「卖家详情文案一律不算」），`infer_censorship` 的调用点只传标题和 release 标题；
  - `scripts/recensor.py` 对齐同一规则：优先读 `provider_results` 里 Fc2 的 `raw.tags`；没有成功抓取记录时只按标题判定，仍无依据就保持 `unknown`（不猜）。
- 实测：`adult.contents.fc2.com/article/5008836/` 的 `data-tag` 是 `顔射`、`ごっくん`（无码制标签）→ 判有码。

## 53. 翻译降级：Google 被限流时用 MyMemory

- 背景：NAS 出口 IP 被 Google 限流（429 / HTML challenge），翻译整段丢失；旧系统的做法是「谷歌被挡时自动降到 MyMemory，全失败才留空」。
- 实现（`app/services/translate.py`）：
  - `MyMemoryTranslator`：`GET https://api.mymemory.translated.net/get?q=…&langpair=ja|zh-CN`，取 `responseData.translatedText`；`q` 上限约 500 字符，超了按句子边界切块再按序拼接；
  - `FallbackTranslator`：先 Google，抛 `TranslationError`（429/超时/非 JSON/空）时降级 MyMemory；两者都失败返回 `None`（卡片无中文，照发）；主后端返回 `None`（空输入 / 本来就是中文）视为最终结果，不触发降级；
  - **只翻译前 400 字**（`TRANSLATE_MAX_CHARS`）：卡片只显示 250 字预览，翻 1600 字纯浪费时间；
  - 配置 `translation.fallback_provider: mymemory`（默认）/ `none`（关闭）。
- 实测（经日本代理）：`18歳の新人少女が初撮影に挑戦しました…` → `一位18岁的新人少女挑战了她的首次拍摄。请欣赏她虽然紧张但仍努力表现的样子。`

## 54. 图片白名单改为「默认宽松、可收紧」（`image_fetch.strict_allowlist`）

- 问题：Nyaa 种子描述里的封面图挂在第三方图床（imagetwist、kr-av…），而 `image_fetch.allowed_hosts` 只含 provider 域名，于是 `metadata.image_rejected host_not_allowed` → `assets=0` → `cover_status` 永远不 ready → `require_cover` 把所有卡片挡住（实测 341/374 内容无封面、8 个 `give_up`）。
- 实现（`app/network/image_fetcher.py`）：
  - 新增 `strict_allowlist`（默认 **false**）。false 时**不校验域名**，只保留真正的安全边界：解析出的 IP 必须是公网、字面量 IP 也要查、**每一跳重定向都重新校验**；
  - true 时沿用原白名单（`allowed_hosts` 为空即拒绝一切）；
  - 私网/回环/链路本地/云元数据（169.254.169.254）/IPv6 ULA 一律拒绝，两种模式都生效；
  - 大小上限、`Content-Type` 白名单、Pillow 解码、像素炸弹防护全部保留（有测试守住）。
- 日志：`image.allowed_by_ip_only`（宽松模式放行了白名单外的公网域名）、`image.rejected_private_ip`（带 `reason=literal_ip|dns` 和地址）、`image.rejected_content_type`、`image.rejected_size`。
- 校验顺序调整：字面量私网 IP 在**域名白名单之前**判定，这样 `https://127.0.0.1/…` 报 `ssrf_blocked` 而不是 `host_not_allowed`，排查时更准确。
- 实测：`picsum.photos`、`httpbin.org/image/png`（都不在白名单）经代理抓取、解码、落盘成功。
- 已知遗留：`kr-av.com/upload/…jpg` 这类链接返回的是 **HTML 图片查看页**（`text/html`，29KB），不是图片本身——现在会明确报 `bad_content_type` 而不是静默失败，但这类图床还需要「解析查看页找真实图片 URL」的能力，属于后续工作。

## 55. HTML「查看页」解析：通用做法，不按图床写适配器

- 问题：`kr-av.com/upload/…jpg` 这类链接返回的是 HTML 查看页（`text/html`），图片本体在页面里，直接抓只能拿到网页。
- 实现（`app/network/image_fetcher.py`）：
  - `fetch(..., resolve_viewer=…)`：遇到 `Content-Type: text/html` 且允许解析时，把 HTML 交给 `image_url_from_viewer()`；
  - **多候选 + 排序**（`viewer_candidates()`）：先按 URL 过滤广告（`banner`/`ads`/`adv`/`candy`/`promo`/`sponsor`/`aff`/`partner`/`300x250`/`728x90`/`160x600`/`320x50`/`/assets/`/`/img/candy/`），再按 `og:image`/`twitter:image` → 主内容区（`#content`/`.main`/`.image`/`article`）→ 声明尺寸 → 图片 CDN 域名（`img\d+\.`/`cdn\.`/`i\.`/`pbs\.`）→ 路径含 `/i/`、`/upload/`、`/images/` 排序；GIF 只降权不排除；
  - 找到后**重新走完整抓取流程**（IP 校验、大小、Content-Type、Pillow 解码），不是绕过；
  - **最多跟一层**：解析出的地址若仍是 HTML，记 `image.viewer_unresolved` 并放弃，不递归；
  - **下载后尺寸校验**（只对查看页候选）：短边 ≥ 400px、文件 ≥ 20KB、必须是 `image/*`、Pillow 能解码；不合格就试下一个候选，**最多 5 个**，全不合格记 `viewer_unresolved {reason: all_candidates_rejected}`。这条是必须的：imagetwist 页面第一个 `<img>` 是 300x250 的广告位，不校验就会把广告当封面。
- 日志：`image.viewer_page_detected`、`image.viewer_candidate {url, source, reason_rejected}`（每个被跳过的候选都记一条，能看出为什么选了这张）、`image.viewer_resolved {url, width, height, bytes}`、`image.viewer_unresolved {reason}`。
- 只在 **Nyaa fallback** 的图片上启用（`provider == "Nyaa"`）；provider 自己的封面不做二次解析，避免把正常图片误当查看页。
- 实测（经代理）：`https://kr-av.com/upload/…-FC2-PPV-5007946.jpg` 解析出真实图片 `700x700 / 154KB / image/jpeg`。

## 56. 标签默认「有码」改为无条件（页面抓不到也一样）

- 背景：§52 的默认有码只在「官网页面抓取成功」时生效，页面失败仍留 `unknown`；而 torrentmate 的 `_censor_label(tags)` 是无条件的——没有 tags 也返回「有码」，所以旧系统里用户永远看不到「未知」。
- 实现：
  - `censorship_from_tags(..., page_available=)` 的默认分支不再受页面状态限制，只影响 `censorship_evidence.reason`：页面成功但无码制标签 → `no_official_tag`；页面没抓到 → `page_unavailable`；
  - 协调器在聚合结束时补同样的默认（`metadata.censorship_default {reason: page_unavailable}`），**仅对 `content_type=fc2`**——generic 资源不是 FC2 商品，不能用这条规则贴标签；
  - **唯一保留 `unknown` 的情况**：provider 之间明确冲突（一个说无码一个说有码），`sources["censorship"]` 记 `conflict:…`，此时不再默认；
  - 页面 404/下架仍然是 `not_found`（抓取状态），标签另按默认有码处理；
  - `scripts/recensor.py` 同步：没有 provider 记录、抓取失败的 FC2 行也走「标题 → 默认有码」，报告里 `evidence_source=default`。
- 预期：现存的 `unknown` FC2 内容会全部变成「有码」（标题带 `【無】` 的除外）。

## 57. 卡片按 torrentmate 的 `_caption()` 逐行对齐

- 背景：用户反馈「还是那个吊样子」。逐行对比旧项目 `_caption()` 后有两处实质差异，照抄。
- 改动（`app/services/cards.py`）：
  1. **标题剥离编号**：`clean_card_title()` 用旧项目的正则 `(?i)fc2[\s._-]*(?:ppv)?[\s._-]*0*{number}`，并去掉 `+++` 前缀与首尾 ` -_.`；剥离后为空就用 `display_code`（`FC2-PPV-xxxxxxx`）兜底。第一行已经加粗显示编号，标题里再出现一次就是重复。标题截断 200 字。
  2. **`📝` 只放一段**：优先 `description_zh`，没有译文才用 `description_ja`，截断 250 字；**删掉 `🇨🇳` 那一段和前缀**。超长时 caption 保留 200/250 预览，完整正文作为紧随的文本消息（仍只一条 `📝`，内容不丢）。
  3. 大小行固定存在：`💾 {size} · 🕒 {YYYY-MM-DD HH:MM}`，size 缺失显示 `-`（旧行为是整行消失）。
- 按钮不变：`⬇️ 下载` / `🚫 不感兴趣` / `🔗 FC2 官网`。
- 目标格式：
  ```
  <b>FC2-PPV-5008836</b>
  🏷️ 有码

  顔射素人 ハードザーメンぶっかけ７連射。…
  💾 2.1 GiB · 🕒 2026-11-16 05:56

  📝 中文简介（无译文时用日文原文）
  <a href="…">nyaa</a>
  ```

## 58. `📝` 只显示 FC2 官网描述，并过滤广告/纯链接

- 问题：`4910695` 的 `📝` 是一串裸 URL（Nyaa 描述里只有图床链接，翻译又原样返回），`4910014`/`4907764` 是卖家广告（「官方频道」「特价 3960pt→1980pt」）。根因是**我们把 Nyaa 卖家文案当简介**。
- 旧项目 `_official_description()` 只取 FC2 官网的 `og:description`，Nyaa 描述从不进卡片；官网抓不到时 `detail=""`，`📝` 整行省略。
- 实现：
  - `publisher._build_card` 通过 `_official_description()` 读 `provider_results` 里 **Fc2** 成功记录的 `description_ja`，其它来源一律不用；没有就整行省略（`description_zh` 也只在有官网描述时才显示）；
  - `MetadataService.enrich` 只翻译 `aggregated.sources["description_ja"] == "Fc2"` 的文本；
  - `cards.usable_description()` 兜底过滤（官网描述也过）：纯 URL / URL 字符占比 >30% / 去掉链接后不足 20 字 / 含 `请关注`、`官方频道`、`不定期更新`、`ランキング`、`高画質版`、`特価`、`pt→`、`ご登録`、`フォロー` 等推广词 → 整行省略；
  - `_translate` 同样先过这个过滤，纯 URL/广告不发翻译请求（日志 `metadata.translate_skipped_junk`）。
- 标题长度：`truncate` 只在超过 200 字时截断；46 字的标题说明库里 `title_ja` 本身就是那么长，不是我们截的（有测试守住：46 字原样保留）。

## 59. javdb：节流、`rate_limited`、精确编号、登录墙

- 实测（匿名、纯 HTTP、经日本代理）：间隔 7–8 秒时稳定 200/36KB；连发立刻 403 空响应。所以 javdb 是**被限流**，不是要浏览器。
- 实现（`app/providers/javdb.py`）：
  - **节流默认值写进适配器**（7 秒 + 0–2 秒抖动），配置留空也生效——否则线上老配置会继续把站点打成 403；
  - 403/429、空 body、超时 → `ProviderError("rate_limited")`；**403 等 15 秒重试一次**，仍失败就放弃本轮；
  - 指标单独计 `rate_limited`（不是 `structure`），且不计入 provider 健康失败数；
  - **精确编号匹配**：从卡片标题里提取 `FC2[-PPV]-<数字>`，必须与请求编号完全相等；`4906053` 不会顶替 `4906073`（宁可没有，不要错数据）；
  - 只用搜索结果页：标题、封面（`c0.jdbstatic.com/covers/…`）、标题里的 `【無】/【有】/【薄】` 标记；详情页要登录，不碰。
- **新发现（2026-11-16 实测）**：即使等 60 秒、带 `over18=1` cookie 并先访问首页预热，匿名搜索仍然返回 **`<title>登入 | JavDB</title>`（33KB 登录页）**，没有结果。所以「javdb 只是被限流」只在部分时段/网络下成立，目前匿名访问是**登录墙**。适配器把这种情况报成 `login_required`，既不算 `structure_changed`（避免继续污染那 38 条统计），也不假装限流。

## 60. provider 不再返回 UNKNOWN；默认有码不会被绕过

- 问题：`_merge` 只要 `result.censorship is not None` 就写 `sources["censorship"]`，于是一个返回 `Censorship.UNKNOWN` 的 provider 会把字段「占住」，`_apply_censorship_title_fallback` 里 `if "censorship" in sources: return` 直接退出——默认有码永远不生效，标签停在 unknown。
- 实现：
  - `_merge`：只有 `result.censorship` **既不是 None 也不是 UNKNOWN** 时才参与合并；UNKNOWN 等同于「没意见」；
  - 冲突状态不会被第三个 provider 抹平：`sources["censorship"]` 以 `conflict:` 开头时直接跳过后续合并；
  - `_apply_censorship_title_fallback` 的早退条件收窄为 **只有 `conflict:` 前缀**（真冲突）；标题之间互相矛盾（`【無】` vs `【有】`）也记成 `conflict:title,release_title` 并保持 unknown；
  - 各 provider 不再返回 UNKNOWN：`censorship_from_tags` 的标题冲突回落到页面默认（`source=default`），javdb 把 UNKNOWN 转成 `None`。
- 诊断：`_apply_censorship_title_fallback` 每个分支都打 `metadata.censorship_resolved {reason}`，reason 取 `provider_value` / `conflict` / `title` / `default` / `generic_skip`，日志里一眼能看出这张卡为什么是这个标签。

## 61. 冲突也默认有码：FC2 内容的 `censorship` 永不出现 unknown

- 偏离规格书 **§7.3「冲突判 UNKNOWN」**。理由：用户明确要求卡片上不能出现「未知」（旧项目 `_censor_label()` 只有无码/薄码/有码三个返回值），而 FC2 资源默认有码。
- 实现（`app/providers/coordinator.py`）：
  - **provider 之间冲突**（一个 CENSORED 一个 UNCENSORED）→ 值取 `CENSORED`，`sources["censorship"]` 保留 `conflict:a,b` 作为证据，日志 `metadata.censorship_conflict {resolved: censored}`；
  - **标题之间冲突**（`【無】` vs `【有】`）→ 值取 `CENSORED`，`sources["censorship"] = "default"`，evidence 里保留命中的词，日志 `metadata.censorship_title_conflict_resolved {resolved: censored}`；
  - 冲突一旦确定，后续 provider 不会把它改回别的值（但封面/预览/日期仍照常合并）；
  - 因此 **FC2 内容的 `censorship` 在任何路径上都不会是 `UNKNOWN`**；唯一例外是 `content_type=generic` 的非 FC2 资源，它们本来就不该被这条规则贴标签。
- 影响：`/providers` 与卡片上不会再出现「未知」；`docs/deploy.md` 的 `unknown` 相关排查条目只对 generic 内容成立。
