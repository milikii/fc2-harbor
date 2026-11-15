# §24 最终验收清单 — 逐条证据

规格书 `docs/BT_AUTOMATION_SPEC.md` 第 24 节共 **19 条**（原文从 984 行到 1002 行；编号按出现顺序，下称 24.1–24.19）。

每条给出：状态、证据（可复现的测试名 / 配置校验 / 日志字段 / commit）、以及未覆盖部分的原因。

图例：**已验证** = 有自动化测试或配置校验证明；**部分** = 主体成立但有一段依赖真实环境；**未验证** = 只在本机/沙箱外才能证明。

---

## 24.1 Nyaa 监控为全站 FC2，不包含 offkab 用户限制

- 状态：**已验证**
- 证据：
  - 配置校验拒绝任何带 `u=` 的查询：`tests/unit/test_config.py::test_nyaa_paths_reject_uploader_filters`（query / rss_path / search_path 三个入口）
  - 默认值与示例配置都是 `q=FC2&c=0_0&f=0`，没有 uploader 参数：`config/example.yaml`
  - 真实站点拉取：`tests/live/test_nyaa_live.py::test_live_rss_and_search_still_parse`（`--run-live`，本次通过代理实测 RSS 75 条、搜索页 75 条）
- 备注：真实请求的 URL 见 `app/sources/nyaa.py::NyaaSource._fetch_rss`。

## 24.2 九源顺序与本文完全一致，全部 enabled

- 状态：**已验证**
- 证据：
  - 顺序被硬校验，改顺序启动失败：`tests/unit/test_config.py::test_provider_order_cannot_be_reordered`
  - 九个适配器按配置顺序实例化：`tests/unit/test_metadata_coordinator.py::test_build_providers_keeps_configured_order`
  - provider id 与优先级表一致：`tests/unit/test_providers.py::test_provider_ids_match_priority_order`
  - 默认严格模式禁用任一源即失败：`tests/unit/test_config_m2.py::test_strict_provider_mode_rejects_disabled_source`

## 24.3 FC2 官方为第 1 来源，Nyaa 为最后补充

- 状态：**已验证**
- 证据：
  - 顺序校验（见 24.2）把 `Fc2` 固定在首位
  - Nyaa 回退只在缺字段/缺图时运行：`tests/unit/test_metadata_coordinator.py::test_nyaa_fallback_only_fills_missing_fields`、`::test_fallback_skipped_when_everything_is_present`
  - 回退 provider 不计入九源优先级列表：`build_providers` 返回 `(providers, fallback)`

## 24.4 日本 provider 和图片 CDN 可按配置走日本代理

- 状态：**已验证**
- 证据：
  - 每 provider 独立 proxy profile：`tests/unit/test_proxy_router.py::test_provider_proxy_is_used_as_configured`
  - 图片继承 provider 代理：`::test_image_inherits_provider_proxy`
  - CDN 域名覆盖：`::test_cdn_route_overrides_provider_proxy`、`::test_cdn_route_matches_parent_domain`
  - 日本出口只在本区域内故障转移、绝不回退 direct：`::test_jp_failover_stays_inside_jp_and_never_direct`
  - socks5h 可用（`httpx[socks]` 已声明依赖）
- 备注：真实代理连通性由运维冒烟确认（见 `docs/runbook-smoke.md`）。

## 24.5 图片由后端代理下载后上传 Telegram

- 状态：**已验证**
- 证据：
  - 下载 + 校验 + 内容寻址缓存：`tests/unit/test_image_fetcher.py::test_valid_png_is_cached_with_metadata`
  - 落库为 assets：`tests/integration/test_metadata_service.py::test_enrich_persists_metadata_assets_and_translation`
  - 上传并缓存 file_id：`tests/integration/test_telegram_assets.py::test_first_upload_stores_file_id`、`::test_second_upload_reuses_cached_file_id`
  - 卡片发送用的是本地文件/缓存 file_id，从不把来源 URL 交给 Telegram：`app/services/publisher.py::_send_poster` / `_send_previews`

## 24.6 卡片包含封面、标签、标题、日文简介、中文简介和 Nyaa 链接

- 状态：**已验证**
- 证据：`tests/unit/test_cards.py::test_card_layout_matches_the_legacy_template`（编号加粗首行、标签行、标题、大小时间、`📝` 简介、`🇨🇳` 译文、`<a>` 链接同处 caption）、`tests/integration/test_publisher.py::test_card_with_cover_and_previews`、`::test_card_shows_size_and_time_from_the_release`
- 备注：排版基准是旧系统模板（见 `docs/deviations.md` §28）；标题 ≤200 字、简介与译文各 ≤250 字，超出加 `…`；中文简介来自翻译，翻译失败时卡片仍含日文简介与链接（`tests/integration/test_metadata_service.py::test_translation_failure_leaves_description_empty`）。

## 24.7 预览图组紧随封面/简介消息

- 状态：**已验证**
- 证据：`tests/integration/test_publisher.py::test_card_with_cover_and_previews`（发送顺序：photo → [文本] → media group，且消息 ID 按序记录）
- 分组规则：2–10 张一组；单张退化为单图消息（`::test_single_preview_is_sent_as_one_photo`）

## 24.8 下载成功后按钮显示「已下载」

- 状态：**已验证**
- 证据：`tests/unit/test_cards.py::test_download_button_labels`、`::test_settled_keyboard_removes_the_download_callback`、`tests/integration/test_acceptance.py::test_first_click_creates_one_job`
- 备注：qB 接受后才变「已下载」；失败变「重试」并保留 accepted 记录（`test_failed_submission_keeps_the_accepted_entry`）。

## 24.9 不感兴趣后黑名单生效，并删除整组消息

- 状态：**已验证**
- 证据：
  - 先写黑名单再删消息，删除失败不回滚：`tests/integration/test_spec_22_3.py::test_not_interested_writes_blacklist_before_deleting_messages`
  - 整组（封面 + 简介 + 图组）删除并标记 deleted：`tests/integration/test_notifications.py::test_deleter_removes_the_whole_group`
  - 删除失败重试后放弃并标 failed：`::test_deleter_retries_then_gives_up`

## 24.10 同一 FC2 编号不同上传者、不同磁链不会重复推送

- 状态：**已验证**
- 证据：
  - 内容层去重 + 发布层留档：`tests/unit/test_gate.py::test_content_and_release_layering`
  - 已接受编号的新上传者只记 release：`tests/unit/test_release_registrar.py::test_accepted_content_still_records_the_candidate_release`
  - 门禁第 3 步 info hash：`tests/unit/test_gate.py::test_info_hash_in_active_job_is_rejected`

## 24.11 黑名单和已接受编号无法通过手动 Bot 再次进入下载

- 状态：**已验证**
- 证据：
  - 订阅里拉黑后手动 Bot 仍拒绝：`tests/integration/test_spec_22_3.py::test_blacklisted_code_is_still_rejected_by_the_manual_bot`
  - 已接受内容重复投递被拒：`tests/integration/test_manual_flow.py::test_duplicate_magnet_does_not_add_a_second_task`
  - 通用资源按 info hash 去重：`tests/unit/test_normalize.py::test_normalize_candidate_prefers_fc2_then_hash`

## 24.12 FC2 与手动投递默认均使用 qBittorrent

- 状态：**已验证**
- 证据：`tests/unit/test_config.py::test_valid_configuration_parses`（五类路由默认 qbittorrent）、`tests/integration/test_manual_flow.py::test_fc2_magnet_is_accepted_and_submitted`、`tests/integration/test_spec_22_3.py::test_clicking_download_creates_exactly_one_task`

## 24.13 qBittorrent 任务完成后才进入整理

- 状态：**已验证**
- 证据：
  - 未完成不整理：`tests/integration/test_spec_22_3.py::test_files_are_organized_only_after_the_download_completes`、`tests/integration/test_monitor_flow.py::test_incomplete_download_is_never_organized`
  - 完成判定：`tests/unit/test_download_completion.py`（progress/状态/致命态）
  - 两次文件快照一致：`tests/unit/test_stability.py::test_two_identical_observations_are_required`

## 24.14 文件整理成功前绝不删除任务

- 状态：**已验证**
- 证据：`tests/integration/test_monitor_flow.py::test_full_pipeline_organizes_then_removes`（ORGANIZED 时 `removed == []`）、`tests/integration/test_spec_22_3.py::test_task_removal_always_uses_delete_files_false`

## 24.15 删除任务使用 deleteFiles=false

- 状态：**已验证**
- 证据：
  - 适配器硬拦：`tests/integration/test_spec_22_3.py::test_qbittorrent_adapter_refuses_to_delete_files`、`tests/integration/test_qbittorrent_adapter.py::test_remove_always_sends_delete_files_false`
  - Transmission 同样 `delete-local-data: false`：`tests/integration/test_transmission_adapter.py::test_remove_never_deletes_data`
  - 监控路径：`tests/integration/test_spec_22_3.py::test_task_removal_always_uses_delete_files_false`

## 24.16 跨盘移动失败时源文件和任务均保留

- 状态：**已验证**
- 证据：
  - 移动失败保留任务与源文件：`tests/integration/test_spec_22_3.py::test_move_failure_never_removes_the_torrent`
  - 跨盘复制路径本身：`tests/unit/test_mover.py::test_cross_filesystem_copy_verifies_and_cleans_source`、`::test_partial_files_are_removed_on_failure`

## 24.17 容器重启后任务可恢复对账，不重复投递

- 状态：**已验证**
- 证据：
  - 重启后双击不重复：`tests/integration/test_spec_22_3.py::test_double_click_and_restart_do_not_add_a_second_task`
  - 启动对账重投 ACCEPTED 且有上限：`tests/integration/test_monitor_flow.py::test_startup_reconcile_resubmits_accepted_jobs`、`tests/integration/test_retry_cap.py`
  - 卡片续传：`tests/integration/test_publisher.py::test_resume_after_partial_send`

## 24.18 两个 Bot 均只允许白名单用户私聊操作

- 状态：**已验证**
- 证据：`tests/unit/test_bots_filter.py`（白名单、非私聊、缺用户三种拒绝）、`app/bots/commands.py::WhitelistFilter` 同时挂在 message 与 callback_query 上
- 备注：订阅 Bot 与手动 Bot 各自的路由都带该过滤器。

## 24.19 配置、数据库和日志可从 Docker 卷持久化并备份

- 状态：**已验证**
- 证据：
  - 卷与只读配置：`tests/unit/test_deployment_files.py::test_compose_matches_spec`
  - 日志落盘（滚动文件，位于 `/app/data/logs`）：`tests/unit/test_logging_redaction.py::test_file_sink_writes_redacted_json`
  - 备份脚本用 SQLite 在线备份 API + 打包配置，保留 14 份：`tests/unit/test_deployment_files.py::test_backup_script_is_executable_and_uses_the_sqlite_backup_api`
- 备注：`docker compose logs` 仍可用（json-file，10m×3）；文件日志是额外的一份，供备份使用。

---

## 未验证项与原因（如实列出）

| 项目 | 状态 | 原因 / 后续 |
|---|---|---|
| 24.4 日本代理真实连通 | 部分 | 代码路径有测试；真实出口地区与延迟需运维在 NAS 冒烟（`docs/runbook-smoke.md`） |
| 24.5 真实图片 CDN | 部分 | 校验逻辑有测试；真实 CDN 是否命中 allowlist 需冒烟确认（日志 `metadata.image_rejected` 会给出 error_code） |
| 24.6 中文翻译 | 部分 | 客户端有测试；真实翻译端点需配置后冒烟 |
| 24.12 Transmission 实机 | 部分 | 适配器有 respx 测试；未在真实 Transmission 上联调（首版默认禁用） |
| 24.19 恢复演练 | 部分 | 备份脚本有测试；恢复流程见 `docs/deploy.md`，尚未实机演练 |

## 测试总量

```
pytest -q           → 453 passed, 2 skipped（live 冒烟默认跳过）
pytest --run-live   → 需 LIVE_PROXY_URL，本次实测通过
ruff check .        → clean
mypy app            → clean
```
