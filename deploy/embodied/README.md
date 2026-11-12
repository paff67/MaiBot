# 具身全模态微信 AI 伴侣系统（Embodied WeChat MaiBot）

本目录承载 MaiBot 具身微信全模态架构的生产部署配置模板、`systemd` 单元文件、技术开发规范与生产运维手册。

---

## 一、 核心架构特性

1. **零 UI 点击 / 零无障碍的纯后台收发管线**：
   - **入站采集**：通过 APatch Root 守护进程管理本地 `frida-inject`（[`src/phone_edge/message_capture.js`](../../src/phone_edge/message_capture.js)），在微信（`8.0.79 / 3200 / patch-aa2a4a1f`）WCDB 提交态钩子后由独立线程只读查询 `EnMicroMsg.db`（`message`、`ImgInfo2`、`voiceinfo`、`videoinfo2`、`appattach`、`rcontact`、`chatroom`），经手机端单写者 SQLite 日记账（`maibot_message_journal.db`）同步至云端账本 `message_ledger.db`。
   - **双轨出站发送**：
     - **普通文本**：通过原子文件 Spool 投递至微信进程内上层发送控制器 `ISendMsgMgr`（`rt5.i5.Jj`），无需目标聊天窗处于前台即可向任意好友（`wxid_*`）或群聊（`*@chatroom`）发送消息；
     - **原生引用回复**：通过逆向分析微信原生 AppMsg 57 协程链路，Hook 调用 `AppMsgLogic:k0.I(appMsg, "", "", chatId, "", null)`，由底层自动完成 `<refermsg>` 组装、NetScene 调度入库及服务端 `status=2` 回执确认（平均耗时 ~600ms），彻底杜绝引用退化为纯文本或发送卡死。
2. **全账号多会话默认直通（Wildcard `"*"` 模式）**：
   - 驱动层（[`WeChatRootDriver`](../../src/platform_io/wechat/driver.py)）、消费层（[`LedgerSource`](../../src/platform_io/wechat/ledger_source.py)）、宿主策略层（[`AdapterPolicyManager`](../../src/platform_io/adapter_policy.py)）与记忆巩固层（[`MemoryConsolidator`](../../src/companion_vault/consolidator.py)）全面支持通配符 `"*"`，默认放行账号下全部现有及未来新增好友与群聊，同时自动拦截微信系统伪账号（`weixin`、`notifymessage`、`gh_*`、`filehelper` 等）。
   - 遇到新群聊或未知发送者时，按需触发 `/wechat/group_members` 增量同步，自动将微信通讯录昵称、备注（`conRemark`）及群成员关系写入 `companion_truth.db`。
3. **原生全模态就地挂载与双通道容错召回**：
   - 语音（SILK $\rightarrow$ 24kHz 16-bit 无损 PCM WAV）、图片（JPEG/PNG/GIF 逐帧无损 PNG 及 `WxGF` 内嵌 HEVC 流剥离解码）、文档（PDF/TXT/Office 派生）经 SHA256/MD5 校验入库后，由 [`hydrate_wechat_history_media_async`](../../src/companion_vault/wechat_recall.py) 按消息原始时间序直接挂载到 Maisaka 历史上下文节点（`SessionBackedMessage.vault_descriptors`），Planner 与 Replyer 均直接观测原件。
   - 支持 **`ContextFileUriPart` + `ContextAudioPart`（`audio_base64` 内联载荷）双通道多模态传输**：中小型语音或上游代理不支持 Gemini Files API（返回 404）时，自动无损回退至内联音频载荷（兼容 Gemini `Part.from_bytes`、OpenAI Chat `input_audio` 与 Responses API）。
   - 49 类引用回复消息（`refermsg`）自动反查账本绑定被引用原文与原件，无需模型额外点亮绿灯。
4. **结构化真值库（`shujuku` 范式）与人设多态性解耦**：
   - 采用 8 张 SQLite 结构化真值表（`global_state`、`user_status`、`quests_events`、`chronicle_events`、`media_vault`、`app_sop_skills`、`person_info`、`phone_surfing_log`）加 `memory_provenance` 溯源表。
   - 严格实施“**私聊可读共有群、群聊绝不可读私聊**”的 RBAC 铁律：群聊记忆整合运行在当前会话受限内存快照（`_build_scoped_consolidation_db`）中，从根源阻断跨私聊 SQL 读取与注入。
   - **动态人设与 @提及排他体系**：动态对接 `bot_config.toml` 配置事实源，支持群聊 `<atuserlist>` XML 穿透解析、公开昵称与单向备注双轨保留，以及脱敏群名册注入。
5. **全能动态沙盒工作区与限时子域交付（`plugins/embodied_workspace`）**：
   - 统一收敛至异步慢轨工具 `run_agent_workspace`（支持 `document` 杂志级长文、`webapp` 交互式单页、`backend` Scale-to-Zero 临时后端），最长达 3 小时（10800s）执行窗口并由快轨模型按人设周期播报客观进展。
   - 采用 Bubblewrap (`bwrap`) 只读根命名空间 + `systemd-run --scope` cgroups v2 资源硬配额 + 宿主侧外部 Git 时光机快照回滚 + 单级泛域名网关（`:18790`）与四级免登身份接力 + 全双工 WebSocket 实时协作总线。

---

## 二、 文档与配置文件索引

| 文件 | 说明 |
| :--- | :--- |
| [`TECHNICAL_DEVELOPMENT.md`](./TECHNICAL_DEVELOPMENT.md) | **技术开发、架构规范与代码审计治理文档**：模块分层、数据流、多模态双通道时序、沙盒工作区架构、RBAC 边界及**核心技术债重构清单**。 |
| [`OPERATIONS_MANUAL.md`](./OPERATIONS_MANUAL.md) | **生产环境运维与排障手册**：三节点网络拓扑、`systemd` 服务矩阵、网关与沙盒巡检、日常巡检 SQL、代码发布与典型故障根因 SOP。 |
| [`../../docs/EMBODIED_WORKSPACE_ITERATION.md`](../../docs/EMBODIED_WORKSPACE_ITERATION.md) | **具身工作区迭代档案与 v2.1 架构审计报告**：Phase 1~4 设计演进、踩坑记录与全栈代码质量审查结论。 |
| [`../../docs/CLEANROOM_REFACTOR_SPEC.md`](../../docs/CLEANROOM_REFACTOR_SPEC.md) | **Phase 1~4 净室重构规范与双子世界观预留蓝图**：私享场景边界取舍、7 大功能点净室重构细则与双子/Fairy 扩展接口预留规范。 |
| [`wechat_driver.example.toml`](./wechat_driver.example.toml) | `WeChatRootDriver` 生产配置模板（支持 `allowed_chats = ["*"]` 与全局/单会话双层限流）。 |
| [`memory.example.toml`](./memory.example.toml) | `MemoryConsolidator` 长期记忆巩固配置模板（支持 `chat_id = "*"` 动态发现新会话）。 |
| [`media.env.example`](./media.env.example) | 生产服务环境变量模板（`/etc/maibot-media.env`：mTLS 证书、边缘节点地址、Google Drive 与 Gemini Files API 配置）。 |
| `maibot-*.service` | 云端（`DMIT-US`）6 个核心生产 `systemd` 服务单元模板。 |
