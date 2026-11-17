# fc2-harbor 九源接入建议（2026-11-17）

## 证据口径与边界

- 依据已完整读取的本机 `/tmp/sol-sources-inventory.md`（源码盘点、VM 失败记录、NAS 直连与 Byparr 实测），以及 NAS `/tmp/sol-direct/`、`/tmp/sol-byparr/` 全部原始响应。原始文件只读取、复制分析，未修改。
- 唯一编号为 `FC2-PPV-4559944`。全文编号次数是 HTML 中字面 `4559944` 的次数，不是结果数；挑战页、URL、canonical、广告 referrer、搜索框、语言链接、脚本均可反射编号。九源均未取得这个编号的精确目标卡片。
- 清单的早期 VM 请求九次都是代理连接失败：curl exit=7、HTTP=000、CONNECT=000，不能据此判站点故障。以下“直连”沿用清单的 NAS 测试标签，实际请求经 NAS 7890 出站代理，而非 Byparr；原始 headers 的 `200 Connection established` 是代理 CONNECT，不是目标站状态。
- NAS 直连 curl 均 exit=0。清单及现存 headers/HTML 未保留逐次直连耗时，故各节明确记“耗时未留存”，不杜撰。清单中的直连 bytes 与现存 HTML 文件大小不同（可能是传输量与落盘正文口径差异，原因未验证）；下文用本次核查的落盘 UTF-8 bytes，不把差异当成追加实测。
- Byparr HTTP 是本机 API HTTP；`solution.status` 才是目标站 HTTP。耗时优先记 curl wall time，并附 JSON 时间戳耗时；返回 cookie 含不含 cf_clearance 只记存在性，不记录任何值。
- 本轮只增加 Javlibrary 两次 NAS 前台顺序请求，均 POST `http://127.0.0.1:8191/v1`，`cmd=request.get`、`maxTimeout=60000`、`curl --max-time 90`；没有追加其他站点请求、后台任务、并行或重试。两份正文已在 NAS 就地检查，均无影片卡片。
- 源码基线 `/tmp/fc2-harbor` HEAD `cb09085`。九源配置优先级必须保持 Fc2 → fc2_hub → Javdb → Mmtv → freejavbt → miss_av → Javlibrary → avsox → Madou；下面的“下一批顺序”是实施顺序，不是聚合配置重排。
- `strict_enabled=true` 默认要求九源启用；若选择暂禁用源，须明确处理 `strict_enabled=false`，否则不能只改 enabled。source 的 concurrency/retries schema 默认值未在 `build_providers` 传入，不能假定改这两个键就能控制实际调度。

## 1. Fc2

1. **现状**：NAS 直连正常返回业务页，但该商品不存在；Byparr 未测，无绕验证的证据需求。
2. **实测证据**：`https://adult.contents.fc2.com/article/4559944/`，HTTP 200，耗时未留存；NAS `/tmp/sol-direct/Fc2.headers`、`/tmp/sol-direct/Fc2.html`，落盘 33533 bytes。关键原文 `お探しの商品が見つかりませんでした`；编号 0 次。不是正常详情、不是 CF 拦截或首页重定向。本号官方已下架/不可取得，不能推出官方站坏了。
3. **推荐打法**：**直连优先，下一批第 2 家**。保留官方第一优先级与有码/无码、封面、简介等权威字段来源；不为这个下架号强上 Byparr。
4. **待改点**：URL 仍用数字详情 `/article/{number}/`，不改成搜索。需要确认 parser 将“商品不存在”识别为无结果，而不是把错误页 og:title 当商品标题；以仍在售编号验证既有详情选择器、标签判定和日期。不改默认官方有码规则的证据范围。

## 2. fc2_hub（javten.com）

1. **现状**：直连 `/search?q=...` 被 CF 挡住；Byparr 可以拿真实 HTML，但未证实一次有效的目标搜索。源码 `?q=` 不是页面公开表单的搜索协议，不能拿其他 GET 参数盲换后宣称修好。
2. **实测证据**：直连 HTTP 403，耗时未留存；NAS `/tmp/sol-direct/fc2_hub.headers`、`/tmp/sol-direct/fc2_hub.html`，5562 bytes，`Just a moment...`、`cf-mitigated: challenge`，编号 3 次仅挑战回显。Byparr `/tmp/sol-byparr/1-javten-q.json` 也是超时 detail（该次 HTTP/耗时未在现有清单留存）；`/tmp/sol-byparr/2-javten-q-retry.json`：API 408，78.166265 秒；`/tmp/sol-byparr/3-javten-kw.json`：API 408，69.115440 秒；后二者均为 `Timed out while loading the page or solving the challenge`，无 solution、无目标状态或 HTML，编号 0 次不能解释成零搜索结果。`/tmp/sol-byparr/4-javten-keyword.json`：API 200 / 目标 200，50.995654 秒（JSON 46.510 秒），HTML 155802 bytes，返回 cf_clearance；标题 `Search For :  - JAVten.com`，描述 `All Video And Photo By '' Search Keyword`。编号 4 次仅 canonical 和三个广告 referrer，通用直播卡片不是目标结果。实页表单明确 `method="post"`、`action="https://javten.com/sss"`、`name="kw"`，另有隐藏 `_token`，值不记录。
3. **推荐打法**：**Byparr 优先，留在 P0，但未通过验收**。当前只能证明存在绕过 CF 的通路，不能证明站内搜索、编号匹配或提取成功。
4. **待改点**：首先改请求协议：从无效的 `/search?q=` 接入真实 POST `/sss`、字段 `kw`；核验隐藏 `_token`、会话 cookie 是否必需及 Byparr POST 支持，不在本轮假定它们可忽略。拿到真搜索结果后再对齐 `.card/.video-card/article` 与实际卡片、标题、图片 lazy 属性及编号精确匹配。还要隔离/记录共享 Byparr 排队与挑战耗时；两次 408 的原因未验证，不直接归咎 DSH。

## 3. Javdb

1. **现状**：直连搜索得到真实列表，不是登录墙；Byparr 未测。列表只有相邻但错误的编号，不能算目标命中。
2. **实测证据**：`https://javdb.com/search?q=FC2-PPV-4559944&f=all`，HTTP 200，耗时未留存；NAS `/tmp/sol-direct/Javdb.headers`、`/tmp/sol-direct/Javdb.html`，36538 bytes。真实原文 `<div class="video-title"><strong>FC2-455994</strong> 超接近！素人パンチラ Vol.6 【56分】</div>`。目标编号出现 21 次均为查询回显等，卡片是 `455994`，比 `4559944` 少一位。页面有“登入”与 CF jsd 脚本，但已经取得 movie-list，不能据此判登录墙/挑战拦截。
3. **推荐打法**：**直连优先，下一批第 1 家**。已有真实列表，最先验证错号拒绝和正常样本；暂不引入 Byparr。
4. **待改点**：URL 暂不改；用真实 HTML 验证 `.movie-list .item`、`.video-title`、封面与详情链接选择器。源码虽然要求 5–8 位数字精确相等，实际 parser 拒绝该少一位卡片尚未运行验收；禁止通过子串/模糊匹配“补命中”。保留节流配置（默认构造器 7 秒间隔、2 秒 jitter），不要将登录导航误判为 login_required。简介设计为 None，不能把日期当剧情。

## 4. Mmtv

1. **现状**：直连可访问，但现有 GET 搜索模板返回通用影片列表；Byparr 未测，当前首先是搜索协议不匹配问题。
2. **实测证据**：`https://7mmtv.sx/search/?q=FC2-PPV-4559944`，HTTP 200，耗时未留存；NAS `/tmp/sol-direct/Mmtv.headers`、`/tmp/sol-direct/Mmtv.html`，132716 bytes。标题 `7mmtv.sx - Watch JAV Online`，编号 0 次，无目标卡片；无 HTTP 首页重定向，不等于 GET 搜索生效。实页表单 `method='post'`、`action='https://7mmtv.sx/zh/searchform_search/all/index.html'`，字段 `search_keyword`、`search_type`（默认 `searchall`）、隐藏 `op=search`。
3. **推荐打法**：**直连优先，下一批第 4 家**。先修搜索，再考虑浏览器方案；HTTP 200 通用列表不可验收。
4. **待改点**：从 `/search/?q=` 改成实页 POST 协议，并核验提交后的结果/跳转。真实结果取得前，不能认定源码 `div.video/.video-item/.item` 和标题选择器已有效；随后验证 lazy 图片及精确编号。不是单改 proxy 配置就能解决。

## 5. freejavbt

1. **现状**：直连正常，旧 `?s=` 返回无关列表；Byparr 未测，尚无 CF 阻断证据。
2. **实测证据**：`https://freejavbt.com/?s=FC2-PPV-4559944`，HTTP 200，耗时未留存；NAS `/tmp/sol-direct/freejavbt.headers`、`/tmp/sol-direct/freejavbt.html`，435138 bytes。标题 `FREE JAV BT | 每日更新`；编号 13 次只在 URL/语言链接等回显，卡片 0 次。公开表单 `action="https://freejavbt.com/search" method="GET"`，输入字段 `name="wd"` 且 value 为空；旧查询疑似被忽略，不是已证实的无收录。
3. **推荐打法**：**直连优先，下一批第 3 家**。搜索修正是简单 GET，优先于 Mmtv 的 POST 接入；先不使用 Byparr。
4. **待改点**：改为表单指示的 `/search?wd={code}` 并实测（本轮未发这个请求）。之后验证 `.video-list-item/.card/article`、卡片标题、lazy 封面、精确编号；不能把泛化 `p.card-text` 同时当标题与有效剧情而不审查实际文本。配置只保留既有 host/referer，别用延长超时掩盖参数错。

## 6. miss_av（missav123.com）

1. **现状**：直连撞 CF；Byparr 过验证、拿到真实搜索与卡片，但目标号没有精确结果。
2. **实测证据**：直连 HTTP 403，耗时未留存；NAS `/tmp/sol-direct/miss_av.headers`、`/tmp/sol-direct/miss_av.html`，5651 bytes，`Just a moment...`、`cf-mitigated: challenge`，编号 3 次仅验证回显。Byparr NAS `/tmp/sol-byparr/5-missav.json`：API 200 / 目标 200，54.045360 秒（JSON 49.602 秒），HTML 254505 bytes，返回 cf_clearance。标题 `fc2-ppv-4559944的搜尋結果 - MissAV | 免費高清AV在線看`；编号 57 次主要为查询、URL、语言/筛选链接和脚本，真实卡片为 3341582、2859042、4304385 等其他编号，没有目标标题或简介。
3. **推荐打法**：**Byparr 优先，留在 P0**。绕验证与页面内容阶段通过，精确命中和字段提取阶段未通过，不能独自撑起组合验收。
4. **待改点**：现有 `/search/{code}` 至少能进带查询词的搜索页，暂无证据需要改 URL。主要验证真实卡片选择器、链接、图片（src/lazy 属性）与严格编号拒错；源码 fixture 是构造样本，不能当真实解析成功。给浏览器通路显式合理超时预算，默认 source 15 秒明显短于本次 54 秒 wall time；记录慢请求、不污染下游聚合，不把挑战 cookie 的存在等同结果可用。

## 7. Javlibrary

1. **现状**：直连撞 CF；Byparr 能取得真实业务页。完整 `FC2-PPV-4559944` 与纯数字 `4559944` 被判格式无效，**`FC2-4559944` 已被接受进入搜索，但无结果**。
2. **实测证据**：直连 HTTP 403，耗时未留存；NAS `/tmp/sol-direct/Javlibrary.headers`、`/tmp/sol-direct/Javlibrary.html`，5669 bytes，`Just a moment...`、编号 3 次仅挑战回显。既有 `/tmp/sol-byparr/6-javlibrary.json`：API 200 / 目标 200，43.724053 秒（JSON 37.940 秒），HTML 15099 bytes，原文 `The search term you entered is invalid. Please try a different term.`，编号 3 次仅标题/搜索标题/Text Mode 链接。
   - 本轮第 1 次：`https://javlibrary.com/en/vl_searchbyid.php?keyword=FC2-4559944`，NAS `/tmp/sol-byparr/8-javlibrary-fc2.json`；curl exit=0，API 200 / 目标 200，47.718121 秒（JSON 43.576 秒），HTML 15032 bytes。原文 `Search returned no result.`，无“本次输入无效”业务提示，无 `.video` 卡片；编号 3 次均为页面标题、搜索标题、Text Mode 链接。结论是格式可接受、进入搜索、没有目标卡片，不是命中。
   - 本轮第 2 次：`https://javlibrary.com/en/vl_searchbyid.php?keyword=4559944`，NAS `/tmp/sol-byparr/9-javlibrary-num.json`；curl exit=0，API 200 / 目标 200，42.461672 秒（JSON 38.659 秒），HTML 15075 bytes。原文 `The search term you entered is invalid. Please try a different term.`；Search Tips 写 `the search term must start with a letter`。编号仍 3 次、同样仅回显，无卡片。两页都带通用 Search Tips，不能把 Tips 内“invalid search terms”当第 1 次也格式无效。
3. **推荐打法**：**降级为 Byparr 后备，下一批第 6 家**。先用 `FC2-{number}`，不再用完整 PPV 或纯数字；已解决这个号的输入格式疑问，但未验证正常 FC2 收录与字段质量。
4. **待改点**：改 keyword 格式，不改 `vl_searchbyid.php?keyword=` 端点。显式区分 invalid term、valid but no result 与结构变化。列表模式源码直接取首卡、没有精确编号校验，应补精确匹配验收；若需要详情字段，当前“不再请求详情”的设计须另评估。不要把错误页通用 h1 当影片标题，也不要为已知无结果页无限重试。

## 8. avsox

1. **现状**：直连是 JS 空壳；Byparr 渲染得到真实无结果搜索页，不需要把问题解释为 CF。
2. **实测证据**：直连 HTTP 200，耗时未留存；NAS `/tmp/sol-direct/avsox.headers`、`/tmp/sol-direct/avsox.html`，1520 bytes（清单 curl bytes 为 797），标题 `AVSOX`，只有站点变量及 `/res/js/javu.min.js`，编号 0 次、无卡片。Byparr NAS `/tmp/sol-byparr/7-avsox.json`：API 200 / 目标 200，12.883192 秒（JSON 8.006 秒），HTML 9291 bytes，没有 cf_clearance。标题 `FC2-PPV-4559944 - Search - Movie - AVSOX`，原文 `No results.`；编号 4 次仅 title、keywords、description、搜索框 value，无目标卡片。
3. **推荐打法**：**Byparr 优先用于渲染，下一批第 5 家**。渲染通路已有证据，正常结果尚未验证；无需先做 CF cookie 专项。
4. **待改点**：当前 `/en/search/{code}` 能渲染带查询词的搜索，不急改参数。接入渲染后 HTML，显式识别 `No results.`；用有收录编号验证 `#waterfall .item/.item`、标题、封面与精确编号。直连空壳不能按结构变化反复重试，也不能把 meta 搜索词算影片。

## 9. Madou

1. **现状**：直连进入搜索业务页但无结果；Byparr 未测，没有当前需要绕验证的证据。
2. **实测证据**：`https://madou.club/?s=FC2-PPV-4559944`，HTTP 200，耗时未留存；NAS `/tmp/sol-direct/Madou.headers`、`/tmp/sol-direct/Madou.html`，23402 bytes。存在 `.f404`、`404 . Not Found`、`沒有找到你要的内容！`，只有返回首页链接而非 HTTP 首页重定向。编号 5 次仅标题/元数据/查询回显，无目标卡片；200 不等于有结果，页面内 404 也不是 HTTP 404。
3. **推荐打法**：**先跳过积极接入，降级直连后备，下一批第 7 家**。现有无结果处理方向正确，但单个 FC2 样本没有证明该站的有效覆盖，不值得优先花浏览器成本。
4. **待改点**：`?s=` 与实页 GET 搜索表单一致，暂不改 URL。验证 `.f404` 返回 None 的真实解析行为，再找已收录样本检查文章选择器与精确编号。若暂禁用，要遵守 strict_enabled 配置约束；不改九源固定优先级。

## P0 试点结论

**miss_av + fc2_hub 目前不能作为“可用数据源组合”验收。** 两家都已有 Byparr 取得真实站点 HTML 的证据，但 miss_av 只有其他编号卡片，fc2_hub 更没有证实有效搜索：keyword GET 返回空搜索词，q/kw GET 超时。可以验收“绕 CF 后拿到业务 HTML”的局部通路，不能验收“精确搜到目标并补齐字段”。缺少：fc2_hub 真实 POST /sss + kw（含会话/隐藏字段要求）的提交验证；两家的真实 DOM parser 对齐与错号拒绝；至少一个已收录编号的精确标题、详情链接、封面等有效字段；多个编号/重复请求的稳定性、超时预算和共享 Byparr 排队观测。fixture 为构造样本、浏览器一次成功、clearance、编号回显均不能替代端到端提取与聚合验收。此处只是建议，本轮未运行 P0 程序或改配置。

## 下一批优先级

剩下七家按实施顺序 **Javdb → Fc2 → freejavbt → Mmtv → avsox → Javlibrary → Madou**：先利用 Javdb 已有真实卡片验证精确匹配；再以 Fc2 正常在售号建立官方正样本；接着修 freejavbt 简单 GET 与 Mmtv POST 搜索协议；然后接 avsox 已证实的浏览器渲染；Javlibrary 虽已确定可接受格式，但收录/列表精确匹配/详情覆盖仍待验，后置；Madou 当前只有无结果，最后作为低成本直连后备。这不是修改配置 priorities；现有九源聚合仍按配置顺序补缺、核心字段满足可提前停、单源失败继续后源。未实测 Byparr 的五家不声称已通或已失败。

## 最不确定的三点

1. **只测一个编号的外推风险**：4559944 在 Fc2 官方已下架/不可取得，多家无结果可能只是该号未被收录，而不是站坏了；不能以这一次否定九源的覆盖或稳定性。Mmtv/freejavbt 的旧 URL 则连有效搜索都未证实，不能混作“已确认无收录”。需要不同状态且已收录的正样本与明确不存在的负样本，验证字段而非仅页面可达。
2. **最不确定的站是 fc2_hub / javten**：`?q=` 重跑与 `?kw=` 两次 408 未定论，既不能证明这些 GET 参数有效，也不能证明站不可用；keyword GET 200 只得到空搜索词。POST /sss + kw 是真实表单证据，但尚未提交验证，隐藏字段、cookie、Byparr 能力与共享实例排队影响都未确认。今天“实测不通”的是直连 CF 通路及这两个 Byparr GET 请求，不是整个站永久不通。
3. **Javdb 精确匹配逻辑待验**：直连 200 却只返回 `FC2-455994`（相差一位）卡片；源码的精确数字比较看起来能拒绝，但本轮未运行 parser，也未验证正确编号卡片的字段提取。21 次 `4559944` 回显不能证明逻辑已通过。Javlibrary 两个新增输入测试只消除了本号格式歧义，不消除九源端到端验收的不确定性。
