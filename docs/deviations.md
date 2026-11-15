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
