# CHANGELOG

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
