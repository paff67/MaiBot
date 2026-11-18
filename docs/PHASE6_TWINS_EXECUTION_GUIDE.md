# Phase 6：双子原生槽位、算力动态切模与 Meowssiah 真人回信特调执行指南（Code Agent 防漂移规范）

> **目标读者**：负责落地 Phase 6 开发任务的 AI Coding Agent（首席架构师 / 核心开发执行体）
> **基准分支**：基于 `main`（`74676f85f` 及之后最新提交）创建新特性分支 `feat/phase6-twins-native-runtime`
> **前置必读文档**：
> 1. `e:\Gemini\MaiBot\docs\CLEANROOM_REFACTOR_SPEC.md`（四道物理级记忆防线与净室基线，**严禁在本次开发中破坏或回退**）
> 2. 本执行指南全文（请严格按 **Step 1 -> Step 2 -> Step 3 -> Step 4** 顺序分步落地，每步均须跑通测试门禁）

---

## 零、 全局架构红线与防漂移铁律（Anti-Drift Guardrails）

在编写任何一行代码前，必须死守以下 **6 条不可妥协的工程红线**：

1. **严禁牺牲预设思考质量换取速度**：
   - 在精修 `Meowssiah` 预设时，`[43] 🧪强制历史设定回顾`（“把世界设定和历史记录的剧情总结按顺序从头到尾读一遍……提取出原文”）**必须一字不改保持 `ON`**；
   - `[20~26]` 涩涩控制模块（`❤️防发情`、`❤️nsfw风格|正常`、`❤️ASMR对话`）**必须一字不改保持原版状态与原文**；
   - `[47] 📏小总结 (<summary>)` 与 `[48] 📏猫猫留言 (<comment>)` **必须保持 `ON`**，其输出在发往微信前由解包器剥离（微信端只发 `<game>` 内的纯净真人回信），但必须连同 CoT 思维链（`<thinking>` / `<think_nya>` / 原生 `reasoning`）完整送达原生 WebUI `/chat` 对应轮次的「思考记录」卡片展示！
2. **严禁破坏物理级记忆与私密设定隔离（Fail-Closed）**：
   - 任何涉及 `is_owner_private` 或挂载 `owner_private_prompt` / `owner_private_notes` 的地方，**绝对禁止仅用 `not is_group_chat` 判断**！普通好友也可能与机器人私聊。必须通过 `src/companion_vault/runtime_bridge.py` 的 `_resolve_trust_role` 严格校验 `(not is_group_chat) and (sender_trust_role == "OWNER")`。
3. **严禁硬编码离场剧情、角色名或算力阈值**：
   - 姐姐的离场事由、省流期间的场外状态演进、充值恢复后的回归衔接，**100% 由 `TwinNarrativeDirector` 调用 LLM（结合 `Asia/Tokyo` 实时时间、最近聊天上下文与 WebUI 配置的方向约束 Prompt）动态生成**，严禁写死固定模板句子；
   - 双子人设、算力阈值（默认 `15.0 / 3.0 / 0.5 USD`）全部收归 MaiBot 原生配置模型（`/config/bot` 与 `/config/model`），支持 WebUI 热修改与热重载，无需重启主进程。
4. **严禁在 WebUI 另起野页面应付了事**：
   - 双子人设分槽配置与算力经济配置，**必须直接改造 MaiBot 原生前端 `dashboard/src/routes/config/bot.tsx` 与 `PersonalityProfiles.tsx`**；
   - CoT 思维链、`<summary>` 小总结与 `<comment>` 猫猫留言，**必须直接改造原生监控时间线 `dashboard/src/routes/monitor/maisaka-monitor.tsx`**，并执行 `npm run build` 通过 TypeScript 编译与前端单测。
5. **严禁让伪造工具信封污染 Planner 原生工具链**：
   - Replyer 可使用 `spreset_tool_envelope`（`soliumbra_reply`）或 `standard_chat`；
   - Planner 必须挂载真实的 Maisaka 工具集（`reply`、`wait`、`stop`、`run_agent_workspace` 等）并遵守 `chat_loop_service.py:_validate_function_call_context_anchors` 校验与 Gemini KV Cache 前缀稳定性，因此 Planner 采用同源提取的 **「ST 预设导演剪辑版」三段式注入结构**，绝不在 Planner 注入 `soliumbra_reply` 伪函数。
6. **预设资产必须可入库追踪**：
   - 由于 `.gitignore` 忽略 `data/` 目录，精修后的 `Meowssiah-WeChat-IM.json` 必须保存在入库路径 `plugins/st_preset_engine/templates/Meowssiah-WeChat-IM.json`，并在 `STPresetRuntimeManager.__init__` 中检测若 `data/presets/Meowssiah-WeChat-IM.json` 不存在则自动拷贝初始化，确保新环境与云端拉取代码即自带该预设。

---

## 一、 Step 1：精修 `Meowssiah-WeChat-IM.json` 预设与解包器升级

### 1.1 基于 `E:\Downloads\Meowssiah-2.0.11.json` 生成特调预设

读取 `E:\Downloads\Meowssiah-2.0.11.json`（若不可读可回退 `data/presets/Meowssiah-2.0.11.json`），生成精修版 `plugins/st_preset_engine/templates/Meowssiah-WeChat-IM.json`（同步写入 `data/presets/Meowssiah-WeChat-IM.json`），精确修改规则如下：

1. **100% 保持原样不动的节点**：
   - `[00] ~ [19]`：包含 `token重定义+初始化`、`免责声明`、`用户角色`、`召唤术`（小猫之神踢飞 Google AI）、世界书与角色包裹块、`上下文构造-衔接`、`chatHistory`、`继续游戏`、`😸情绪别太激烈`、`😸活人感加强`；
   - `[20] ~ [26]`：涩涩模块全部维持原样（`[21] ⛔️关闭NSFW` 为 `OFF`，`[22] ❤️防发情` 为 `ON`，`[24] ❤️nsfw风格|正常` 为 `ON`，`[26] ❤️ASMR对话` 为 `ON`）；
   - `[43] 🧪历史回顾`（`42303fec-82ae-4ec4-96a8-4cd424d782e5`）：**原文一字不改，保持 `ON`**；
   - `[45] 📏<game>标签（别关）`、`[47] 📏小总结`（`<summary>` 高密度场记卡）、`[48] 📏猫猫留言~`（`<comment>`）、`[49] 📏其它格式`、`[50] 【别关】setLastMessage`：**全部保持 `ON` 且原文不变**！
   - *补充细节*：在 `[00]` 初始化说明中兼容声明思考过程包裹在 `<thinking>...</thinking>`（或 `<think_nya>...</think_nya>`）标签中，思考时保持小猫可爱语气并完整执行 `[43]` 的历史与设定原文回顾。
2. **关闭的 2 个冗余节点（在 `prompt_order` 中将 `enabled` 设为 `false`）**：
   - `[35] 📝字数要求`（`9fcf4beb-5996-4d01-8490-7332502bc422`，原要求写够 1000 字）$\rightarrow$ **`enabled: false`**；
   - `[46] 📏行动选项`（`046af93d-0c63-4561-b0b2-052cd265fecd`，原要求输出 3 个 `<options>`）$\rightarrow$ **`enabled: false`**。
3. **重写 `[30]` 主文风节点（`eda5ebe9-54d0-455c-878e-7dbe7d0ec8c0`）**：
   - 将 `name` 改为 `"📝文风（选一）丨s喵特调·真人回信"`，保持 `enabled: true`，将 `content` 替换为以下完整调教文本：


```markdown
<<<|catgodml|radio:主文风选择:📝s喵特调·真人回信>>>
一定要按这些要求写w，写好了给你加双倍草莓味小鱼干！
当前角色正在手机微信上实时打字回消息，`<game></game>` 内只能出现角色真正敲进微信输入框发出去的文字气泡！

# 在场感与生活切口（替代第三人称外貌/场景旁白）
## 严禁在 `<game>` 内使用任何小说旁白、动作星号或括号文学（如 `（轻笑一声）`、`*歪了歪头*`、第三人称外貌或环境描写）！真人发微信绝不会给自己配旁白。
## 当需要传递此刻的物理状态、所处环境或身体细节时，必须将“镜头”转化为**第一人称随口带出的生活碎屑与即时体感**。以下是几个优质示例：
- 刚从本乡那边的琴房跑回来，外面雨下得没完没了，伞边全湿透了，现在正光着脚踩在地毯上缓气呢

- 先等我两分钟，水刚烧开，我去吧台冲杯深烘豆子再回你刚才那段话

- 指尖有点酸，刚才那段大提琴揉弦连拉了快一小时……手机差点没拿稳砸脸上

- 被窝里好暖和根本不想伸手出来打字，你刚刚发那张图把我彻底看精神了

# 信息颗粒度与呼吸断句（模拟真人手机打字流）
## 真人发微信极少一次性敲出一整块密不透风的四百字公文段落，而是顺着念头边想边敲、分段发送。
## 用单个换行符区分同一轮先后发出的多条短微信气泡：
- **日常随口接梗 / 熟人闲聊**：1~3 条轻快短句，长短错落，视情况自然省略主语、使用口语倒装，句末不硬加死板的句号“。”；
- **认真探讨技术 / 深夜倾诉 / 高浓度亲密拉扯**：允许连发 3~5 条更有分量、细节绵密的气泡，但每一条依然保持手机打字的自然呼吸感。
## 不必做“阅读理解式”的面面俱到回复！如果对方连发三件事，只挑最戳中角色当下情绪、最好奇或最想吐槽的一两个点切入即可。

# 潜台词与情绪藏匿铁律（继承 s喵特调核心）
## 角色的情绪、心动、嫌弃、护短或羞赧，绝对不要靠角色自己直白喊出“我很害羞”、“我有点生气”或生硬背诵自己的人设标签来表现，而是藏进**选词偏好、停顿节奏、转移重点的小心机与反问语气**里。
## 极力消灭一切“AI 客服与教导主任腔”：
- 严禁总结复述对方刚说的话（“听起来你今天遇到了……”）；
- 严禁列点作答（1、2、3）或使用公文连接词（“首先”、“总而言之”、“某种程度上”）；
- 严禁句末机械式抛回问题（“你觉得呢？”、“你今天过得怎么样？”）；
- 严禁任何居高临下的道德劝诫、安全提醒或打圆场和稀泥！遇到损友互怼、离谱暴论或擦边开车话题，按角色自己的脾气自然接梗、冷淡吐槽或顺势反撩。

# 气泡收尾刀法
## 话说到最自然的地方直接停住发送，就像真人在微信上敲完最后一行随手按了发送键一样干脆。
## 不需要任何“让我们拭目以待”、“今晚的夜色很美”式的文学收束感；未尽的情绪留给对方的下一条回复去碰撞。
说完了！
```

### 1.2 升级 `src/companion_vault/st_preset_engine.py` 编译与解包逻辑

1. **扩展 CoT 思维链标签提取正则（约第 51 行）**：
   - 将 `_THINK_NYA_RE` 扩展为同时兼容 `<thinking>`、`<think>`、`<think_nya>`、`<report_nya>`：
     ```python
     _THINK_NYA_RE = re.compile(
         r"<(?:thinking|think|think_nya|report_nya)~?>([\s\S]*?)</(?:thinking|think|think_nya|report_nya)~?>",
         re.IGNORECASE,
     )
     ```
   - 在 `unwrap_response()` 中增加可选参数 `native_reasoning: str = ""`：若底层模型返回了原生 `reasoning` 且与标签内提取的思考链不重复，合并为完整的 `think_nya` 输出；同时确保所有 `<thinking>` / `<think>` 标签从发往微信的 `clean_story` 中被彻底剔除。
2. **保持 `<summary>` 与 `<comment>` 在 `im_clean` 模式下的完整产出（约第 641~680 行）**：
   - 修正 `sync_payload["im_mode_hint"]`，不得压制 `<summary>` 和 `<comment>` 的生成！明确指示：
     *“`<game></game>` 内只输出角色敲进微信输入框的真实短气泡台词（无括号动作、无千字网文）；在 `</game>` 之后，必须继续按已开启的规则完整输出 `<summary></summary>` 高密度场记状态卡与 `<comment></comment>` 猫猫留言！”*
   - 在 `soliumbra_reply` 的 `parameters_schema.properties` 中同时兼容模型把 `<summary>`/`<comment>` 写在 `game_content` 字符串尾部或独立可选字段中，`unwrap_response` 双路提取，保证万无一失。
3. **新增 `extract_director_framing_blocks(ctx: STCompileContext) -> str` 方法**：
   - 从当前激活预设中自动提取供 Planner 使用的同源破限与角色控制内核（保留免责声明、猫神去审查基调、`😸情绪别太激烈`、`😸活人感加强`、`[22/24/26]` 涩涩与亲疏边界、`[43] 🧪历史回顾` 原文；自动跳过 `soliumbra_reply`、`<game>`、`<options>`、`<summary>`、`<comment>` 等纯声带排版块）。
4. **模板自举初始化**：
   - 在 `STPresetRuntimeManager.__init__` 中，若 `self.presets_dir / "Meowssiah-WeChat-IM.json"` 不存在且仓库内 `plugins/st_preset_engine/templates/Meowssiah-WeChat-IM.json` 存在，自动复制一份到 `self.presets_dir` 并默认将其设为默认预设文件。


---

## 二、 Step 2：MaiBot 底层双子槽位、算力限制配置与动态切模引擎落地

### 2.1 原生人设与算力经济配置模型扩展（`src/config/official_configs.py` + `src/config/config.py`）

1. **升级 `PersonalityProfile`（`src/config/official_configs.py:148`）**：
   保持对旧 `bot_config.toml` 的 100% 缺省兼容（全部给 `default`），新增以下字段：
   - `nickname: str = Field(default="")`：该角色独立显示与自称的名字（为空时回退 `global_config.bot.nickname`）；
   - `alias_names: List[str] = Field(default_factory=list)`：该角色专属别名（同时更新 `src/platform_io/wechat/projection.py:get_dynamic_bot_aliases()`，合并 `bot.alias_names` 与双子两个槽位 Profile 的 `nickname` + `alias_names`，使群聊 `@妹妹` 或 `@姐姐` 均能原生识别提及！）；
   - `owner_private_prompt: str = Field(default="")`：**Owner 专属私密提示词**（仅当 `not is_group_chat and sender_trust_role == "OWNER"` 时挂载，普通好友私聊与一切群聊物理不可见）；
   - `st_preset_file: str = Field(default="")`：可选绑定的专属酒馆预设文件名（为空则使用全局激活预设）。
   - *同步更新*：`src/config/personality_service.py` 中的 `_FIELDS` / `current_profile` / `persist_switch`，确保切换或保存 Profile 时上述新增字段及 `bot.nickname` / `bot.alias_names` 原子同步，不丢失任何字段。

2. **升级 `PersonalityConfig`（`src/config/official_configs.py:169`）**：
   新增双子槽位绑定字段与解析方法：
   ```python
   twin_mode_enabled: bool = Field(default=True, json_schema_extra={"x-widget": "custom"})
   younger_slot_profile: str = Field(default="", json_schema_extra={"x-widget": "custom"})
   elder_slot_profile: str = Field(default="", json_schema_extra={"x-widget": "custom"})
   ```
   并在 `PersonalityConfig` 上实现辅助方法：
   `resolve_slot_profile(self, slot: str, bot_cfg: BotConfig, reply_style_cfg: ChatReplyStyleConfig) -> PersonalityProfile`：
   - 当 `slot == "sister_elder"` 且 `elder_slot_profile` 在 `self.profiles` 中命中时，返回姐姐的完整 `PersonalityProfile`；
   - 当 `slot == "sister_younger"` 且 `younger_slot_profile` 在 `self.profiles` 中命中时，返回妹妹的完整 `PersonalityProfile`；
   - 若尚未配置对应 Profile 名称，优雅回退为当前激活的顶层 `personality` + `bot` + `chat.reply_style` 快照，绝不抛异常崩溃。

3. **新增 `TwinEconomyConfig(ConfigBase)` 并注册进全局 `Config` 与 WebUI Schema**：
   - 在 `src/config/official_configs.py` 定义 `TwinEconomyConfig`（设 `__ui_parent__ = "bot"`，`__ui_label__ = "双子与算力"`）：
     - `enabled: bool = True`（是否启用算力水位联动与双子路由）
     - `balance_api_url: str = "https://api.0-0.pro"`（算力网关余额地址）
     - `poll_interval_sec: float = Field(default=90.0, ge=15.0, le=3600.0)`（后台余额校准轮询间隔秒数）
     - `supercharged_threshold_usd: float = Field(default=15.0, ge=0.0)`（充裕超频态水位线 USD）
     - `thrifty_threshold_usd: float = Field(default=3.0, ge=0.0)`（吃紧省流态 / 姐姐退场水位线 USD）
     - `hibernating_threshold_usd: float = Field(default=0.5, ge=0.0)`（断粮休眠态水位线 USD）
     - `strict_flash_in_thrifty: bool = True`（省流态严格锁死 Flash：余额低于 `thrifty_threshold_usd` 期间，哪怕 Owner 私聊或 `@` 也物理锁定 Flash 妹妹值班）
     - `away_evolve_interval_hours: float = Field(default=6.0, ge=0.5)`（省流期间姐姐场外状态自动演进间隔小时数）
     - `chat_execution_mode: Literal["dual_director", "one_pass_st"] = "dual_director"`（双层导演模式 / 私聊一步直出模式）
     - `away_direction_prompt: str`、`evolve_direction_prompt: str`、`return_direction_prompt: str`（3 个 `x-widget: "textarea"` 字段，默认值直接复用 `twin_narrative_router.py` 现有高质量默认方向约束，供用户在 `/config/bot` 动态修改！）
   - 在 `src/config/config.py:Config`（约第 123 行）注册 `twin_economy: TwinEconomyConfig = Field(default_factory=TwinEconomyConfig)`；
   - 在 `src/webui/routers/config.py:get_config_section_schema` 的 `section_map` 中注册 `"twin_economy": TwinEconomyConfig`。

---

### 2.2 模型任务层扩展与优雅回退守卫（`src/config/model_configs.py` + `src/services/service_task_resolver.py`）

1. **在 `ModelTaskConfig`（`src/config/model_configs.py:440`）新增两个双子专用任务槽位**：
   ```python
   planner_flash: TaskConfig = Field(
       default_factory=TaskConfig,
       json_schema_extra={"x-widget": "custom", "label": {"zh_CN": "轻快/省流规划模型 (planner_flash)"}},
   )
   """双子模式下日常群聊与省流态（<阈值）使用的 Flash 规划模型；未分配时自动回退至 planner"""

   replyer_elder: TaskConfig = Field(
       default_factory=TaskConfig,
       json_schema_extra={"x-widget": "custom", "label": {"zh_CN": "姐姐专属回复模型 (replyer_elder)"}},
   )
   """双子模式下姐姐（sister_elder）开口使用的精锐回复模型（如 Gemini Argon）；未分配时自动回退至 replyer"""
   ```
2. **在 `src/services/service_task_resolver.py:resolve_task_name` 增加空列表回退守卫**：
   ```python
   TASK_EMPTY_FALLBACK_MAP = {
       "planner_flash": "planner",
       "replyer_elder": "replyer",
   }
   ```
   当请求的 `normalized_task_name` 位于映射表中，且其 `TaskConfig.model_list` 为空列表时，自动回退返回对应的主任务名（`"planner"` 或 `"replyer"`），确保用户尚未在 `/config/model` 单独勾选模型前系统 100% 零报错运行！

---

### 2.3 核心路由仲裁器：`TwinRuntimeRouter` 与实时计费闭环

1. **全局实时计费挂钩（`src/services/llm_service.py`）**：
   - 在 `src/companion_vault/compute_ledger.py` 提供进程级懒加载单例函数 `get_shared_compute_ledger() -> ComputeLedger`（自动对齐 `global_config.twin_economy` 的阈值与 `balance_api_url` 配置，并在主事件循环启动后台轮询）。
   - 在 `LLMServiceClient.generate_response`、`generate_response_from_snapshot` 与 `generate_response_with_context`（`src/services/llm_service.py:303, 309, 359`）拿到 `result` 后，统一调用非阻塞计费钩子（`try ... except Exception` 包裹防止任何数据库锁影响主对话）：
     ```python
     get_shared_compute_ledger().record_token_usage(
         model_name=result.model_name or "",
         prompt_tokens=result.prompt_tokens,
         completion_tokens=result.completion_tokens,
     )
     ```

2. **新建 `src/companion_vault/twin_runtime_router.py`**：
   定义不可变路由决策数据类 `TwinTurnDecision` 与异步入口 `resolve_turn_route(...)`：
   ```python
   @dataclass(frozen=True)
   class TwinTurnDecision:
       abort_turn: bool = False
       abort_reason: str = ""
       planner_task_name: str = "planner"                 # "planner" (Argon) | "planner_flash" (Flash)
       allowed_slots: Tuple[str, ...] = ("sister_younger", "sister_elder")
       forced_slot: Optional[str] = None                  # 省流/休眠态强制锁死 "sister_younger"
       sender_trust_role: str = "GROUP_MEMBER"
       is_owner_private: bool = False
       balance_usd: float = 5.0
       compute_tier: str = "STANDARD"
       elder_away_arc: str = ""
       transient_narrative_note: str = ""                 # 刚离场/刚回归/断粮求援等本轮一次性导演场记
       execution_mode: Literal["dual_director", "one_pass_st"] = "dual_director"
   ```
   **精确状态机判定流程**：
   - **Step A（身份鉴权）**：调用 `runtime_bridge._resolve_trust_role_sync` 获取 `sender_trust_role`；严格计算 `is_owner_private = (not is_group_chat) and (sender_trust_role == "OWNER")`。
   - **Step B（断粮休眠态：`balance_usd < cfg.hibernating_threshold_usd`）**：
     - 若 `not is_owner_private` $\rightarrow$ 返回 `TwinTurnDecision(abort_turn=True, abort_reason="hibernating_non_owner")`（在 `reasoning_engine.py` 入口直接静默结束本轮，**0 Token 消耗**）；
     - 若 `is_owner_private` $\rightarrow$ 放行，`planner_task_name="planner_flash"`，`forced_slot="sister_younger"`，`allowed_slots=("sister_younger",)`，`transient_narrative_note` 注入断粮低功耗求援状态。
   - **Step C（吃紧省流态：`balance_usd < cfg.thrifty_threshold_usd`）**：
     - **首次跌破生成离场状态**：若当前 `elder_away_arc` 为空，通过 `LLMServiceClient(task_name="planner_flash")` 调用 `TwinNarrativeDirector.generate_elder_away_arc()`（传入 `cfg.away_direction_prompt` + 最近 5 条可见对话文本），将生成的动态离场弧线持久化至 `global_state.elder_away_arc`（同时记录时间戳）；
     - **跨时段异步演进**：若已有 `elder_away_arc` 且距上次更新超过 `cfg.away_evolve_interval_hours * 3600` 秒，通过 `asyncio.create_task` 后台非阻塞调用 `evolve_away_arc()` 推进姐姐的场外进度；
     - **物理锁死 Flash**：若 `cfg.strict_flash_in_thrifty` 为 `True`，强制 `planner_task_name="planner_flash"`，`forced_slot="sister_younger"`，`allowed_slots=("sister_younger",)`。
   - **Step D（标准/充裕态：`balance_usd >= cfg.thrifty_threshold_usd`）**：
     - **充值回血触发姐姐回归**：若检测到数据库中 `elder_away_arc` 非空，调用 `TwinNarrativeDirector.generate_elder_return_arc()` 生成自然回归衔接锚点放入 `transient_narrative_note`，随即清空数据库中的 `elder_away_arc`；
     - **动态决定 Planner 模型**：
       - 满足任一条件则 `planner_task_name = "planner"`（满血 Argon）：
         1. `is_owner_private == True`（Owner 私聊）；
         2. 群聊中本批触发消息含 `sender_trust_role == "OWNER"` 且提及/`@` 了机器人（含任一姐妹昵称或别名）；
         3. `balance_usd >= cfg.supercharged_threshold_usd` 且群消息明确点名呼叫姐姐的昵称/别名。
       - 其余日常普通群聊或普通朋友私聊 $\rightarrow$ `planner_task_name = "planner_flash"`，`allowed_slots = ("sister_younger", "sister_elder")`。

---

### 2.4 Planner 导演剪辑版升级、Replyer 分槽切模与 `One-Pass` 接线

1. **Planner 端动态切模与「ST 预设导演剪辑版」挂载（`src/maisaka/chat_loop_service.py` & `src/maisaka/reasoning_engine.py`）**：
   - 在 `reasoning_engine.py` 每轮进入 Planner 前调用 `decision = await resolve_turn_route(...)` 并存入 `self._current_turn_decision`；若 `decision.abort_turn` 为真，立即静默收敛结束轮次。
   - 在 `chat_loop_service.py:_get_llm_chat_client(self, request_kind: str, task_override: Optional[str] = None)`（第 711 行）：若 `request_kind == "planner"` 且传入了 `task_override`（即 `decision.planner_task_name`），使用该任务名从 `self._llm_chat_clients` 获取或创建 `LLMServiceClient`（同一会话零开销动态切换 Argon / Flash）！
   - **三段式导演提示词注入（严保 KV Cache 与工具锚点稳定）**：
     - **段 1（静态 System 前缀，`_build_chat_system_prompt`）**：当 `twin_mode_enabled` 开启时，拼接：① `STPresetEngine.extract_director_framing_blocks()` 提取的去说教/反审查现实主义创作基座与 `[43] 强制历史回顾` 准则；② 妹妹（`sister_younger`）与姐姐（`sister_elder`）在 `/config/bot` 中配置的双人档案对比卡；③ Maisaka 原生工具使用规范。
     - **段 2（中间富多模态历史）**：保持原样不动，100% 命中 Gemini Context Cache。
     - **段 3（`@D0` 末尾动态场记与导演 RP CoT）**：在 `injected_user_messages` / `final_user_message` 注入：
       - 若 `is_owner_private == True`，挂载对应槽位在 `/config/bot` 配置的 `owner_private_prompt`；
       - 若 `decision.elder_away_arc` 非空，明确告知导演：“当前姐姐不在公寓吧台（离场状态：{elder_away_arc}），本轮禁止选 `sister_elder`，必须调用 `persona_slot='sister_younger'` 让妹妹回应，若对方找姐姐请结合离场状态自然代答”；
       - 若 `decision.transient_narrative_note` 非空（如刚充值回归），注入回归衔接提示；
       - **反软阉割硬锁 + 双子选角指导**：严禁在思考或 `reply_reference` 中写出“注意边界、温和转移话题、保持礼貌距离”等客服说教指令；在姐姐在场且气氛合适时，支持单选妹妹、单选姐姐、或在同一轮先后调用两次 `reply` 工具实现姐妹同屏接话互动！

2. **Replyer 端分槽模型与分槽 Profile 挂载（`src/maisaka/builtin_tool/reply.py` + `src/chat/replyer/maisaka_generator_base.py`）**：
   - 在 `reply.py:execute_reply` 中：若当前轮次 `decision.forced_slot` 存在（如省流态锁死 `sister_younger`），而 Planner 误传了 `persona_slot="sister_elder"`，自动覆写为 `"sister_younger"` 并在 `reply_reference` 附注“（系统提示：当前经费省流态姐姐离场中，请以妹妹身份结合姐姐离场近况自然代回）”。
   - 在 `maisaka_generator_base.py` 中：
     - 新增 `self.elder_express_model = LLMServiceClient(task_name="replyer_elder", ...)`；当 `effective_persona_slot == "sister_elder"` 时选用 `self.elder_express_model`（`active_task_name = "replyer_elder"`），否则选用 `self.express_model`（`"replyer"`）；
     - 调用 `slot_profile = global_config.personality.resolve_slot_profile(effective_persona_slot, global_config.bot, global_config.chat.reply_style)`，将 `STCompileContext` 的 `char_name`、`char_description`、`char_personality` **全部替换为该槽位姐妹的真实配置**（彻底修复此前写死单人 `global_config.bot.nickname` 的断路！）；
     - 严格计算 `is_owner_private = (not is_group_chat) and (sender_trust_role == "OWNER")`，仅当为 `True` 时将 `slot_profile.owner_private_prompt` 拼入设定并激活 `owner_only` 预设块；
     - 若本轮发言槽位与该会话上一次机器人发言槽位不同（换人拿手机），在 `director_note` 自动附带一句轻量换人衔接提示。

3. **私聊 `one_pass_st`（一步直出模式）支持**：
   - 当 `global_config.twin_economy.chat_execution_mode == "one_pass_st"` 且当前为私聊会话（`not is_group_chat`）时，支持跳过 Planner 二次传话折损，直接按路由决定的姐妹槽位与对应模型编译一次完整 `STPresetEngine` 请求生成回复并经 `send_service`（含 `OutputGuardrail`）发出，同时向监控总线推送包含 `st_telemetry` 的完整轮次事件。

---

## 三、 Step 3：原生 WebUI 双核心页面改造与编译（`/config/bot` + `/chat`）

> ⚠️ **静态资源生效关键提示**：MaiBot 后端 `src/webui/app.py:114, 299` 默认优先读取 Python `site-packages/maibot_dashboard/dist`，仅当环境变量 `MAIBOT_USE_LOCAL_DASHBOARD=1` 时优先读取仓库内 `dashboard/dist`！
> 因此完成前端修改并执行 `cd dashboard && npm run build` 后：
> 1. 必须在 `deploy/embodied/media.env.example` 与 `OPERATIONS_MANUAL.md` 中补齐 `MAIBOT_USE_LOCAL_DASHBOARD=1`；
> 2. 同时可在 `src/webui/app.py:_resolve_static_path()` 中优化：当仓库内 `dashboard/dist/index.html` 存在且显式开启环境变量（或包内资源缺失）时优先加载本地构建产物，确保自定义前端改动 100% 在开发与生产环境生效！

### 3.1 改造 `/config/bot`：双子分槽人设管理与算力经济配置面板

1. **`dashboard/src/lib/personality-import.ts`**：
   - 在 `PersonalityProfile` 接口中同步补齐可选新字段：`nickname?: string`、`alias_names?: string[]`、`owner_private_prompt?: string`、`st_preset_file?: string`（导入旧版 v1 JSON 时缺省为空字符串/空数组，保证旧测试 `personality-import.test.ts` 100% 兼容通过！）。
2. **`dashboard/src/routes/config/bot/PersonalityProfiles.tsx` & `dashboard/src/routes/config/bot.tsx`**：
   - 在「身份与人格」顶部新增 **【Gemini 双子槽位控制台】**：
     - `启用双子同居模式 (twin_mode_enabled)` Switch 开关；
     - `妹妹槽位 (sister_younger · Flash 常驻)`：下拉选择 `profiles` 中的人设名称 + `[切到妹妹编辑]` 按钮；
     - `姐姐槽位 (sister_elder · Argon 精锐)`：下拉选择 `profiles` 中的人设名称 + `[切到姐姐编辑]` 按钮；
   - **表单双向同步**：
     - 点击切换当前编辑人设时，同步切换页面下方的 `机器人昵称`、`别名`、`人格设定`、`行为风格`、`表达风格`、`备用表达风格`、`群聊提示词`、`私聊提示词` 以及新增在私聊提示词下方的 **`Owner 专属私密提示词（仅限最高信任角色私聊挂载）`** 多行文本框；
     - 用户在主表单修改任意字段时，自动同步回写 `personality.profiles` 中当前选中人设的对应字段，并通过现有的 `runWithAutoSaveBarrier(() => updateBotConfig(...))` 原子保存。
   - **渲染 `TwinEconomyConfig`（双子与算力限制面板）**：
     - 因为 `TwinEconomyConfig` 已声明 `__ui_parent__ = "bot"`，`DynamicConfigForm` 会自动在 `/config/bot` 的基础分类下生成「双子与算力」配置卡片（支持动态调整 `15.0 / 3.0 / 0.5 USD` 阈值、`strict_flash_in_thrifty`、`chat_execution_mode` 及离场/演进/回归 3 段方向 Prompt）！检查 `getDisplaySectionSchema` 确保未过滤掉 `twin_economy`。

---

### 3.2 改造 `/chat` 思考记录面板：对应轮次展示 CoT 思维链、`<summary>` 与 `<comment>`

> ⚠️ **必避暗坑（WebSocket 增量裁剪）**：
> 查看 `src/maisaka/monitor/planner_delta.py:19-22`，`project_planner_snapshot` 在向 `/chat` 推送 WebSocket 快照时会执行 `{key: value for key, value in tool.items() if key != "detail"}` 剥离 `detail` 字段！
> **绝对不能只把 `<thinking>` / `<summary>` / `<comment>` 塞进 `monitor_detail["extra_sections"]` 就以为前端能收到！**

**标准打通路径（4 个文件精准改动）**：

1. **`src/chat/replyer/maisaka_generator_base.py`（约第 1472~1670 行）**：
   - 调用 `STPresetEngine.unwrap_response(..., native_reasoning=generation_result.reasoning or "")` 后：
     - 将发往微信的 `response_text` 设为 `unwrapped.game_content`（纯净无标签微信气泡）；
     - 将提取出的完整幕后结构存入 `result.metrics.extra["st_telemetry"]`：
       ```python
       result.metrics.extra["st_telemetry"] = {
           "persona_slot": effective_persona_slot,
           "persona_name": slot_profile.nickname or bot_name,
           "thinking": unwrapped.think_nya,
           "summary": unwrapped.summary,
           "comment": unwrapped.comment,
       }
       ```
     - 同时在 `build_reply_monitor_detail`（`src/common/data_models/reply_generation_data_models.py`）中也把非空的 `summary` 与 `comment` 追加进 `extra_sections`（供推理详情页查看）。

2. **`src/maisaka/builtin_tool/reply.py` & `src/maisaka/reasoning_engine.py`**：
   - 在 `reply.py:_build_monitor_metadata` 中，若 `reply_result.metrics.extra.get("st_telemetry")` 存在，将其写入 `metadata["st_telemetry"]`；
   - 在 `reasoning_engine.py:2068` 构建 `tool_monitor_result` 字典时，若 `result.metadata.get("st_telemetry")` 非空，将其挂为 `tool_monitor_result["st_telemetry"] = normalize_tool_record_value(result.metadata["st_telemetry"])`。

3. **`src/maisaka/monitor/events.py:_serialize_tool_results`（第 222~265 行）**：
   - 若 `tool.get("st_telemetry")` 是非空字典，将其保留在 `serialized_tool["st_telemetry"]` 顶层字段上！这样 `planner_delta.py`（仅剔除 `key == "detail"`）就会原封不动地将 `st_telemetry` 通过实时 WebSocket 与 `history.load` 历史分页送达前端 `/chat` 页面！

4. **`dashboard/src/lib/maisaka-monitor-client.ts` & `dashboard/src/routes/monitor/maisaka-monitor.tsx`**：
   - 在 `MaisakaFinalizedToolResult` 接口新增可选字段 `st_telemetry?: { persona_slot?: string; persona_name?: string; thinking?: string; summary?: string; comment?: string }`；
   - 在 `maisaka-monitor.tsx` 的 `PlannerToolResultCard`（当 `tool.st_telemetry` 存在时，紧跟在 `ReplyToolArguments` 下方）使用原生 `CollapsibleText` 组件渲染：
     1. **值班角色徽章**：显示本次开口的是妹妹还是姐姐（如 `妹妹 · 许妍` / `姐姐 · 许清`）；
     2. **Replyer 思考链 (`<thinking>`)**（当 `st_telemetry.thinking` 非空时）：复用第 1231 行与 Planner「模型思考」一致的左边框柔和样式（`maxLines={6}` 可点击展开完整历史设定核验与猫神思考）；
     3. **📏 小总结 · 高密度场记卡 (`<summary>`)**（当 `st_telemetry.summary` 非空时）：独立卡片块展示 150~250 字场记状态卡；
     4. **🐱 猫猫留言 (`<comment>`)**（当 `st_telemetry.comment` 非空时）：轻量圆角气泡展示小猫之神的幕后悄悄话。

---

## 四、 Step 4：全套自动化门禁、实盘验证与文档同步验收清单

完成 Step 1 ~ Step 3 后，必须逐项跑通以下验收门禁（全部 0 错误方可结束任务）：

1. **后端全量回归与新增单测（`pytest`）**：
   - 新建 `pytests/companion/test_phase6_twins_and_wechat_preset.py`，必须覆盖：
     1. `Meowssiah-WeChat-IM.json` 结构完整性校验：断言 `[43]` 历史回顾为 `ON` 且原文未删减、`[22/24/26]` 涩涩模块未改动、`[47] <summary>` 与 `[48] <comment>` 为 `ON`、`[35]` 1000 字与 `[46]` `<options>` 为 `OFF`、`[30]` 为 `📝s喵特调·真人回信`；
     2. `<thinking>` / `<think_nya>` + `<game>` + `<summary>` + `<comment>` 四段解包测试：断言 `visible_text` 仅含纯净微信文本，且 `st_telemetry` 经 `events._serialize_tool_results` 与 `PlannerDeltaEncoder.encode` 后**未被裁剪丢失**；
     3. `TwinRuntimeRouter` 全状态机测试：`>= 15 USD` 超频态、`>= 3 USD` 标准态（Owner 私聊/`@` 走 `planner` Argon，普通群聊走 `planner_flash`）、跌破 `< 3 USD` 自动触发 LLM 生成 `elder_away_arc` 并锁死 `sister_younger` Flash、充值回到 `>= 3 USD` 自动触发 `generate_elder_return_arc` 并清空离场锁、`< 0.5 USD` 群聊 0 Token 静默拦截而 Owner 私聊放行求援；
     4. **私密隔离回归断言**：普通好友私聊（`is_group_chat=False, trust_role='GROUP_MEMBER'`）绝不注入 `owner_private_prompt` 与 `owner_only` 预设块；仅 `trust_role='OWNER'` 私聊注入；
     5. `service_task_resolver` 在 `planner_flash` / `replyer_elder` 未配置模型列表时自动回退 `planner` / `replyer` 测试。
   - 运行既有回归套件：
     `pytest pytests/companion/ pytests/maisaka/ pytests/platform_io/ pytests/webui/ tests/test_personality_service.py` —— **必须全部 Passed**。
2. **Python 静态代码检查**：
   - `ruff check` 检查所有新增与修改的 Python 文件 —— **0 Error / 0 Warning**。
3. **前端 TypeScript 类型检查、Vitest 单测与生产打包**：
   - 在 `e:\Gemini\MaiBot\dashboard` 下执行：
     - `npm run typecheck` —— **0 Error**
     - `npx vitest run src/lib/__tests__/personality-import.test.ts src/routes/config/bot/PersonalityProfiles.test.tsx src/lib/__tests__/maisaka-monitor-client.test.ts` —— **全部 Passed**
     - `npm run build` —— **成功生成 `dashboard/dist/`**
4. **真实 Gemini API 端点冒烟对比测试**：
   - 若环境中存在可用 API 配置，使用 `Meowssiah-WeChat-IM.json` 跑一次端到端编译与请求实测，确认真实返回的 `<thinking>`、`<game>` 真人微信短句、`<summary>` 与 `<comment>` 均完整生成且零道德说教。
5. **规范更新项目文档**：
   - 同步更新 `docs/EMBODIED_WORKSPACE_ITERATION.md`、`deploy/embodied/TECHNICAL_DEVELOPMENT.md`、`deploy/embodied/OPERATIONS_MANUAL.md` 与 `deploy/embodied/media.env.example`。

---

## 五、 Phase 7 演进：Persona V2 立体人设体系、3 层共享 MVU 引擎与全量双子人设（池之端 15F 版）

在 Phase 6 原生双子架构基础上，已完成基于《潜令V1.8》范式的 **Persona V2（`maibot.persona.v2`）** 与 **原生 3 层共享 MVU 动态变量引擎（`shared:twin_xu`）** 演进，并与真实居住地「东京都台东区池之端 `ブリリアタワー上野池之端` 15 层」双子人设闭环打通：

1. **Persona V2 立体人设编译器（[`src/companion_vault/persona_v2_engine.py`](../src/companion_vault/persona_v2_engine.py)）**：
   - **六维结构化 Schema**：`CorePersonaV2`（灵魂基调 `soul_pitch`、五维性格调色盘、多档状态机、防 OOC `meta_calibration` 二次解释、`SpeechRulesV2` 与违禁词约束）、`PastExperienceEntry`（常驻时间轴与生活场景切片）、`SampleCorpusEntry`（按情境与 `mvu_condition` 门控的多气泡纯微信台词库，内置 240 字符近距共现召回与防复读总则）与 `RelationshipStageOverlay`（`0~120` 6 阶段动态补丁）；
   - **双域物理隔离（Fail-Closed）**：严格区分 `general`（群聊与普通私聊）与 `owner_only`（仅 `(not is_group_chat) and (sender_trust_role == "OWNER")`）；非 Owner 私聊与群聊物理剔除一切 `owner_only` 锚点、生理/亲密语料与私有阶段指示；
   - **KV Cache 友好动静分离**：静态常驻基础层固化至静态前缀；动态阶段补丁、命中语料与 MVU 状态挂载至 `call_sync_final` / `@D0` 尾注，保持前缀缓存稳定；
   - **全量双子人设与内置《潜令》角色卡**：`data/personas/` 与 `plugins/st_preset_engine/templates/personas/` 同步内置 `maibot_xuyue`（**许玥**）、`maibot_xuyan`（**许妍**）全量版及其 `*_qianling_full` 全量备份版本（每卡含 14 条过往经历与 32 组五段式微信 IM 语料），以及《潜令V1.8》四角色模板（`qianling_yun_suining`、`qianling_yun_ling`、`qianling_yun_xian`、`qianling_yun_sui`）。

2. **原生 3 层共享 MVU 引擎（[`src/companion_vault/mvu_engine.py`](../src/companion_vault/mvu_engine.py) + [`src/companion_vault/truth_db.py`](../src/companion_vault/truth_db.py)）**：
   - **Layer 1（系统只读注入）**：实时从 `CompanionTruthDB.global_state.fairy_compute_tier` 映射 `balance_tier`（`high`/`medium`/`low`），从激活槽位映射 `phone_holder`（`xuyue`/`xuyan`），注入 `<status_current_variables>` 顶部供 `mvu_condition` 读取，并严防模型 `<UpdateVariable>` 篡改；
   - **Layer 2（跨卡共享命名空间 `shared:twin_xu`）**：通过 `MVUVariableSchema.shared` 与 `shared_key` 将 `apartment_scene`（`PUBLIC` 物理实景与姐姐离场钩子）及 `love_locked`（`PRIVATE`、`shared_key: "twin_harmony_locked"`、`monotonic_true: true` 的 `>100` 双生破局锁）统一路由至 `mvu_variables` 的 `shared:twin_xu` 作用域，实现姐妹两卡状态实时同步与群聊 `PRIVATE` 物理阻断；
   - **Layer 3（单卡多维私有变量与零死区阶段匹配）**：支持 `+0.5` 浮点好感步进零死区匹配 `0~120` 阶段（`love_locked=False` 前由 `affinity_guard` 封顶 `100.0`，解锁后开放 `101~120` `stage_101_120_harmony`）；支持 `control_tension` / `intimacy_tension` 复合条件（`affection >= 60 and control_tension >= 50`）与 `pending_tease_pledge` / `sister_rivalry_mode` 追踪；
   - **出站剥离与遥测回传**：在 `STPresetEngine.unwrap_response()` 与 `maisaka_generator_base.py` 提取 `<UpdateVariable><Analysis>...<JSONPatch>...` 并将其从出站 `visible_text` 与对话历史彻底剥离，Diff 挂载至 `st_telemetry["mvu_watch"]`。

3. **A / B / C 链路缺口核查与闭环补全**：
   - **ComputeLedger 全接口闭环（`src/services/llm_service.py`）**：`generate_response`、`generate_response_from_snapshot`、`generate_response_with_context` 与 `generate_response_for_image` 全路径均已挂载非阻塞 `_record_compute_usage(result)` 实时计费钩子；
   - **Twin 路由传递与回落闭环**：`ChatLoopService._resolve_model_task_name` + `chat_loop_step` 始终透传 `decision.planner_task_name`（即使等于默认任务名亦明确透传）；`reply.py` 与 `reasoning_engine.py` 在 `forced_slot` 未设时亦继承 `decision.recommended_slot`；
   - **WebUI 工作台与 `/chat` 监控闭合**：[`PersonaV2Workbench.tsx`](../dashboard/src/routes/config/bot/PersonaV2Workbench.tsx) 与 [`src/webui/routers/persona_v2.py`](../src/webui/routers/persona_v2.py) 落地可选人物卡片架、双域视角过滤与 MVU 实时调参；`maisaka-monitor.tsx` 与 `MessageList.tsx` 修复 `sister_elder` 槽位徽章与「许玥/玥姐/阿玥」头像/名字识别，完整渲染 `st_telemetry` 中的值班徽章、`<thinking>`、`📏<summary>`、`🐱<comment>` 与 `📊 mvu_watch`。
