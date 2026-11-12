# Phase 1~4 净室重构规范与双子世界观预留蓝图（Cleanroom Refactoring Specification）

> **文档状态**：v1.1 全三批次净室重构已落地验收（Batch 1 ~ Batch 3 100% 完成，第六章 `STPresetEngine` 本体按计划留待下一迭代）  
> **适用仓库与分支**：`MaiBot` (`feat/embodied-workspace-phase4-twins`，审计基线 `2bbcae1d4`)  
> **目标场景定位**：**个人（OWNER）+ 身边少数熟人朋友的私享小圈子日常使用**  
> **关联审计档案**：[`docs/EMBODIED_WORKSPACE_ITERATION.md`](./EMBODIED_WORKSPACE_ITERATION.md)（第十章、第十一章）

---

## 一、 重构核心哲学与设计边界（不做过度设计，专治痛点与补丁）

鉴于本项目仅服务于号主本人（OWNER）与身边少数朋友，重构摒弃面向公网陌生攻击者或高并发 SaaS 的“企业级过度工程”，聚焦以下四条黄金准则：

| 治理维度 | ❌ 明确不做（避免过度设计） | ✅ 必须精准落地（私享场景核心痛点） |
| :--- | :--- | :--- |
| **安全与身份边界** | 不引入 OAuth2/JWT 复杂网关、不搞公网 WAF/IP 封禁策略、不限制熟人在群协作网页中的正常交互。 | 1. **防密钥外泄被爬虫盗刷**：彻底清除源码中的明文 `sk-...` Key，沙盒启用 `--clearenv` 环境变量白名单；<br>2. **防熟人社交“社死串台”**：修复私聊链接转发给朋友即向其签发 365 天 OWNER 全局身份 Cookie 的漏洞；修复群聊自动画像把私密 `impression`（吐槽/亲密设定）注入群 Prompt 的越权问题；<br>3. **防特殊昵称炸网页**：朋友微信名含颜文字/`<_>`/引号时，前端一律使用 DOM `.textContent` 安全渲染。 |
| **沙盒与单机稳定性** | 不引入 Docker/K8s 容器编排、不引入分布式调度节点或复杂 seccomp 系统调用表。 | 1. **保卫单台 VPS（`DMIT-US`）内存与进程树**：为 `systemd-run --scope` 赋予确定性 `--unit` 名称，超时/休眠/卸载时通过 `systemctl kill` 连根回收整个 cgroup，杜绝孤儿进程泄漏；<br>2. **任务级附件隔离**：按 `inbox/<job_id>/` 硬链接挂载授权原件，任务结束即清理，零双倍磁盘占用且不串前序任务文件。 |
| **数据存储与职责** | 不引入 Redis / PostgreSQL / 消息队列，保持纯 SQLite WAL + 本地文件系统的极简架构。 | 1. **单一 Schema 事实源**：严禁业务插件在运行期直连 `companion_truth.db` 动态 `ALTER TABLE` 制造错位表结构，所有真值库列演进统一收归 `src/companion_vault/truth_db.py`；<br>2. **群级待办不串群**：群网页提交的约定绑定 `scope_chat_id`，A 群待办绝不出现在 B 群常驻提示词中。 |
| **代码洁癖与契约** | 不保留兼容错误单测的双轨死分支、不留已废弃的早期快轨与旧 UI 探针死代码。 | 100% 对齐 MaiBot `1.3.19` 宿主 `plugin_runtime` 原生协议（`@Command` 命名捕获组、标准 Runner Hook 返回结构、真实 Capability 参数签名），删净 `except TypeError` 盲猜补丁。 |

---

## 二、 现有 7 大功能点的净室重构实现细则

### 1. 统一慢轨构建（`document` / `webapp` / `backend`）与快轨人设进度播报
- **清理冗余与死代码**：
  - 物理删除 `plugins/embodied_workspace/fast_renderer.py` 及 `plugin.py` 中的无用导入与 `publish_document_page` 空壳转发方法；
  - 更新 `plugins/embodied_workspace/_manifest.json`：从 `capabilities` 移除不再使用的 `"maisaka.reply.generate"`，插件仅暴露 `@Tool("run_agent_workspace")` 与 `@Tool("manage_workspace_projects")`。
- **恢复真实配置契约**：
  - 移除 `plugin.py` 中 `max(10800, int(self.config.cli_timeout_seconds or 10800))` 的暴力硬钳制，改为 `max(30, int(self.config.cli_timeout_seconds))`（默认值仍为 `10800` 秒 = 3 小时）；
  - 新增配置项 `fairy_cli_model: str = "gemini-4-argon-high"` 与 `cli_auth_dir: str = "~/.gemini"`，解除对模型名与 `/root/.gemini` 路径的硬编码。
- **外置构建规范与移除脆弱正则**：
  - 将 `cli_bridge.py` 内嵌的长篇 `AGENTS.md` 字符串抽离为独立模板 `plugins/embodied_workspace/templates/FAIRY_AGENTS.md`；
  - 移除 `editorial_kit.py` 中篡改 `<pre class="mermaid">` 的两条客户端正则，避免破坏正常符号，改为在 `CLIBridge` 收尾校验产物时做轻量检查与构建规范约束。
- **对齐宿主原生 Capability 与多任务状态隔离**：
  - 调用 `maisaka.context.append` 仅传宿主真实消费的 `(stream_id, segments, visible_text, source_kind)`；调用 `maisaka.proactive.trigger` 仅传 `(stream_id, intent)`，删除无效的 `target_id` / `source_message_id` 伪参数；
  - 运行中任务状态按 `self._active_jobs[job_id]` 多槽维护（不再单列覆写 `global_state` 导致两群并发任务互相清空进度）；`on_unload()` 显式遍历 `cancel()` 并 `await` 回收所有在途后台任务。

### 2. 净室沙盒隔离（`bwrap`）、cgroups v2 单元管控与附件暂存
- **环境变量净室白名单（`cli_bridge.py`）**：
  - 彻底删除 `build_sandbox_env()` 中的明文 `sk-...` 密钥及 `dict(os.environ)` 全量复制；
  - `build_bwrap_prefix` 增加 `--clearenv`，仅显式注入白名单变量：`PATH`、`HOME=/tmp/home`、`XDG_CACHE_HOME`、`XDG_CONFIG_HOME`、`LANG=C.UTF-8`、`TZ=Asia/Tokyo`、`PORT`（后端模式），以及从宿主受控环境变量 `FAIRY_SANDBOX_API_KEY`（回退 `GEMINI_API_KEY`）和 `FAIRY_SANDBOX_BASE_URL` 读取的网关凭据（缺失时立即返回可读错误）。
- **确定性 cgroups v2 进程树回收（`resource_quota.py` & `backend_supervisor.py`）**：
  - 修复 `ResourceQuotaWrapper.wrap_command(base_cmd, unit_name: str)`：实际向 `systemd-run --scope` 传入 `--unit={unit_name}`（如 `maibot-fairy-cli-{job_id}` 或 `maibot-fairy-be-{token}`）；
  - 新增 `ResourceQuotaWrapper.stop_unit(unit_name: str, proc: Process)`：在超时、15 分钟空闲休眠（Scale-to-Zero）或卸载时，优先执行 `systemctl kill --kill-who=all {unit_name}.scope` 配合 `os.killpg` 兜底，确保沙盒内任何 `setsid` 子进程均被内核级原子清理。
- **单任务隔离的附件硬链接目录（`inbox/<job_id>/`）**：
  - `stage_authorized_files(destination_chat_id, job_id, file_codes)` 将通过 `authorized_file_rows` 校验的原件优先通过 `os.link` 硬链接（跨文件系统回退 `shutil.copy2`）至 `<chat_ws>/inbox/<job_id>/`，并在 `run_cli_job` 的 `finally` 块中自动清理该 `<job_id>` 暂存子目录，杜绝跨任务文件残留。

### 3. 单级泛域名网关（`WorkspaceGatewayServer`）、Scale-to-Zero 与 Google Drive 归档
- **长连接复用与流式反代（`gateway_server.py`）**：
  - 在 `gateway.start()` 初始化单一共享 `self._client_session = aiohttp.ClientSession()`，在 `stop()` 中优雅关闭，废除每个 HTTP/WebSocket 请求新建 `ClientSession` 的连接池泄漏模式；
  - 反代请求对非 HTML 内容使用 chunk 流式传输，避免将 50MB 请求体一次性读入主脑内存。
- **标准路径防穿越与层级归档（`gateway_server.py` & `archive_manager.py`）**：
  - 静态资源与归档校验全部改用 `target.is_relative_to(root_dir)` 替代易被同名前缀目录绕过的 `str.startswith`；
  - `WorkspaceArchiveManager` 使用 `src_file.relative_to(files_dir)` 保留子目录树写入 `/mnt/gdrive/companion_vault/workspace_archives/<chat>/<date>_<slug>/`，防止不同子目录同名文件静默覆盖。

### 4. 小圈子友好的无感网页身份体系（好分享、不串号、不泄露备注）
- **自举随机密钥与 Cookie 过期强校验（`IdentityVerifier`）**：
  - 若 `cookie_secret` 为空或未配置，首次启动自动生成 32 字节随机 hex 写入 `<workspaces_root>/.cookie_secret`（`0600`）；
  - `verify_cookie` 实装服务端 `ts` 有效期检查（默认 90 天有效，常用手机/电脑绑定一次长期免登）。
- **彻底修复“私聊链接转发即送出 OWNER 身份”漏洞（`/_maibot/auth/whoami`）**：
  - 移除“只要路由来自私聊且访客无 Cookie，就无条件向访客浏览器签发创建者 `maibot_wxid` 全域 Cookie”的危险逻辑；
  - 私聊生成的网页对无 Cookie 访客默认提供**免登只读浏览**（方便把报告/页面随手转发给朋友查阅）；仅在需要提交交互回写（`/_maibot/notify`）时引导首次设备通过 4 位 PIN、口令或单次 Handoff 票据完成绑定。
- **按当前群过滤成员名单 + DOM 安全渲染**：
  - `whoami` 返回的候选成员列表严格限定在当前 `route.chat_id` 的 `group_members` 范围内，且**仅返回公开微信昵称 `person_name`（绝不返回号主单向私密备注 `wechat_remark`）**；私聊页面返回空成员列表；
  - `fulfill_claim_by_wechat` 校验群成员归属并用循环生成避免 4 位口令碰撞覆盖；`verify_user_pin` 增加轻量内存防呆（连续错误 5 次冷却 5 分钟）；
  - `MAIBOT_SDK_JS` 与 `editorial_kit.js` 移除拼接用户昵称/标题的 `innerHTML`，统一改用 `document.createElement` + `.textContent` 挂载。

### 5. 网页记忆回写隔离（`UserScopedTruthWriter`）与全双工实时总线（`GroupRealtimeHub`）
- **群待办按会话隔离（不跨群串台）**：
  - 在 `truth_db.py` 的 `quests_events` 表正规增加 `scope_chat_id TEXT DEFAULT ''` 列；
  - 群聊网页提交的待办写入 `scope_chat_id = origin_chat_id`；`truth_db.py` 构造群聊 `<全局与实时状态>` 时仅查询 `(visibility = 'PUBLIC' AND (scope_chat_id = '' OR scope_chat_id = ?))`，确保 A 群投票/约定绝不注入 B 群提示词。
- **标准账本监听与广播限长**：
  - `GroupRealtimeHub` 移除每次轮询跑 `PRAGMA table_info(messages)` 猜列名的测试补丁，固定使用 `MessageLedger` 真实列（补齐 `source=? AND account=?` 并过滤 `(kind & 65535) == 10000` 撤回行）；WebSocket 单次 `broadcast` 限制包体 `<= 64KB`。

### 6. 外部 Git 时光机并发锁与宿主 `@Command` / Hook 原生契约
- **共用项目互斥锁与操作者校验**：
  - `CLIBridge` 暴露 `project_lock(chat_id, slug)` 异步锁，`run_cli_job` 与 `_perform_project_rollback` 严格互斥；`creator_only` 项目的 `/rollback` 与 `/revoke` 仅允许 `creator_wxid` 本人或 `OWNER` 执行。
- **修复 `@Command` 为命名捕获组并清理双轨 Hook 胶水**：
  - 全部 `@Command` 正则改为宿主 `m.groupdict()` 原生要求的命名捕获组（`(?P<slug>...)`、`(?P<code>...)`、`(?P<pin>...)`、`(?P<token>...)`），发送者昵称从宿主真实传入的 `kwargs["message"]["message_info"]["user_info"]` 提取；
  - 删除 `plugin.py` 自定义 `@ctx.setter`、`gateway_server.py` 的 `except TypeError` 回调兼容分支、`subdomain_router.py` 的 `d.pop("first_visit_auto_claim")` 以及 `protect_subdomain_url_hook` 的旧版原地改字典死分支。

### 7. 底层数据归位、多模态音频内联直通与微信撤回队列同步
- **群聊自动画像脱敏加固（`src/maisaka/memory/person_profile.py`）**：
  - 传入 `is_group_chat`：当处于群聊会话时，自动人物画像注入仅保留公开角色与称呼，**硬性屏蔽 `person_info.impression`**（保护私聊沉淀的金主专属关系、亲密偏好与对朋友的私人评价绝不外泄至群聊），并将 Python 全表扫描改为 SQL `json_extract` 查询。
- **多模态内联直通开关（合并 `model_assets.py` & `quote_recall.py`）**：
  - 提炼统一的 `encode_media_part()` 辅助模块，支持环境变量 `MAIBOT_FILES_API_ENABLED`（对接 `api.0-0.pro` 等第三方中转网关时设为 `0`），使 `<= 15MB` 语音与原件直接零延迟走 `audio_base64` / `Part.from_bytes` 内联通道，消除每次先请求 `/upload/v1beta/files` 撞 404 的无效往返。
- **微信撤回消息级联清理内存缓冲（`ledger_source.py` $\rightarrow$ `runtime.py`）**：
  - 当账本消费到微信撤回修订（`(kind & 65535) == 10000`）时，通知对应会话 Runtime 将该 `native_message_id` 从防抖合批队列与内存 `_chat_history` 中即时剔除，消除“7 秒内撤回仍被 Planner 看见”的幽灵推理。
- **去除实例硬编码与跨平台测试修复**：
  - 剥离 `truth_db.py` / `projection.py` 中写死的 `"@ì _eun"`、`"wxid_w5d0efdxczt322"`、`"许妍"` 兜底与 `message_capture.js` 的 `msgId >= 143000` 阈值；
  - 在 `pyproject.toml` 与 `requirements.txt` 补齐 `tzdata` 依赖（确保 Windows 与 Linux/WSL 下 `pytest` 均可直接运行），归档移除已废弃的 `src/mobile_agent/` 与 `src/integration_tools/` 旧 UI 探针目录。

---

## 三、 面向「Gemini 双子 + Fairy 世界观 + 全局 ST 预设引擎」的底层预留接口契约

> **原则声明**：以下设计**不在本次净室重构中写死未敲定的业务细节（如姐姐姓名/外貌、具体预设条目文本）**，仅在数据库 Schema、模型任务路由、提示词编排层与配置中预留正交扩展槽位，确保后续无缝接入双子切换、离场状态弧线与 SillyTavern（ST）预设管理引擎。

1. **值班角色槽位与经费驱动的「离场/回归弧光状态机」预留**：
   - **中性槽位枚举（`persona_slot`）**：统一使用 `"sister_younger"`（默认，当前映射妹妹许妍 / Flash）与 `"sister_elder"`（预留，未来映射东大架构师姐姐 / Argon），严禁在数据库列名或状态枚举中硬编码具体人名。
   - **动态 Planner 模型路由与离场弧线（`elder_away_arc`）**：
     - 正常充裕/标准态（`>= 3.0 USD`）：私聊及被 `@` 时由满血 **Argon** 担任全局导演 Planner，`reply` 工具支持按 `persona_slot` 路由至 Flash（妹）或 Argon（姐）声带；
     - 跌破省流阈值（`< 3.0 USD`）：预留状态切换钩子生成一条**可演进的姐姐离场叙事状态**写入 `global_state.elder_away_arc`（如本乡研究室闭关跑实验），此后**全链路物理切至 Flash 独立接管 Planner + Replyer**（含 Owner 私聊与 `@`），并允许妹妹在日常交互中自然推进该场外状态线；
     - 经费充值恢复（回到 `>= 3.0 USD`）：触发回归钩子，Argon 重新接管 Planner 并读取演化后的 `elder_away_arc` 结合上下文无缝回归前台。
   - **子域路由署名（`subdomain_routes.issuer_name`）**：创建工作区链接时记录当前发言角色的显示名，网关过期页与弹窗动态显示“让 `{issuer_name}` 重新生成”，兼容未来姐妹任一人派发任务。

2. **全局 SillyTavern（ST）预设注入管理引擎与可切换直出模式预留**：
   - **反软审查与高活人感基座全局常驻（Global ST Preset Mounting）**：
     - 熟人群聊与私聊**全局统一挂载**修改版 ST 预设基座（含去 AI 客服腔、反道德说教/软审查虚构契约头、高质量 RP CoT 潜台词心理推演、以及 `@D` 深度插槽机制），确保群友日常开玩笑、擦边调侃或“开车”接梗时绝对不触发模型生硬说教，全场景保持鲜活细腻的真人质感。
     - **机制与隐私解耦**：去说教基座与细腻 RP CoT 全局共享；而**事实与秘密层**（精确美元账单、金主身份、仅对特定亲密关系开放的个人特殊偏好细节）仍严格受会话 RBAC 与 `person_info` 亲疏关系约束，做到**“群里接梗自然不端着、但私密关系秘密绝不往外漏”**。
   - **预留 Planner / Replyer 双端 ST 编排接管口**：
     - 在 Planner 提示词构建入口（`MaisakaChatLoopService._build_chat_system_prompt` / `maisaka.planner.before_request`）与 Replyer 消息组装入口（`BaseMaisakaReplyGenerator._build_request_messages`）保持干净的列表化 `ContextItem` 插槽，后续可零侵入替换为修改版 ST 预设引擎（消灭原生 Replyer 末尾生硬堆叠的多条 User 指令块）。
   - **预留 Owner 可切换的「一步直出模式（One-Pass ST Mode）」开关**：
     - 在会话运行态配置中预留 `chat_execution_mode: "dual_director" | "one_pass_st"`（支持 Owner 通过指令或配置随时切换）：默认 `dual_director` 走 Planner 导演 + Replyer 演员双层；切换为 `one_pass_st` 时，单次模型调用内一气呵成跑完 ST RP CoT 并直出最终回复，消除跨模型传话的情绪张力折损。

3. **真值库单一 Schema 预留（统一收敛于 `src/companion_vault/truth_db.py`）**：
   - `global_state`（`row_id = 1`）：正式纳入 `timezone_name`（默认 `'Asia/Tokyo'`）、`active_persona_slot`（默认 `'sister_younger'`）、`elder_away_arc`（默认 `''`，承载省流态姐姐离场演进状态线）、`chat_execution_mode`（默认 `'dual_director'`）、`fairy_remote_balance_usd`、`fairy_unsettled_cost_usd`、`fairy_total_spent_usd`（无上限累计研发总投入）、`fairy_compute_tier`（`SUPERCHARGED` | `STANDARD` | `THRIFTY` | `HIBERNATING`）与 `balance_updated_at`；
   - `phone_surfing_log`：预留 `persona_slot TEXT DEFAULT 'sister_younger'`，用于后续区分妹妹刷小红书与姐姐刷 X/知乎的待分享冲浪库存。

4. **双轨制算力账本与 Owner 专属记忆/设定隔离**：
   - **独立 `ComputeLedger` 服务预留**：将原插件内的 `ComputeEconomyManager` 抽离为宿主与插件共用的干净服务（时区对齐 `Asia/Tokyo` 叙事时钟）：预留 `on_token_usage(model_name, prompt_tokens, completion_tokens)` 本地瞬时折算钩子与 60~120s 远端 `/v1/user/balance` 权威对账及 `on_recharge_detected` 充值唤醒回调。
   - **Owner 专属事实层隔离（`OwnerPersonaOverlay`）**：在 `runtime_bridge.py` 中，仅当 `not is_group_chat and sender_trust_role == 'OWNER'` 时挂载精确算力账单、`person_info` 金主专属关系印象及预留的 `owner_overlay` 补充设定块，与普通私聊好友和群聊保持物理隔离。

---

## 四、 分批次重构执行与验收计划（3 Batches）

| 批次 | 聚焦范围 | 核心交付物与验收门禁 | 状态 |
| :---: | :--- | :--- | :---: |
| **Batch 1** | **`plugins/embodied_workspace/` 净室重构** | 完成功能点 1~4、6：清除明文 Key、实装 `bwrap --clearenv` + `TZ=Asia/Tokyo`、命名 cgroup scope 进程树回收、`inbox/<job_id>` 硬链接暂存、私聊只读分享防串号、本群公开昵称过滤与 DOM `.textContent` 安全化、`issuer_name` 动态署名、删除 `fast_renderer.py` 等死代码、对齐 `@Command` 命名捕获组与 Hook/Capability 原生协议；重写真实无假分支的 Phase 1~4 单测套件全绿通过。 | ✅ 已验收 |
| **Batch 2** | **铁桶级记忆隔离、`companion_vault` Schema 归位与双子预留** | 完成第五章**全部 6 条跨聊记忆泄漏暗道物理封堵**（`person_info` 公私印象物理拆列、群聊 `person_profile` / `query_person_profile` 脱敏、`consolidator` 私聊产物强制 `PRIVATE` 锁、掐断 Owner 私聊向 `A_memorix` 与全局 `ExpressionLearner` 的旁路采样、群聊出口敏感词熔断）；在 `truth_db.py` 统一正规化 `global_state` / `quests_events(scope_chat_id)` / `phone_surfing_log(persona_slot)`（彻底废除插件运行期 `ALTER TABLE`）；提纯双轨算力账本服务接口；合并多模态音频内联直通编码器。 | ✅ 已验收 |
| **Batch 3** | **微信撤回内存同步、去硬编码与遗留包袱清理** | 完成功能点 7（后半）：打通微信撤回对内存 `_chat_history` 防抖队列的级联剔除；剥离残留的真实群友昵称/微信号/`msgId>=143000` 硬编码；补齐 `tzdata` 依赖使 Windows 与 WSL 本地验证全绿；归档移除 `src/mobile_agent/` 与 `src/integration_tools/` 废弃代码。 | ✅ 已验收 |

---

## 五、 铁桶级记忆隔离规范：6 条跨聊泄漏暗道尸检与四道物理防线

> **架构师最高红线警告**：一旦全局常驻挂载“无软审查、去道德说教”的修改版 ST 预设，大模型将不再替系统做保守的话题刹车——**只要某个私密字节进入了群聊 Prompt，模型就极可能在群友起哄或调侃时顺嘴说出！** 因此记忆隔离严禁依赖 Prompt 叮嘱（“请勿在群里透露”），**必须 100% 在 Python 存储与读取层实现物理字节级阻断**。

### 1. 现网实锤的 6 条私密记忆跨聊泄漏暗道（Batch 2 必须逐一焊死）

| 暗道编号 | 源码精确位置 | 泄漏机理与严重后果 |
| :---: | :--- | :--- |
| **LEAK-01** | `src/maisaka/memory/person_profile.py` (`L303-362`) | **群聊每轮自动注入裸读私聊 `impression`**：当 OWNER 在群里发言或被群友 `@` 时，`build_person_profile_injection_messages` 未判断 `is_group_session`，直接将私聊整合进 `person_info.impression` 的金主关系、亲密调情与特殊偏好原样拼入当轮群聊 Planner 上下文。 |
| **LEAK-02** | `src/maisaka/builtin_tool/query_person_profile.py` (`L103-241`) | **内置工具主动查询无视群私边界**：群聊 Planner 可通过 `tool_search` 唤醒 `query_person_profile` 查询任意人物，该工具直接返回完整的 `person_info.impression` 至群聊历史。 |
| **LEAK-03** | `src/companion_vault/consolidator.py` (`L579-625`)<br/>`src/companion_vault/truth_db.py` (`L55-59`) | **`person_info` 公私混写同一列 & 文档宣传的内存快照根本不存在**：现网代码中并不存在文档声称的 `_build_scoped_consolidation_db`；`person_info` 仅有单一 `impression` 列，私聊整合写入的绝密印象与群聊整合写入的公开印象共用同一字段互相覆盖混染。 |
| **LEAK-04** | `src/companion_vault/consolidator.py` (`L615-622`)<br/>`src/companion_vault/truth_db.py` (`L396-399`) | **私聊巩固未强制覆写 `quests_events` / `user_status` 的 `visibility`**：`consolidator.py` 仅校验了 `chronicle_events` 必须为 `PRIVATE`，若巩固模型将私聊产生的某条私密约定写入 `quests_events` 并误标为 `visibility='PUBLIC'`，下一轮所有群聊都会在常驻 `<全局与实时状态>` 中看到它！ |
| **LEAK-05** | `src/main.py` (`L204`)<br/>`src/chat/heart_flow/heartflow_message_processor.py` (`L65`)<br/>`src/services/send_service.py` (`L784`) | **宿主原生 `A_memorix` 记忆写回暗门仍在双轨运行**：每条收发消息仍在触发 `memory_automation_service`（`fact_writeback` 与 `chat_summary_writeback`），私聊事实被旁路抽入宿主 `A_memorix` 库并可通过 `person_profile.py:365` 回退分支漏出。 |
| **LEAK-06** | `src/maisaka/runtime.py` (`L195-197`)<br/>`src/chat/replyer/maisaka_expression_selector.py` (`L87-108`) | **口癖与情景学习器（`ExpressionLearner` / `Jargon`）跨会话传染**：若 `expression_groups` 含全局通配符 `("*", "*")`，私聊亲密互动中学到的暧昧 `{situation, style}` 句式会被选入群聊 Replyer 提示词。 |

### 2. 四道物理级封堵防线实现规范（Zero-Leak Architecture）

1. **防线一：`person_info` 物理分列 + SQL 投影级硬裁剪**
   - 在 `truth_db.py` 将人物印象拆分为物理隔离的两列：
     - `public_impression TEXT DEFAULT ''`：仅记录群聊公开互动、学术/音乐共同兴趣、普通社交印象；
     - `owner_private_notes TEXT DEFAULT ''`（兼容迁移旧 `impression` 中 OWNER 的私密内容）：**物理级绝密列**，仅存放天使投资人关系、亲密恋人互动进展与个人特殊审美偏好。
   - **写入硬锁**：群聊 `MemoryConsolidator` 执行时，SQLite Authorizer / 提交前校验**物理拒绝任何针对 `owner_private_notes` 的读写与修改**。
   - **读取硬锁**：在 `truth_db.py`、`person_profile.py`、`query_person_profile.py` 及 `user_truth_writer.py` 中，凡 `is_group_chat == True` 或 `sender_trust_role != 'OWNER'`，SQL `SELECT` 列表**物理剔除 `owner_private_notes` 列**。
2. **防线二：巩固提交期强制可见性降级（不信任模型填写的 `visibility`）**
   - 在 `consolidator.py` 的 `commit()` 阶段：
     - 当 `not is_group_scope`（任何私聊整合）时，对本批新增或更新的所有 `user_status`、`quests_events`、`chronicle_events` 行，由 Python 代码在事务内**强制执行 `SET visibility = 'PRIVATE', scope_chat_id = self.chat`**，彻底剥夺巩固模型将私聊事实标为 `PUBLIC` 的能力；
     - 实装真正的受限可见集校验：群聊整合仅允许更新 `public_impression` 与绑定 `scope_chat_id = self.chat` 的本群 `quests_events`。
3. **防线三：切断宿主 `A_memorix` 与跨聊 `ExpressionLearner` 对私密域的旁路采样**
   - 在微信具身驱动模式下，对 `platform == "wechat"` 的消息旁路禁用宿主冗余的 `memory_automation_service`（`fact_writeback` / `chat_summary_writeback`），保持 `shujuku`（`companion_truth.db`）作为唯一权威记忆源，消除双记忆库脑裂；
   - 在 `ExpressionLearner`、`JargonLearner` 与 `maisaka_expression_selector.py` 中增加硬性守卫：**私聊会话（尤其是 OWNER 私聊）产生的表达习惯与黑话严禁进入跨会话共享池，群聊选择器严禁读取任何私聊 `session_id` 来源的表达条目**。
4. **防线四：群聊发包前出口敏感锚点熔断（Output Guardrail）**
   - 在群聊消息进入 `send_service` 前（或 `before_post_process`），配置极轻量的本地词表断言（仅包含可配置的硬机密特征词，如精确美元余额数字串、Owner 专属私密昵称/暗号）：一旦群聊待发文本意外命中，立即拦截不发往微信并记入告警日志。

---

## 六、 原生级 SillyTavern (ST) 预设注入管理引擎工程规格（`STPresetEngine`）

> **模块定位**：`src/companion_vault/st_preset_engine.py`（作为后续双子活人感升级的独立核心组件，当前重构阶段为其预留干净的挂载插槽）

### 1. 标准预设结构兼容层（JSON Schema）
直接兼容 SillyTavern Chat Completion Preset 与 World Info（Lorebook）核心结构，支持将本地酒馆可视化调好的 `.json` 预设放入 `config/presets/` 直接加载（如 `preset_sister_younger.json` / `preset_sister_elder.json`）：
- **`prompts[]`**：每个提示块支持 `identifier`、`role`（`system` | `user` | `assistant`）、`content`、`injection_position`（`0` 相对骨架位置 | `1` 聊天流内 `@D` 深度注入）、`injection_depth: int`、`enabled: bool`；
- **`prompt_order[]`**：定义激活块及上下游精确排布顺序；
- **`character_book / lorebook`**：支持 `constant` 常驻条目、`keys` 关键词/正则触发条目、`depth` 插入深度，并扩展字段 **`scope_guard: "all" | "owner_only" | "group_only"`**（在引擎编译期做物理级会话鉴权裁剪）；
- **`regex_scripts[]`**：支持 `PROMPT`（发往模型前清洗）与 `AI_OUTPUT`（模型生成后剥离残留标签、修正微信口语标点与分段）。

### 2. 五步纯函数上下文编译管线（`compile_st_context(...) -> list[ContextItem]`）
1. **Step 1：宏变量单次扫描求值（Macro Resolution）**
   - 原生解析 `{{char}}`（当前值班姐妹名）、`{{user}}`（私聊对方称呼 / 群聊当前主交互群友）、`{{tokyo_time}}`（东京文京区昼夜时段与星期）、`{{shujuku_state}}`（已按第五章四道防线物理脱敏后的真值目录与工况）、`{{shujuku_recalled}}`（本轮绿灯召回详情）、`{{director_note}}`（双层模式下 Planner 传入的 `reply_reference`）。
2. **Step 2：主骨架铺设与富多模态原位保留（Relative Skeleton）**
   - 按 `prompt_order` 展开静态基座块（虚构现实主义契约头、反说教/反客服基调）、角色设定槽（`charDescription` + 仅 `is_owner_private` 时拼接的 `owner_private_overlay`）及 `worldInfoBefore/After`；
   - 在 `chatHistory` 标记位**原位展开富模态 `SessionBackedMessage` 序列**（完整保留微信原生 `<message quote="...">` 标签、真值库高清原图 `ContextImagePart`、24kHz 无损语音 `ContextAudioPart` 与 PDF 文件句柄），兼具酒馆的文风编排与具身微信的全模态感知。
3. **Step 3：安全的轮次边界 `@D` 深度注入（Turn-Safe In-Chat Depth Injection）**
   - 收集所有 `injection_position == 1` 的块（如 `@D4` 往事闪回、`@D2` 导演耳语、`@D0` 社区预设精华 RP CoT 潜台词推演与微信短句微习惯锚点），倒数定位至第 `D` 个**完整对话轮次边界**插入；
   - **Agent 铁律保护**：计算倒数深度时**严禁切开 `FunctionCallItem` 与 `FunctionCallOutputItem` 配对组**，彻底避免 Gemini API 抛出 `400 Function call turn ordering` 错误。
4. **Step 4：Gemini KV Cache 前缀钉死优化（Cache-Friendly Tail Binding）**
   - 所有高频变动变量（秒级时间、本轮实时余额浮动、临时绿灯召回块）**严禁放入顶部 `System` 或 `Depth > 4` 区域**，统一锚定在尾部 `Depth 0 ~ 2`，确保连续对话中前 85%+ 的多模态与预设前缀字节 100% 稳定命中 Gemini Context Caching，大幅压降 Argon 调用成本。
5. **Step 5：双模式出口契约无冲突适配（避免 Assistant Prefill 破坏工具调用）**
   - 摒弃会破坏 Gemini 原生 Function Calling 触发率的末尾 `Assistant` 预填（Prefill），利用 Gemini 4 原生 Reasoning 配合 `@D0` 引导块驱动社区 RP CoT，按 `global_state.chat_execution_mode` 自动切换末尾出口：
     - **`dual_director`（双层导演模式，默认）**：Planner 完成 RP 心理推演后输出工具调用 `reply(persona_slot=..., reply_reference=...)` / `run_agent_workspace(...)` / `wait()`；Replyer 挂载演员侧 ST 预设直出微信口语气泡（彻底取代原生 `maisaka_generator_base.py` 末尾生硬的 6 连 User 说教块）。
     - **`one_pass_st`（Owner 可切换一步直出模式）**：单次模型请求内跑完完整 ST RP CoT 并直出 `<reply>...</reply>`，经 `regex_scripts` 后处理清洗后直接发送，实现零传话损耗的酒馆级沉浸私聊体验。

### 3. 主干零侵入挂载点（预留对接位）
- **Planner 侧**：`src/maisaka/chat_loop_service.py`（通过可插拔 `PromptComposer` 策略或 `maisaka.planner.before_request` 接管）；
- **Replyer 侧**：`src/chat/replyer/maisaka_generator_base.py`（在 `_build_request_messages` 中与现有 `_uses_retro_prompt()` 并列增加 `_uses_st_preset_engine()` 分支，支持一键热切与回退对比）。
