# 主动消息上下文与人格名称残留诊断

检查范围：插件源码、当前配置、AstrBot 本机源码、只读数据库查询。未修改代码或数据库；本文件是本次唯一新增的文件。数据库快照位于 `data/plugin_data/astrbot_plugin_congling/congling.db`（AstrBot 的 `plugin_data`），配置位于 `data/config/astrbot_plugin_congling_config.json`。该快照中主动消息等统计以检查当时的库内容为准。

## 1. 问题一：主动消息发出后接不上上下文

### 结论

主动消息确实通过 `context.send_message()` 发出；它**不会无条件写入 AstrBot 的 conversation**，但插件随后确实将它写进 `msg_archive`，而下一轮 `_recent_context()` 的查询也会选中这条记录。问题是：查询只取 `sender_name,text`，格式化时把主动消息显示成 `绫: ...`，**没有将 `sender_id='bot'` 映射为 assistant/“我”**。这把 bot 自己说的话伪装成了第三方姓名，是这条归档上下文链里的直接身份错误。修改 `sender_name` 为当前人格名称单独不能修复“它不知道刚才是自己问的”；需要在 prompt 中表达它是 assistant 自己说的话。

还有一层实际运行风险：`_remember_sent()` 成功发送后会登记自身行为，而 `memory.inject()` 会查询该会话的自身行为。但账本中的 audience/session ref 使用发送端平台字符串，当前数据库中主动记录是 `session:default:private:<id>`，普通入站 QQ 事件的平台会标准化为 `qq`（见 `memory/refs.py:12-16`、`29-35`）。两者 ref 不相等，按 session 的行为召回会 miss。加上 `self_events()` 仅对匹配 `audience` 的记录放出当前文本，正常用户请求不满足 owner-private 条件，无法把主动消息正文当作该会话的 prompt 记录。因此这条旁路记忆不保证下一轮能补救。

可核验的当前数据库证据：

- `msg_archive` 中有 60 条 `msg_id LIKE 'proactive-%'` 的成功主动消息归档，全部是 `sender_id='bot'`、`sender_name='绫'`；说明主动消息已入插件归档，并非“只发送、未归档”。
- `event_ledger` 有 37 条 `event_type='proactive' AND status='succeeded'` 的自身行为记录；对应 `mem_atom` 为 `scope='self'`、`actor_ref='bot:ling'`、`ref_status='resolved'`。所以“主动消息没有记忆记录”也不是普遍事实；问题在于普通会话上下文没有稳定取到并以 assistant 角色呈现该消息。
- 上述 37 条自身行为记录和 60 条归档记录数量不同，表示不能假定每次归档都有对应账本记录。即便有账本记录，平台/ref audience 不匹配也会影响正常用户轮次的自述召回。

### 端到端链路

```text
Proactive.send_one()
  ├─ context.send_message(umo, MessageChain(...))         humanize/proactive.py:468
  │    └─ AstrBot Context.send_message → platform.send_by_session
  │       默认不会更新 conversation                 AstrBot core/star/context.py:692-695
  │       例外：群消息且 group_message_history_enable 时，另存群历史
  │                                             AstrBot core/star/context.py:713-740
  ├─ _remember_sent() → remember_action → event_ledger + mem_atom
  │                                             humanize/proactive.py:489-516
  └─ _archive() → msg_archive(sender_id='bot', sender_name=persona_name)
                                                humanize/proactive.py:518-537

用户下一轮入站
  └─ perception_hook → PerceptionService.inject()
       → archive 当前用户消息
       → _recent_context(): 读同 session_id 的最近归档消息，排除当前 msg_id
                                                perception/service.py:147-169
       → 只 SELECT sender_name,text；渲染为「name: text」
                                                perception/service.py:117-145
       → add_temp_part() 加到 extra_user_content_parts
                                                perception/service.py:174-181
       → memory_hook：self_events() 可额外召回自身行为，session/ref/audience 有匹配限制
                                                main.py:658-675; memory/service.py:325-339
```

关键文件/行号证据：

- 发送不是框架响应/LLM 回合：`humanize/proactive.py:433-468`。本地 AstrBot `Context.send_message()` 在 `astrbot/core/star/context.py:665-695` 直接向平台发送；该函数不调用 `ConversationManager.update_conversation()`。只有满足“群、非 webchat、配置开启”才在 `:713-740` 另写群消息历史。这不是标准 conversation 的 assistant turn，且私聊不走此例外。
- 成功发送后先 ` _remember_sent()`、后 `_archive()`：插件 `humanize/proactive.py:489-504`；归档指定 `sender_id='bot'`，却把 `sender_name` 写成 `self.config.persona_name`：`:518-537`。当前配置 `persona.mode='astrbot'`、`persona.name='绫'`，所以数据库留下的 60 条 bot 消息名字都是旧值。
- 注入读取会话最近 N 条归档消息并排除当前消息：`perception/service.py:106-145`。SQL 没有取 `sender_id`（`:117-122`）；格式化只按非空 `sender_name` 输出 `name: text`（`:138-145`）。因此真实的 `sender_id='bot'` 没有影响 prompt 角色标签。默认 `recent_context_count=6`、`recent_context_chars=1500`（`core/config.py:58-70`）；最近六条不足以覆盖消息且历史太长时也会漏，当前库里有 60 条主动记录，查询只按最近 id 限六条，属预期窗口而非永久完整历史。
- `inject()` 将 recent 内容放入临时 prompt part（`perception/service.py:164-181`）。`core/prompt_parts.py:1-17,68-77` 说明临时 part 设计上不落 conversation history。它可让**本轮模型**看到已归档的主动消息，但不能将此前主动消息追加入框架对话历史。
- 钩子顺序为 perception → memory → persona：`main.py:35-41,658-694`。`response_hook` 只在普通 LLM response 阶段通知 `memory.observe_response()`（`main.py:718-725`；实现 `memory/service.py:409-431`）。主动 `send_one()` 不触发这条 response hook/正常回复生命周期；不能指望它自动生成 conversation assistant turn。
- 角色语义也被 `memory/extractor.py:195` 明确区分为 assistant 与 user；仅给 bot 行填一个显示名称不足以代表 assistant role。

**判定原疑点：**“没进框架对话历史”成立（不能作为充分根因，因为插件另有 archive-backed 近期上下文）；“没有写入 msg_archive”不成立；真正可直接定位的上下文 bug 是 recent 查询丢了 sender 身份，并将 bot 发言按显示名误标成说话人标签。`_archive()` 的 `sender_name=persona_name` 确实使旧名显眼，但它只是标签来源错误，不是 archive 缺失。行为账本是另一条受 audience/session ref 约束的旁路，不能替代会话级 assistant turn。

## 2. 问题二：「绫」残留清单

当前有效插件配置经只读读取确认：`mode='astrbot'`、`astrbot_persona_id=''`、`persona_id='ling'`、`name='绫'`。数据库 `kv` 中 `persona:ling` 当前名称仍为“绫”。注意 `PersonaService.load()` 虽会在内存中将展示名更新为 AstrBot 当前人格名（`humanize/persona.py:34-53`、`:185-218`），但 `_archive()`、配置 health、多个模块的 fallback 仍读静态配置，因此仅在 UI 选了跟随模式并不意味着所有代码已跟随。

### 必须改（若产品要求运行时人格名/会话身份正确）

- **主动消息上下文角色标签**：不是改某个“绫”字符串的问题。修复 `perception/service.py:_recent_context` 的查询与渲染：读取 `sender_id`，bot 行固定标成“我/assistant 自己”，用户行按 `sender_name` 或 `sender_id` 输出。否则改静态名字后，bot 消息只会从 `绫:` 变成别的人格名加冒号，仍被模型当成第三方。
- **近期 prompt 中的旧 bot 昵称**：修复角色标签后，`humanize/proactive.py:_archive`（`:518-537`）建议从成功发送的来源拿 `await plugin.persona.display_name()`，或直接存稳定角色标签并让 reader 识别 bot。按 `sender_name` 展示人格名是可读性改进，角色识别应以 `sender_id` 为准。该处当前明确写入配置名，已验证 60 条旧记录。
- **平台键必须统一，若希望行为账本能在普通下一轮召回**：`_remember_sent()` 在 `humanize/proactive.py:506-516` 从 UMO 取 `default` 作为 platform；入站检索走 `memory/refs.py:12-16` 的 `platform_key()` 后是 `qq`。可在这里与现有 `platform_key()` 共用标准化逻辑，保证写入 `event_ledger.audience/session_ref` 的键与读取键一致；同时处理历史 `default` 记录的兼容读取/回填。只改 `_archive.sender_name` 对这个问题无效。须在目标机器核对该会话当前 `msg.meta.platform` 和 ref 后再上线。
- **配置名 `persona.name='绫'` 是否必须改取决于用途**：在 `mode='astrbot'` 下它不是应该生效的人格名（`humanize/persona.py:181-218` 会读 AstrBot 默认人格），但 `core/config.py:400-405` 给配置属性的默认值仍为该配置字段；`humanize/decide.py:111,139-143` 在自称名检测中会消费它，多个模块 fallback 也会消费它。若要彻底去除错误人名，将 `persona.name` 改成当前 AstrBot 人格名或改这些消费点为动态 `display_name()`；不可把“仅 UI 配置 astrbot 模式”当成完成。

### 建议改（真实前端残留；显示/降级时会看见）

- `pages/fromling/diarybook.js:7,255`：`DIARY_SIGNATURE="绫"` 用于日记页脚。日记本来自独立 JS（`pages/fromling/index.html:13` 引入），不是所有页面都经过后端 persona API。改为从 mount 接收 persona display name，或从日记页 API/host 明确设置名称；失败时用中性“我”而非旧名。
- `pages/fromling/night.js:595` 与**实际加载的 bundle** `pages/fromling/night.1029c.js:3456`：前者 `data.name || '绫'`，后者另一处日记双面书组件也有签名常量。`index.html:111` 显示浏览器实际加载 `night.1029c.js`，文件头 `night.1029c.js:2-3` 标明需与 `night.js` 同步；若修源文件必须同步生成 bundle，否则运行效果不变。`njPersonaBoot()` 会主动从 `page/persona` 读取并渲染当前 AstrBot 名（`night.js:126-150`），因此 `data.name` 正常时旧兜底可能不显现，但 API 失败/字段缺失时仍会显示错名。
- `[config] persona.name`：当前 `data/config/astrbot_plugin_congling_config.json:12-19` 明确配置“绫”。若要减少 UI/后备/历史生成入口中的错标，可把**显示名称**改成 AstrBot 当前名字；这不是改 `persona_id`。保存设置或部署配置时确保生效，并检查 `persona:ling` KV 中的历史人格快照/当前名称语义（PersonaService load 会以内存当前名覆盖 KV 显示名）。
- 日记数据库的 2 条含“绫”文本和 mem_atom 中 246 条含“绫”的正文：仅在产品要求历史日记/历史记忆正文改名时做人工审核式批次处理。它们是已存文字/回忆证据而非运行时人格配置，盲目替换可能改变用户谈“绫”的事实、引文和归档内容，不建议当作行为修复。

### 不用改（或不要当作显示名处理）

- `persona_id='ling'`、数据库主键/分区和引用中的 `ling`：这是内部稳定身份标识，不是展示名字。`memory/refs.py:29-35` 生成 `bot:ling`；`core/store.py:55-80,123-134,165-180,192-216,237-270` 中多个表以 persona_id 作列、复合主键或唯一索引。当前 DB 至少有 `mem_atom=8117`、`mem_subject=607`、`affinity=446`、`event_ledger=80`、`proactive_log=60`、`mem_diary=20` 等记录都在 `ling` 分区；另有 `night_dream_plan=38`、`persona_state=16`、`mem_profile=1`、`inject_cursor=34`、版本和维护/反思 KV 键含 `ling`。
- “内部 id 不是 Unicode 人名，也不会呈现在记忆对象 UI 当作名字”的判断：配置 API 明确拒绝修改 persona_id（`page_api/__init__.py:276-283`、`:444-454`）；人格对象亦会拒绝变更（`humanize/persona.py:63-66`），且有 `tests/test_persona_id_lock.py` 锁定该行为。**不要为了改显示名去改这个 id。**
- 插件注册名、命令别名“绫”、包/页路由名 `fromling`：`main.py:26-32`、`:823-825` 和 `core/paths.py:14-17`。这些是产品/兼容名称或路由，不等同当前角色的自称名；除非产品明确要重命名插件/命令/URL，不必为人格切换调整它们。
- 需要区分前端文件是否被加载：`diarybook.js` 被 `index.html` 引用且其独立签名常量真实可见；`night.js` 在当前 index 中不是加载文件，`night.1029c.js` 才是。只改未加载的源文件不足以消除浏览器页面的旧名。

## 3. 修改方案

### 问题一

1. **首选：保留现有归档链，修复 role-aware recent context。**在 `perception/service.py:_recent_context`（当前 `:106-145`）查询 `sender_id,sender_name,text`；`sender_id=='bot'` 时渲染成明确的“我刚才说：…”，其它消息渲染成“对方/昵称说：…”。同时在 `humanize/proactive.py:_archive`（`:518-537`）将展示名改为 `await plugin.persona.display_name()` 或存中性的 bot 标签（角色由 sender_id 判定）。影响面只在感知 prompt 和主动消息归档；无需改 conversation 表或回填历史。风险：近期窗口仍有 6 条/字符预算限制；该方案修复被窗口选中的主动消息，不保证很久以前的消息可见。可增加一个小型单测覆盖 bot 行与用户行角色映射，并验证最近消息行在 `inject()` 的临时 part 中。
2. **备选：为下一轮加主动会话锚点。**在 `memory/service.py:inject()` 或 `perception/service.py:_recent_context()` 加一个基于 `msg_archive.sender_id='bot'`、session/时间顺序的查询，优先保留紧邻当前用户回复之前的主动 bot 消息，即便近期 6 条/预算截断会挤掉它；仍以 role-aware 格式注入。影响多一个 SQL/选择策略，适合“用户隔多条闲聊再回复”场景；风险是跨越多个主动消息时选错锚点或把旧消息当成当前话题，须限制时间窗并优先同会话最近未回复的 `proactive_log` 记录。先不要将主动消息直接写回 AstrBot conversation：需要可靠取得目标 conversation id、并发/事务处理及跨私聊/群适配；相比插件已有 archive，这一方案风险和影响面更大。

**账本 ref 对齐（若也要求用 mem_atom 行为召回来补充上下文）：**改 `humanize/proactive.py:_remember_sent` 使用 `memory.refs.platform_key()` 规范平台，再对旧 audience `session:default:...` 做兼容查询或一次性事务回填。不要依赖 `default` 与 `qq` 自然相等。事件表以 `(persona_id,event_key)` 唯一，记忆表对 `(persona_id,event_key)` 建唯一索引（`core/store.py:490-512`）；回填应修改受影响行 ref/audience，而非再插入一份事件。

### 问题二

1. **推荐的小步改名：只改显示来源。**数据库主键保持 `ling`。部署配置将 `persona.name` 改为当前 AstrBot 人格名；若配置项在 astrbot mode 应完全不参与自称，则进一步把 `core/config.py` 的 name 消费点（至少 `humanize/decide.py:139-143` 与 `humanize/proactive.py:_archive`）改成 `PersonaService.display_name()`，使 AstrBot 当前名为唯一运行时展示源。UI 页脚签名改成动态字段；`night.js` 与其线上 bundle `night.1029c.js` 同步。风险：AstrBot API 不可用/缺少 `page/persona` 时要给中性兜底；确认 UI 不依赖静态 `data.name` 作为编辑值。
2. **历史文本处理（可选，不是行为修复）：**先用只读查询导出命中行，人工区分旧人设自述、用户称呼、引文，再只更新确认属于 bot 的历史正文，并用 backup/事务和行 id 白名单。mem_atom 正文变化还必须同步 `norm_hash`（`memory/atom.py:448-459`），如果是 event atom 还应同步 `event_ledger.result_text` 并核对关键词/向量；日记改正文则应确认 UI/cache 和 refs。避免全库 `REPLACE('绫', ...)`，它会误改用户陈述和第三方事实。

## 4. 不能直接改的地方

- **不能随意改 `persona_id='ling'`。**这会切到一个空的新数据分区，看起来像全部记忆消失；并不会自动搬移 `mem_atom`、主体/画像/好感、行为账本、主动日志、日记、作息计划、cursor、人格 KV、维护标记等。还需审计代码/SQL 中 47 处 `persona_id` 消费（只读搜索按文件统计：`core/store.py`、`memory/{affinity,atom,diary,extractor,ledger,maintenance,profile,refs,retrieve,service,subject}.py`、`humanize/{persona,proactive}.py`、`night/{dream,plans}.py`、`qqext/{avatar_pool,feed_memory,qzone_daily}.py` 等），以及复合唯一约束和 idempotency key。
- 若确有业务要求换 ID，必须设计一次性迁移：备份 DB；暂停插件写入；对所有 persona_id 表做事务回填；处理 `kv` 键名 `persona:ling`、版本、维护/reflect 标记；`mem_atom.actor_ref/subject_ref/event_ledger.subject_ref/actor_ref` 中 `bot:ling` 等 URI ref 要同步改；防止新旧 persona 的 event_key/idempotency 冲突；验证唯一索引、外键语义、日记 day_key 约束、向量和导出；提交后再切配置。仅改 config 不安全。
- **不能把 msg_archive 全量 sender_name 替换为新名作为上下文修复。**它混有历史身份/展示字段，bot/user 必须由 `sender_id` 判角色；历史自述正文还可能是证据。修复新写入逻辑即可，历史标签回填应限定 `sender_id='bot'` 且需确认历史记录无第三方 bot 混用。
- **不能在没有核对实际会话 conversation 的情况下，把 `context.send_message()` 的文本伪造追加到 AstrBot conversation。**框架的 send_message 是平台发送 API；conversation history 由普通 agent pipeline 保存。手动插入会牵涉目标 conversation、用户发送后 history 竞争覆盖、并发、多平台，以及该插件专用归档和框架历史重复注入。

## 5. 不确定项与数据口径

- 当前运行期配置文件和 DB 已核对；本地 `data/cmd_config.json` 不是人格主配置。本机 `data_v4.db` 是 AstrBot 框架数据库，插件数据 DB 是 `plugin_data/astrbot_plugin_congling/congling.db`。
- 用户提供“sender_name 有 80 条绫”，本次只读库快照通过 SQLite `char(32491)` 精确匹配查询得 `msg_archive.sender_name` 77 条（其中 `sender_id='bot'` 60 条）；`mem_atom.text` 用 `instr(text,char(32491))>0` 得 246 条，`mem_diary.text` 得 2 条。另一次绑定 Unicode 参数的只读抽样查询结果不一致，故不把未确认的 sender_name 余项作分类依据；统计时点/查询口径可能不同。用户提供数字与此快照不同，需按运行环境现场重查。
- 当前 `proactive` 账本记录的 `session_ref/audience` 是 `session:default:private:<id>`，而通用 `memory/refs.py:platform_key()` 会将 `aiocqhttp` 标准化为 `qq`。这是源码和当前行的证据；需要在现场检查目标用户事件 meta 的值，确认真实运行路径后再动旧 ref。
- `persona:ling` KV 与其 versions 的最新历史名仍为“绫”；`humanize/persona.py:210-218` 在加载时从 AstrBot 默认人格取当前名，但数据库配置字段、历史版本不会因此自动被重写。
