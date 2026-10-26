# 0.6.1 · 2026-10-26

审核整改：这一版只动 prompt 注入方式和日志，回复行为不变。

## 整改 · 前缀缓存（不予上架项）

**问题**：`on_llm_request` 里直接改写 `req.system_prompt`，注入的却是每轮都在变的内容——
召回的长期记忆、当前消息的解析补充、长夜状态与头像提示。system prompt 每轮都不一样，
厂商按前缀做的 prompt cache 全废，等于每轮全价重算。

**改法**：新增 `core/prompt_parts.py`，动态内容一律走 `req.extra_user_content_parts` +
`mark_as_temp()`，system prompt 保持一字不动。

| 注入点 | 原来 | 现在 |
|---|---|---|
| `memory/service.py` 长期记忆 | 拼进 system_prompt，带 `<!--congling:memory-->` 判重 | `add_temp_part(req, block, MARKER)` |
| `perception/service.py` 消息解析补充 | 以 `msg_id` 为 marker 逐条往 system_prompt 追加 | `add_temp_part(req, block, marker)` |
| `main.py` 长夜 hint / 头像 hint | 两次字符串拼接写回 system_prompt | `add_temp_part()`，marker `<!--congling:night-->` / `<!--congling:avatar-->` |

- 幂等仍在：同 marker 已存在就替换那一块，tool loop 重入不会叠加三份
- 静态人设（`humanize/persona.py`）继续留在 system_prompt——它每轮内容一致，且有
  `<!--congling:persona-->` 判重只写一次，不影响缓存
- 新增退路 `append_user_pair()`：确实拿不到 `extra_user_content_parts` 的环境，
  改成往 `req.contexts` 追加一对消息，不再回落到改 system_prompt

## 整改 · 日志规范

`night_selftest.py` 原来 `import logging` 并自建 `getLogger`，违反「日志只能走
`astrbot.api.logger`」。改成文件内自带一个最小 `_ShimLogger`（只把消息打到 stdout），
整个插件现在没有任何标准库 logging 的引用。顺带把这脚本里硬编码的绝对路径改成
`os.path.dirname(__file__)` 推导。

## 新增校验

`tests/check_prompt_cache.py`，四组断言：

1. `core.prompt_parts` 行为：system_prompt 不变、内容进 temp 块、marker 可查、同 marker 幂等、空内容不注入
2. 注入点源码扫描：`memory/service.py`、`perception/service.py`、`main.py`、`night/state.py`、`qqext/avatar.py` 里不许再出现 `req.system_prompt` / `setattr(req, "system_prompt")`，且必须接入 `add_temp_part`
3. 人设注入仍然幂等
4. 全插件（含 tests、脚本）不许 `import logging`

跑法：`python tests/check_prompt_cache.py`，当前 15 项全过。

## 关于 0.4.2 那条 changelog

v0.4.2 里写的「已改为 `extra_user_content_parts` + `mark_as_temp()`，新增
`core/prompt_parts.py` 与 `tests/check_prompt_cache.py`」当时并没有落地，代码里从来没出现过
`extra_user_content_parts`。这次是真落地了，上面两个文件现在都在仓库里。

# 0.6.0 · 2026-10-26

这一版只动界面和面板接口，回复逻辑、记忆判定、长夜状态机一行没改。

## 界面 · 三套 UI

面板从一套配色变成三套各自排版的 UI，`<html data-ui>` 一个属性切换，默认夜航：

| UI | 侧栏 | 长什么样 |
|---|---|---|
| `night` 夜航（默认） | 252px 品牌栏，导航带 01–08 编号 | 墨绿底、米金点缀、卡片浮起、等宽数字、面包屑 |
| `console` 仪表台 | 78px 窄图标栏，只留单字 | 浅灰底、36px 大标题、下划线 tab、卡片去阴影改细线 |
| `scroll` 尺素 | 232px 书卷栏 | 宣纸底、宋体、朱砂「卷」印、方框 tab、行距 1.95 |

- 三套各写自己的 `.app` 栅格列宽（252 / 78 / 232）。**注意**：`.app` 是 grid，只给 `.side` 写 width 会横向溢出压到主区，看着像"板块重叠"，这坑这版踩过一次
- 新增 `theme-v6.css`：三套版式差异 + 夜航 `n6-*` 组件
- 新增 `night.css`（78KB）：夜航全套皮肤——书脊、顶栏、卡片、时间轴、刻度曲线、能量环、配置卡，全部限定在 `data-ui="night"` 下

## 界面 · 夜航全量

夜航从「两页」补成整套，八个板块全部改由 `night.js` 渲染：

- **概览**：34px 问候 + 四指标带（各带迷你趋势线）+ 3×2 板块入口 + 好感刻度曲线（0–1000 六段，带「再 N 分进入下一阶段」）+ 最近决策
- **人格 / 行为 / 工具 / QQ / 系统**：统一换成夜航卡片语言，配置项仍是原来的 `data-path` 协议，保存逻辑没动
- **记忆**：6 个子页（总览 / 按用户分 / 按会话分 / 记忆库 / 等级与遗忘 / 好感度），记忆库是会话栏 + 时间轴（今天·昨天·日期分组，点开看上下文）+ 构成与最近会忘三栏
- **长夜**：暗色主卡 + 220px 精力环 + 睡眠节律柱 + 未曾爬楼 + 梦境残片；状态/睡眠/记忆/梦境/设置五个子页都在
- **导航**：侧栏 01–08 编号、顶部面包屑「从绫开始 · 04」、底部人格卡「绫 · 在线」

## 记忆三板块（新增）

- **记忆总览** `/page/memory/m3/overview`：总条数、永久档占比（按 `mem_level=0` 算）、7 天内会淡出的、认识的人与会话数、类型分布、26 周热力图、最近写入 8 条、系统自述一句话
- **按用户分**：人列表（头像 + 条数 + 等级 + 好感）+ 这个人的记忆时间轴 + 画像四项（称呼/作息/忌口/雷点）+ 好感七段刻度
- **按会话分**：会话列表（头像 + 条数 + 最近时间）+ 单会话记忆时间轴 + 24 小时活跃柱 + 类型分布

## 头像

- 用户 `https://q1.qlogo.cn/g?b=qq&nk={QQ}&s=640`，群 `https://p.qlogo.cn/gh/{群号}/{群号}/640`
- 私聊圆形、群聊方形；加载失败回落首字；主体是昵称（不是纯数字）时不拼号，直接首字

## 接口

新增三条，已注册进路由表：

- `GET /page/memory/m3/overview`
- `GET /page/memory/sessions`
- `GET /page/memory/session?session_id=`

## 修复

- **请求参数全丢**：`page_api` 里读参数原来写 `request.args`，AstrBot 给的是 `PluginMultiDict`（挂在 `request.query`），且不是 dict 子类，用 `isinstance(x, dict)` 判断会整批漏掉。按会话分那个详情接口一直报「缺 session_id」就是这个
- 记忆总览判断永久档原来用 `level` 列，实际列名是 `mem_level`
- 记忆类型中文映射补齐 `pref / promise`（真实值是 `fact / event / pref / promise / emotion / relation`，不是 `preference`）

## 样式

- `style.css` 里 163 条 `font-size` 规则全量重写：正文 10–13px → 13–16.5px，页面标题 20 / 26 / 34
- 硬编码的 `rgba(255,255,255,.8)` 卡片底色改成走变量，深色主题下不再出现白块
- `.sec` 章节标题不再铺底色，只留左侧竖线

## 工程

- `app.js` 只在各 render 入口加一行 `if (isNightUI() && typeof njXxx === "function")`，另外两套 UI 逻辑原样保留；`night.js` 没加载时自动回落到旧界面
- 加载顺序：`night.js` 先于 `app.js`（都用 `defer`），`night.js` 只定义函数不执行、不碰 DOM

## 校验

- `node --check` 过（`app.js` 115KB / `night.js` 105KB）
- 前端调用的 44 个接口，全部命中后端 49 条路由，没有调空接口
- 20 条接口冒烟全绿（HTTP 200 + 关键字段齐全），报告见 `route_report.md`
- 1440 / 1024 / 780 三档宽度截图核对，无横向滚动；仪表台、尺素两套皮肤不受夜航皮肤影响
- 真数据验证：1624 条记忆、永久档 77 条占 4.7%、8 个会话、官方群 1196 条、望山私聊 111 条

## 已知待办

- 好感刻度在好感分还没攒起来时是空刻度（不是渲染错）
- 热力图与节律柱只挤在最后一格：历史记忆的 `created_at` 都是导入那天，要改成按有数据的范围自适应
- 「认识的人」会把昵称类主体算进去，数量偏多，要加过滤

# CHANGELOG

## v0.5.0 · 2026-10-26

记忆从「一堆平权的事实」变成「有层级、会淡忘、看人下菜」的一套东西。
另外补了空间自动说说、换头像、二级栏目面板。

### 新增 · 记忆 2.0
- **记忆等级**：每条记忆带 0~5 级。0 级不可忘（禁区、里程碑），1 级随口一句几天就散，
  5 级能留小半年。等级不是模型一句话定的：模型给基础档，标签压上下限，好感度再微调
- **遗忘曲线**：四档可选——不衰减 / 长夜式 / 均衡（默认）/ 经典遗忘曲线。
  按「距上次被想起多少天」重算保留率，跑一次跑两次结果一样，不会因为维护频率不同而跑偏
- **好感度**：记忆贡献 + 说话加成 - 冷淡扣分，每天结算一次；单日有上限，掉分不掉穿阶段；
  面板上可以手改，改过之后不再被自动加减
- **用户画像**：把某个人的记忆压成一段认识 + 关键词，生成数量跟着好感度走；
  有白名单、不再构建名单、最低好感阶段三道关
- **用户识别库**：昵称会变，QQ 号不会。记忆里提到的人名现在能对上人

### 新增 · QQ 扩展
- **空间自动说说**：每天 1~3 条，时段随机撒开；素材从当天的梦、新记忆、情绪浓度高的对话里抽，
  7 天内不重复；带隐私过滤，涉及别人的内容不发
- **换头像**：插件进程内直接 set_qq_avatar，不用临时插件重启。图片走本地压缩，
  头像库分「可信 / 待审」，她自己扒到的图必须你点头才会用

### 新增 · 面板
- 二级栏目：记忆页拆成 概览 / 记忆库 / 用户 / 等级与遗忘 / 好感度；QQ 页拆成 空间 / 头像
- 配置项说明改成问号气泡，鼠标悬停才展开，页面不再被提示文字撑长
- 地址栏带 `#memory/levels` 这种锚点，刷新还停在原页

### 变更
- `qq_extra.qzone_allow_write` / `qzone_auto_post` / `signature_auto_update` 默认打开。
  只升级还停在旧默认值的配置，手改过的不动
- 数据库升到 v4：新表 `mem_subject` / `mem_atom_tag` / `mem_profile` / `affinity` / `qq_avatar_pool`，
  `mem_atom` 补 6 列。老记忆按 importance 一次性回填等级，不用重跑抽取
- 记忆注入除了事实，还会带一句「你和 TA 现在是什么关系」和已有画像

### 修复
- `test_targets` 的两条断言自相矛盾（拿管理员号当"普通用户"、把去重结果写成重复）
- `smoke_page_api` 漏掉一批依赖请求体的 handler，导致测试常年飘红
- `render_check` 的 subtabs 检查改成查源码定义，避免切页后 DOM 已重画误判

## v0.4.7 · 2026-10-25

文档：去掉定制授权说明。

### 变更
- README 顶部那段「本插件由「梦半何时醒」定制使用，除授权外不许外用」整块删掉
- 顶部第一块现在是官方群提示

## v0.4.6 · 2026-10-25

文档调整：官方群提到最前面。

### 变更
- `metadata.yaml` 的 `desc` 开头加上群号，插件列表和面包屑里一眼能看到
- `README.md` 标题下面第一块就是群号 TIP，写在版权声明之前
- 原来的「官方群」章节仍在开头（版权声明之后），内容不变

## v0.4.5 · 2026-10-25

午睡还是睡到第二天：真正的根因在前端和 page_api 两处把 kind 写死了。

### 修复
- **前端**：面板上点「睡」发的是 `{ kind: "night" }`，后端拿到就是 night，
  压根没走到时段判断。改成发 `{}`
- **page_api**：`night_sleep` 拿不到 kind 时默认填 `"night"`，
  现在留空交给 `sleep_now` 按当前时段判断
- **night/__init__.py**：收线 / 精力耗尽两处 `pending.get("kind", "night")`
  改成空值透传，统一由 `sleep_now` 兜底

### 说明
- 这一版改的是 `page_api` 包，AstrBot 的插件热重载对子包里的 handler 不一定生效，
  实测重载后仍是旧代码，**必须重启进程**才会加载

## v0.4.4 · 2026-10-25

官方群入口 + 午睡设置漏在界面外。

### 新增
- **官方交流群 577084969**
  - 页面左下角常驻一个群号卡片，点一下复制群号
  - 概览页顶部一条横幅，带「复制群号」按钮
  - README 头部信息块和独立一节都写了

### 修复
- **午睡时长在设置页找不到**：`noon_nap_minutes` / `noon_nap_jitter_minutes`
  加进了 schema 和 DEFAULTS，但长夜设置页是按字段名白名单渲染的，
  没把这两项列进去，所以页面上看不到、也改不了
  - 长夜 · 设置多一张「午睡」卡
  - 长夜 · 睡眠页显示「午睡时长」，和「夜间起床点」分开写，避免再混
  - `snapshot()` 补 `noon_nap_minutes` / `noon_nap_jitter` / `sleep_kind`

### 测试
- `render_check.mjs` 加四条断言：概览页要有官方群横幅和群号、
  设置页要能改午睡时长和浮动、睡眠页要显示午睡时长

## v0.4.3 · 2026-10-25

修午睡醒来时间算错。

### 修复
- **午睡会睡到第二天早上**：`next_wake_time()` 只按「固定起床点 08:00 ± 浮动」算，
  不管这次是夜里睡还是午睡。下午 13:40 睡下，`08:00` 已经过去，于是算到
  次日 08:00，一睡 18 小时
  - 现在 `next_wake_time()` 收 `kind` 参数，午睡走固定时长：默认 60 分钟 ± 15
  - `_do_sleep()` 把当前 fatigue_kind 透传下去
  - 手动点「睡」不传 kind 时按当前时段自动判断，下午点就是午睡

### 新增
- `night.noon_nap_minutes`（默认 60，范围 10~180）
- `night.noon_nap_jitter_minutes`（默认 15，范围 0~60）

### 不影响
- 夜里睡仍然走 `wake_time` + `wake_jitter_minutes`，凌晨 1:20 睡还是 6:46 醒

## v0.4.2 · 2026-10-25

上架审核反馈整改（prompt 缓存命中）。

### 修复
- **不再改写 `req.system_prompt`**：长期记忆、消息解析补充、关系/时段/场景、
  长夜状态全部改为 `req.extra_user_content_parts` + `mark_as_temp()`，
  只对本次请求生效、不落历史。系统提示词恢复静态，prefix cache 可以命中了
- 人格注入拆成两层：人设正文、说话风格、禁忌清单属于静态，留在 system prompt
  且只写一次；关系远近、当前时段、群聊/私聊属于动态，每轮跟着消息走
- `night_selftest.py` 的 logger 改为优先 `from astrbot.api import logger`，
  离线自检时再退回内置 logging

### 新增
- `core/prompt_parts.py`：统一封装临时上下文注入，marker 收口，避免再漏改
- `tests/check_prompt_cache.py`：断言连续两次请求的 `system_prompt` 完全相等

### 说明
- 审核提到的 `make_as_temp()` 实际方法名是 `mark_as_temp()`

## v0.4.1 · 2026-10-25

修掉「设置里开了，长夜还显示没开」。

### 修复
- **页面接口全挂**：AstrBot 新版页面 bridge 的地址是
  `/api/v1/plugins/extensions/{插件名}/{endpoint}`，而插件接口注册在
  `{插件名}/page/...` 下面。前端有七个调用漏了 `page` 这一层，
  请求全部 404，`catch` 之后拿到空对象，于是状态永远显示「没开」
  - 影响：长夜状态、梦境、未读、唤醒、入睡、爬楼、梦的钉选与删除
  - 概览和设置页没事，因为它们恰好写的是带 `page/` 的路径
  - 现在统一走 `apiPath()`，自动补前缀，不再靠手写
- 面板 css/js 加版本号，省得重载插件后浏览器还吃旧脚本
- 保存设置后立刻重渲染当前页，开关状态当场反映

### 新增
- `night.allow_sleep`（允许真正入睡，默认开）：关掉之后照样算精力、
  照样喊累，但永远不会睡过去，也不拦消息。手动「睡」不受它影响。
- 睡眠页多一行「真的会睡：会 / 不会，只是累」
- `core/config.py` 的 `DEFAULTS` 补上 `night` 和 `close` 两段，
  配置文件和 schema 对不上时也有兜底

### 改变
- 作者改为「洛水 & 望山」

## v0.3.0 · 2026-10-21

空间能认人了，管理员有了明确归属。

### 新增
- **目标识别** `qqext/targets.py`：把「看看谁的空间」变成 QQ 号
  - 明确 QQ 号（含 `@123456` / `o123456`）→ 直接用
  - 「我 / 自己 / 我的空间」→ 当前说话人
  - 没指定但 @ 了人 → 被 @ 的人
  - 昵称 → 群成员（群名片/昵称/备注）→ 好友（备注/昵称），精确优先，多人命中报歧义
  - 群成员/好友列表缓存 5 分钟
- **读某人空间**走 `emotion_cgi_msglist_v6`（`QzoneClient.user_feeds`），
  自己和他人都能用；原来的 `feeds3_html_more` 保留为 `timeline()`（好友动态流）
- `qzone.feed` 工具新增 `target` 参数，并支持拿到当前消息事件（`takes_event`）
- 页面接口 `GET /qzone/feed?target=`、`GET /admins`
- **管理员归属明确**：
  - 主名单读 AstrBot 全局 `admins_id`
  - `basic.extra_admins` 只作补充
  - `basic.include_group_role` 控制群主/群管是否算（默认不算）
- 系统页新增「管理员」卡片：只读显示 AstrBot 名单、可编辑额外管理员、群管开关、生效名单预览
- QQ 页新增目标输入框（留空 = 自己）

### 改变
- `is_admin` 重写：不再因为"插件名单非空"就把所有人挡在外面，也不再把群管默认当管理员
- `qq_extra.qzone_read_others` 控制能否看别人空间
- 旧配置 `basic.admins` 仍可读（作为 extra_admins 的回退）

### 测试
- `tests/test_targets.py`：13 项目标识别 + 8 项管理员判定
- `tests/test_qzone_http.py`：新增 msglist 请求形状与分页（验证 uin 指向上正确的人）

## v0.2.0 · 2026-10-21

QQ 空间从"转接别人的"改成"自己会"。

### 新增
- **内置空间客户端** `qqext/qzone_client.py`，协议对照 `astrbot_plugin_qzone_ultra`：
  - 读动态 `feeds3_html_more`
  - 发说说 `emotion_cgi_publish_v6`
  - `g_tk = hash33(p_skey, 5381)`
- **登录态三条来源**：手填 cookie → 本地库里的上次结果 → 现场向 OneBot 要
  （`get_cookies` 退到 `get_credentials`，缺 uin 时补 `get_login_info`）
- 登录态失效自动重拉重试一次
- 页面接口：`GET /qzone/feed`、`POST /qzone/publish`、`POST /qzone/auth`
- 两个模型工具：`qzone.feed`（life 组）、`qzone.publish`（qzone 组，需管理员）
- QQ 页重做：登录态状态卡（uin/来源/获取时间/写权限）、读动态列表、
  带字数统计的发说说框

### 改变
- `qzone_mode` 的 `reuse` 选项移除（它从没真正生效过），现在 `auto`/`builtin`
  都走内置直连；老配置里的 `reuse` 自动按 `auto` 处理
- `QQExt.status()` 改为 async（空间状态需要探测登录态）
- 新增配置：`qq_extra.qzone_cookie`、`qq_extra.qzone_page_size`

### 修
- `extract_cookies` 之前会把 `{"retcode":0,"data":{...}}` 这种响应当成 cookie 本体，
  导致嵌套结构取不到登录态
- `strip_html` 把 `<b>` 这类行内标签转成空格，正文被拆开（"今天 有点 累"）

### 测试
- `tests/test_qzone.py`：gtk/哈希、cookie 各种形状、动态解析、错误分支（26 项）
- `tests/test_qzone_http.py`：起本地假空间服务，验证真实请求的 URL、g_tk、
  hostuin、cookie 头与表单字段

## v0.1.3 · 2026-10-21

按「设置归位、界面减法」重排 UI。

### 改变
- **设置拆回各自板块**，不再集中在最后一页：
  - 人格相关 → 人格页
  - 拟人化 / 输出风格 / 消息感知 → 新的「行为」页
  - 记忆相关 → 记忆页
  - 工具相关 → 工具页
  - QQ 相关 → 新的「QQ」页
  - 生效范围 / 共存 / 调试 → 「系统」页
- **每页只保留核心 3~5 项**，其余收进「高级设置」折叠；每页底部一个保存按钮。
- 导航从 6 项调整为 7 项：概览 / 人格 / 行为 / 记忆 / 工具 / QQ / 系统；
  「收件箱」并入记忆页底部的折叠区（按需加载）。
- 概览页改为六个板块入口卡片 + 模块状态芯片，取代原来的散乱列表。

### 人格页联动
- 选 `builtin`：显示内置人设表单（可编辑）
- 选 `astrbot`：显示下拉 + **选中人格的完整 system prompt（只读）**，切换下拉立即更新
- 选 `none`：显示说明，不摆无用的编辑框
- 后端 `available_personas` 增加完整 prompt 字段（此前只给 180 字预览）

### 说明
- 配置项的说明文字在核心区只显示第一句，高级区显示完整说明，避免一屏字。

## v0.1.2 · 2026-10-21

配置搬进 UI，人设来源可选。

### 新增
- **设置页重做成分区表单**：基础 / 人格 / 拟人化 / 输出风格 / 消息感知 / 长期记忆 /
  实用工具 / QQ 扩展 / 兼容共存 / 调试，共 10 个分区、68 个配置项。
  左侧分区目录可跳转，每项带说明与控件（开关 / 数字 / 下拉 / 文本 / 列表）。
- 保存会**真正写入插件配置文件**（`AstrBotConfig.save_config()`），重载后依然有效；
  v0.1.1 的「临时覆写」改为「重新读取」按钮。
- 概览页加「打开设置 / 人设来源」快捷入口。
- **人设来源三选一**：`builtin`（插件内置「绫」）/ `astrbot`（下拉选 AstrBot 已有
  人格）/ `none`（完全交给 AstrBot 默认人设）。
  - 选 `astrbot` 时不再注入插件人设文本，只保留关系 / 当前状态 / 场景三层增强，
    避免与框架人格打架。
  - 人格页会显示选中人格的 prompt 预览。

### 新增配置项
- `persona.mode`：人设来源
- `persona.astrbot_persona_id`：选定的 AstrBot 人格 ID

### 新增自检脚本
- `tests/smoke_page_api.py`：路由匹配 + handler 返回结构
- `tests/check_settings.py`：分区结构、类型转换、apply 生效
- `tests/check_persona_mode.py`：三种人设来源的注入差异

## v0.1.1 · 2026-10-21

修 UI 连不上后端（两个问题叠加，缺一不可）。

### 修复
- **页面接口路由前缀错误**：注册成 `/congling/page/*`，但框架按
  `/api/plug/{metadata.name}/{endpoint}` 匹配，实际应为
  `/astrbot_plugin_congling/page/*`。全部 GET 请求此前返回「未找到该路由」。
- **bridge SDK 执行时机**：`app.js` 是普通脚本，会在框架注入的
  `/api/plugin/page/bridge-sdk.js` 之前执行，导致 `window.AstrBotPluginPage` 为
  undefined。改为 `defer` + 页面内显式引入 bridge（框架会重写成带 token 的 URL），
  并在 `ensureBridge()` 里做最多 4 秒轮询兜底。

### 其它
- 页面响应信封同时带 `status` 与 `ok`：dashboard 父窗口用 `status` 判错，
  插件前端认 `ok`，两边都不误判。
- 新增 `tests/smoke_page_api.py`：用框架自己的路由匹配函数验证 15 条接口，
  并直接调用无 request 依赖的 handler 检查返回结构。

## v0.1.0 · 2026-10-21

首个可用版本。按《从绫开始_插件制作计划_v1》落地 P0~P5。

### 内核
- 配置层：点路径访问 + 类型转换 + 内存覆写（UI 热改）
- 存储层：SQLite（WAL）+ 9 张表 + user_version 迁移 + 异步封装
- 能力探测：识别 10 个外部插件，按能力让位（拟人/记忆/节奏/守卫）
- 事件总线：6 类事件，订阅者异常隔离
- 输出守卫：去 emoji、去句末句号、压平换行、禁用语、重复自检
- 节奏控制：替换 `event.send` 逐条发送，单例互斥 + 8 秒兜底防吞消息
- token 预算：按优先级装填与裁剪
- 决策日志：记录判断得分、分句方案、引用决策

### 拟人化
- 分层人格注入（身份 / 关系 / 状态 / 场景 / 风格）
- 人格存 DB、可编辑、自动存版本、可回滚
- 回不回打分（12 类信号 + 连续对话窗口 + 概率抽签）
- 分句算法（合并碎片 / 长句二次切 / 随机重组 / 段数上限）
- 动态引用概率 + 冷却

### 记忆
- 原子抽取（LLM + 严格 JSON + confidence 过滤）
- 去重合并（精确 hash + 字符集相似度 + 冲突 supersedes）
- 召回（BM25 中文 bigram / 可选向量 / RRF 融合 / 时间与重要性重排）
- 注入（预算裁剪 + 24 小时去重 + 游标）
- 每日衰减、归档、遗忘、反思、自动备份（保留 7 份）
- 关系值维护

### 感知
- 合并转发展开（缓存 7 天 / 递归 2 层 / 60 条上限 / 长文本摘要 / 发言人标注）
- 引用链展开（3 层，区分三个角色）
- 图片描述缓存、语音转写、卡片域名识别
- 统一归档 + 收件箱

### 能力与 QQ
- 能力注册表（超时/缓存/限流/降级/统计）+ LLM 工具包装
- 内置 6 项能力：时间、天气、翻译、MC 状态、网页标题、自检
- 签名、群名片、资料查询、戳一戳、空间桥接

### 界面
- 原生 JS 单页，6 个页签，明暗主题适配
- 15 个页面接口，全部带异常兜底

### 测试
- 14 项单测（守卫 / 预算 / 相似度 / 转发解析 / 渲染 / 配置）
- 装配冒烟（假 Context 完整初始化 + 路由 + 让位验证）

### 已知未完成
- 群聊主动插话链路
- 语音条 TTS 实际发送
- 表情包转接 stealer
- 空间发布（内置模式）
- 资料卡图片渲染
