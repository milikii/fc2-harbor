# 与规格书的偏离与决策记录

本文件记录实现过程中对 `docs/BT_AUTOMATION_SPEC.md` 的**有意偏离**、规格书未覆盖处的**保守取舍**，以及每条的理由。凡是与规格书冲突的地方，以本文件的记录为准，并应在此处新增条目而不是默默改代码。

格式：`条目 / 规格书位置 / 实现 / 理由`。

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
