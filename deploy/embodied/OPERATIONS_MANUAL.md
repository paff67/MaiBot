# 生产环境运维与排障手册（Production Operations & Troubleshooting Manual）

> **最后更新**：2026-11-19
> **生产版本边界**：运行目录不是 Git 工作树。不能用远端 `main` 的提交推定线上版本；以发布清单、文件摘要和服务探针为准。
> **当前热修复**：`feat/phase6-twins-native-runtime` 上的 `ed77a1304`；本次仅发布该提交中的 9 个运行时文件，不含测试和前端产物。

---

## 2026-11-19 微信直答修复发布记录

DMIT-US 的 `maibot-main` 从 `/opt/maibot-brain` 运行。本次从发布包更新了 9 个 Python 文件，仅重启主服务。部署前校验旧文件及备份摘要，替换后逐一核对新文件 SHA-256。主进程进入 `active/running`，WebUI 本机探针 `http://127.0.0.1:8001/openapi.json` 返回 200，消息采集服务保持 active。

排查微信 `one_pass_st` 空回复时，先看本轮的 `generation_attempts`。`missing_required_tool_args` 表示工具调用缺少当次声明的必填键，`empty_st_visible_reply` 表示清洗后没有可发送正文。两者都在单次尝试记为成功前触发现有重试。顺序策略下，主渠道耗尽后才会选择备用。工具执行后的生成重试复用已有 observations。

离线回归覆盖了主渠道连续三次返回空参、备用生成成功且只发送一次的场景。生产发布验收覆盖文件与进程健康。后续遇到自然失败时，可按同一回合的尝试序列与发送回执核对实际切换结果。

回滚包在 DMIT-US 的 `/opt/maibot-backups/one-pass-st-20261119-LWys5b/before.tar`。回滚这次发布时，先检查归档内的九个路径并核对当前版本，从运行目录恢复该包，重启 `maibot-main`，再检查服务状态和上述 HTTP 探针。

## 一、 生产节点与网络访问矩阵

| 节点角色 | 主机标识 | 访问方式 / 端口 | 核心路径 |
| :--- | :--- | :--- | :--- |
| **云端主脑 (`DMIT-US`)** | `DMIT-US` | `ssh DMIT-US` | 代码与运行目录：`/opt/maibot-brain`<br/>环境配置文件：`/etc/maibot-media.env`<br/>mTLS 证书目录：`/etc/maibot-mtls/`<br/>Google Drive 挂载：`/mnt/gdrive/companion_vault` |
| **亚太中继 (`jp-main`)** | `jp-main` | `ssh jp-main` | mTLS 边缘中继：`https://100.90.156.92:8443` / `http://100.87.39.37:8765` |
| **手机边缘端 (`小米 10 / umi`)** | Android 13 / MIUI 14 (APatch Root) | 本机 ADB (`adb shell`) 或经 `DMIT-US` mTLS 访问 `https://100.90.156.92:8443` | 边缘服务目录：`/data/adb/maibot_edge`<br/>设备守护目录：`/data/adb/device_services`<br/>微信单写者日志：`/data/user/0/com.tencent.mm/files/maibot_message_journal.db` |

---

## 二、 云端（`DMIT-US`）生产服务矩阵与日常管理

### 1. 生产 `systemd` 服务列表（共 6 项常驻服务）

| 服务名 | 启动入口 | 职责说明 |
| :--- | :--- | :--- |
| `maibot-main.service` | `python3 -m src.main` | MaiBot 主程序（Maisaka 推理引擎 + `WeChatRootDriver` 收发驱动 + `MemoryConsolidator` 长期记忆巩固）。 |
| `maibot-message-ingest.service` | `python3 -m src.platform_io.wechat.message_ingestor` | 从手机 `/messages/events` 增量拉取提交态消息与撤回修订，事务性写入 `data/message_ledger.db`。 |
| `maibot-message-media.service` | `python3 -m src.companion_vault.message_media_worker` | 消费 `message_ledger.db` 中的媒体消息（图片/表情/语音/文档），从手机拉取原件与无损转码结果并写入 `data/companion_truth.db` 与 `data/companion_assets`。 |
| `maibot-message-archive.service` | `python3 -m src.companion_vault.message_archive` | 按账号与会话将 `message_ledger.db` 归档为结构化 JSON。 |
| `maibot-drive-sync.service` | `python3 -m src.companion_vault.media_ingestor --mode sync` | 将本地原件与归档通过 `rclone` 同步至 Google Drive 并校验远端 MD5/大小（`drive_status='verified'`）。 |
| `maibot-gdrive.service` | `rclone mount ... /mnt/gdrive/companion_vault` | 挂载 Google Drive 远端存储卷。 |

> **注**：早期基于 UI 屏幕快照与通知探针的旧服务（`maibot-wechat-live.service`、`maibot-chat-archive.service`、`maibot-notification-inbox.service`、`maibot-media-ingest.service`）均已正式停用并下线，**切勿重新开启**。

### 2. 常用服务状态检查与日志跟踪

```bash
# 一键检查 6 个核心生产服务状态
ssh DMIT-US 'systemctl is-active maibot-main maibot-message-ingest maibot-message-media maibot-message-archive maibot-drive-sync maibot-gdrive'

# 实时查看主程序（收发、Planner 思考、Replyer 回复、记忆巩固）日志
ssh DMIT-US 'journalctl -u maibot-main.service -f'

# 查看消息入账与媒体解析日志
ssh DMIT-US 'journalctl -u maibot-message-ingest.service -u maibot-message-media.service -n 50 --no-pager'

# 平滑重启主程序
ssh DMIT-US 'systemctl restart maibot-main.service'
```

---

## 三、 生产配置文件规范（`/opt/maibot-brain/config/`）

为确保账号下**全部现有及未来新增好友（私聊）与群聊默认可用**，以下 4 个配置文件必须保持协同一致：

### 1. `/opt/maibot-brain/config/wechat_driver.toml`
```toml
version = 1
truth_path = "/opt/maibot-brain/data/companion_truth.db"
ledger_path = "/opt/maibot-brain/data/message_ledger.db"

[[drivers]]
enabled = true
source = "https://100.90.156.92:8443"
account = "4d86ac1b06dd252fa3c7327eb5614865"
allowed_chats = ["*"]
send_enabled = true
use_hook_send = true
max_sends_per_minute = 6
max_sends_per_chat_per_minute = 3
rate_limit_wait_seconds = 65.0
poll_interval = 1.0
backfill = false
```

### 2. `/opt/maibot-brain/config/memory.toml`
```toml
version = 1
truth_path = "/opt/maibot-brain/data/companion_truth.db"
ledger_path = "/opt/maibot-brain/data/message_ledger.db"

[[scopes]]
enabled = true
source = "https://100.90.156.92:8443"
account = "4d86ac1b06dd252fa3c7327eb5614865"
chat_id = "*"
model_task = "utils"
batch_size = 20
poll_interval = 15.0
backfill = false
```

### 3. `/opt/maibot-brain/config/adapter_policy.toml`
必须保持 `default_action = "allow"`，否则 `HeartflowManager` 会在创建会话实例阶段拦截私聊或新群聊：
```toml
[defaults.group]
default_action = "allow"
allow_ids = []
block_ids = []

[defaults.private]
default_action = "allow"
allow_ids = []
block_ids = []

[[adapters]]
platform = "wechat"
account_id = "4d86ac1b06dd252fa3c7327eb5614865"

[adapters.group]
default_action = "allow"
allow_ids = []
block_ids = []

[adapters.private]
default_action = "allow"
allow_ids = []
block_ids = []
```

### 4. `/opt/maibot-brain/config/bot_config.toml`
- `[chat.reply_timing]` 下必须确保 **`private_reply_paused = false`**（若设为 `true`，所有私聊 `talk_value` 会变为 `0.0`，只静默接收不进入 Planner 回复）。

---

## 四、 手机边缘端（小米 10）守护体系与运维操作

### 1. 手机端进程层级
1. **`/data/adb/service.d/maibot_edge.sh`**（开机自启脚本）：
   - 拉起 Tailscale（`/data/adb/maibot_edge/tailscale/tailscaled --tun=userspace-networking`）；
   - 启动 `edge_server.py`（监听 `:8765`）、`message_daemon.py` 与 `edge_watchdog.py`（每 5 秒巡检保活并锁定 `oom_score_adj = -1000`）。
2. **`message_daemon.py` + `frida-inject`（`message_capture.js`）**：
   - 自动发现 `com.tencent.mm` PID，校验微信 `8.0.79` 指纹后注入 `message_capture.js`；
   - 状态标志文件：`/data/adb/maibot_edge/state/message_capture.enabled`（存在则启用，移除则自动卸载 Frida 注入器）；
   - 心跳日志：`/data/adb/maibot_edge/run/message-capture.log`。
3. **`/data/adb/device_services/`（常亮与校园网守护）**：
   - 控制命令：
     ```bash
     adb shell su -c "sh /data/adb/device_services/control.sh status"
     adb shell su -c "sh /data/adb/device_services/control.sh restart"
     ```

### 2. 手机端健康检查与进程重启注意事项
- **从云端检查手机端健康状态**：
  ```bash
  ssh DMIT-US 'curl -s --cacert /etc/maibot-mtls/ca.crt --cert /etc/maibot-mtls/client.crt --key /etc/maibot-mtls/client.key https://100.90.156.92:8443/health'
  ```
- **在手机端重启 `edge_server` 或 `message_daemon` 的重要注意事项**：
  > **切勿在 `adb shell su -c "..."` 中直接使用 `pkill -f edge_server.py`**：Android Toybox 的 `pkill -f` 会匹配 `su -c` 自身的命令行字符串并误杀当前 Shell（返回 Exit Code `143`）。
  > **规范做法**：使用 `ps -ef` 过滤出精确 PID 后执行 `kill -9 <pid>`，等待 `edge_watchdog.py` 在 5 秒内自动拉起新进程。

---

## 五、 数据库日常巡检与排障诊断命令

`DMIT-US` 未安装独立 `sqlite3` CLI 二进制，请统一使用 `/usr/bin/python3 -c "import sqlite3 ..."` 进行只读查询：

### 1. 检查最新入站/出站消息与驱动消费游标
```bash
ssh DMIT-US 'python3 - << "PY"
import sqlite3
conn = sqlite3.connect("file:/opt/maibot-brain/data/message_ledger.db?mode=ro", uri=True)
print("=== Latest 10 Messages ===")
for r in conn.execute("SELECT msg_id, chat_id, sender_id, direction, kind, substr(body, 1, 60) FROM messages ORDER BY CAST(msg_id AS INTEGER) DESC LIMIT 10"):
    print(r)
print("=== Driver Cursors ===")
for r in conn.execute("SELECT * FROM wechat_driver_cursors"):
    print(r)
print("=== Quarantined Messages ===")
for r in conn.execute("SELECT * FROM wechat_driver_quarantine ORDER BY updated_at DESC LIMIT 5"):
    print(r)
PY'
```

### 2. 检查通讯录备注、群成员、媒体队列与长期记忆巩固状态
```bash
ssh DMIT-US 'python3 - << "PY"
import sqlite3
conn = sqlite3.connect("file:/opt/maibot-brain/data/companion_truth.db?mode=ro", uri=True)
print("person_info count:", conn.execute("SELECT COUNT(*) FROM person_info").fetchone()[0])
print("group_members count:", conn.execute("SELECT COUNT(*) FROM group_members").fetchone()[0])
print("chronicle_events count:", conn.execute("SELECT COUNT(*) FROM chronicle_events").fetchone()[0])
print("media_jobs by state:", conn.execute("SELECT state, COUNT(*) FROM message_media_jobs GROUP BY state").fetchall())
print("memory_jobs by state:", conn.execute("SELECT state, COUNT(*) FROM memory_jobs GROUP BY state").fetchall())
print("memory_job_cursor:", conn.execute("SELECT * FROM memory_job_cursor").fetchall())
PY'
```

---

## 六、 代码发布与上线标准流程（Release SOP）

> **⚠️ 净室重构上线前必填配置核对（重要）**：
> 净室重构已彻底剥离源码中的个人域名、微信号与明文 API Key 硬编码默认值。首次部署重构版本至 `DMIT-US` 前，**必须确认以下两处生产配置已显式填写**：
> 1. **`/etc/maibot-media.env`**（参考 `deploy/embodied/media.env.example`）：
>    - `MAIBOT_SELF_WXID=wxid_w5d0efdxczt322`（自身底层微信号，供 `<自身账号与提及判定准则>` 与 `@` 解析使用）
>    - `FAIRY_SANDBOX_API_KEY=<受限网关密钥>` 与 `FAIRY_SANDBOX_BASE_URL=https://api.0-0.pro`
>    - `MAIBOT_FILES_API_ENABLED=0`（启用第三方网关 `<=15MB` 语音/附件零延迟内联直通，免除 Files API 404 往返）
>    - `MAIBOT_DISABLE_AMEMORIX=1`（确保宿主 `A_memorix` 完全旁路，仅以 `shujuku` 真值库为单一记忆源）
> 2. **`embodied_workspace` 插件配置（`config.toml`）**：
>    - 显式设置 `base_domain = "paff-67.top"`、`workspaces_root = "/opt/maibot-brain/data/workspaces"`、`gdrive_archive_root = "/mnt/gdrive/companion_vault/workspace_archives"`、`wechat_source`、`wechat_account` 及 `cli_timeout_seconds = 10800`。

1. **本地全量回归与静态门禁验证（142 项）**：
   ```bash
   cd /mnt/e/Gemini/MaiBot
   pytest pytests/companion pytests/platform_io/test_wechat_at_parsing.py pytests/platform_io/test_adapter_policy.py pytests/maisaka/test_reasoning_engine.py -q
   ruff check plugins/embodied_workspace src/companion_vault src/platform_io/wechat
   ```
2. **同步代码至云端 `DMIT-US`**：
   > **规范提醒**：生产环境优先通过 Git 分支拉取（`git pull --ff-only`）保持提交可追溯；仅在单文件紧急验证时使用 `scp` 同步并务必回推 Git 分支。
   ```bash
   ssh DMIT-US 'cd /opt/maibot-brain && git fetch origin && git checkout main && git pull --ff-only origin main'
   ```
3. **如涉及手机边缘端（`src/phone_edge/`）变更，同步至小米 10**：
   ```bash
   adb push /mnt/e/Gemini/MaiBot/src/phone_edge/*.py /mnt/e/Gemini/MaiBot/src/phone_edge/message_capture.js /sdcard/Download/
   adb shell su -c "cp /sdcard/Download/*.py /sdcard/Download/message_capture.js /data/adb/maibot_edge/ && rm -f /sdcard/Download/*.py /sdcard/Download/message_capture.js"
   ```
4. **重启云端服务并核验网关与核心守护进程状态**：
   ```bash
   ssh DMIT-US 'systemctl restart maibot-main maibot-message-ingest maibot-message-media && systemctl is-active maibot-main maibot-message-ingest maibot-message-media && ss -tlnp | grep 18790'
   ```

---

## 七、 常见生产排障 SOP 与典型故障根因分析

### 1. 故障案例：引用回复工具（`reply`）耗时 30s+ 且前端报【执行失败】
- **现象特征**：
  WebUI / 监控中显示 Planner 成功思考并决定调用 `reply` 工具，但工具执行卡住约 35 秒，最终显示 `【执行失败】 35.10s`，群聊中未能发送出任何消息。
- **根本机理拆解**：
  1. **引用保护逻辑升级**：
     即使 Planner 传入 `set_quote: false`，若 Planner 思考耗时较长（例如 15~20s），在此期间群内若有其他成员发言（新消息插入），系统触发 `target_has_moved_on` 保护逻辑，将 `effective_set_quote` 强制升级为 `True`，尝试以微信原生 AppMsg 57 引用回复方式发包。
  2. **手机端 Frida Hook 崩溃**：
     微信底层方法为 `k0.I(appMsg, appId, appName, toUser, mediaTagName, data)`。若误将 `chatId` 传给 `appId`（参数 2）而 `toUser`（参数 4）传空，或 XML 中 `<fromusername>` 未传真实 `wxid`：
     - 微信异步协程 `ru.d` 会在准备阶段报错 `prepare retCode: -520012`；
     - 接着在入库更新阶段报错 `updateMsg retCode: -520009`（`UpdateMsgError`）；
     - 消息彻底无法写入 `EnMicroMsg.db` 的 `message` 表。
  3. **超时级联中断**：
     手机端 Frida 轮询 15 秒未等到本地入库，将任务标记为 `status='submitted_unconfirmed'` 并返回 `DeliveryStatus.PENDING`。云端 `SendService` 将其视作未送达，从而使工具调用在 35 秒时报错中断。
- **排障诊断与修复 SOP**：
  1. 检查手机端 XLog：使用 Frida 挂钩 `com.tencent.mars.xlog.Log`，观察是否有 `send_app_msg_quote` 相关的 `-520012` 或 `-520009` 日志；
  2. 校验 `message_capture.js`：确保调用签名为 `k0.I(appMsg, "", "", chatId, "", null)`，且 `<fromusername>` 从 `userinfo WHERE id=2` 动态获取；
  3. 确认修复后重载：重启 `frida-inject` 守护进程，并在群聊中重试，确认微信返回 `send_result_type=Success`（耗时仅约 600ms）。

### 2. 故障案例：群聊中机器人“认错人”或将艾特群友误判为艾特自己
- **现象特征**：
  群聊中某用户艾特另外一位群友（如 `@R`），机器人却插话回答或在思考中说“群友艾特了我，但我不知道为什么艾特我”。
- **根本机理拆解**：
  1. **账号昵称不对称**：实体机登录的微信号全局昵称（如 `paff`）若与群内真人重名，且该群未设置“我在本群的昵称”（群名片）；
  2. **Prompt 缺乏强排他映射**：Prompt 中如果写死了旧的人设名字或缺少底层 `wxid` 排他声明，大语言模型在注意力发散时，容易将上下文中出现的同名群友发言误以为是指向自己。
- **排障诊断与修复 SOP**：
  1. **检查群名片**：在手机微信中进入群聊天信息，查看“我在本群的昵称”。若为空，建议设置并与 `bot_config.toml` 中的 `nickname` 保持完全一致；
  2. **检查真值库索引**：在 `DMIT-US` 查询 `companion_truth.db` 的 `person_info` 表，核实群内所有成员的 `person_name`、`wechat_remark` 与 `platform_ids`（wxid）映射关系是否健全；
  3. **人设热更与别名同步**：系统已实装动态别名机制，在修改 `bot_config.toml` 中的 `nickname` 或 `alias_names` 后，主程序毫秒级热更生效，Prompt 自动注入带有 `<重要角色索引>` 与强排他准则的身份上下文，彻底消除误判。

### 3. 故障案例：群聊语音附件上传报 `404 Not Found`（Files API 不可用）
- **现象特征**：
  群友发送超过 10 秒的语音（24kHz PCM WAV 体积超过 `512KB`），日志报错上游 `/upload/v1beta/files` 返回 `404 Not Found`，导致模型未能听到语音内容。
- **根本机理与已实装容错**：
  第三方 OpenAI/Gemini 聚合中转网关通常未实现 Google 原生 `Files API` 上传端点。系统已在 `model_assets.py` 与 `quote_recall.py` 实装自动降级：当 Files API 上传抛出异常或未配置客户端、且文件 `<= 15MB` 时，自动回退为 `ContextAudioPart`（`audio_base64` 内联载荷）直传模型。
- **巡检核验命令**：
  ```bash
  ssh DMIT-US 'journalctl -u maibot-main -n 100 --no-pager | grep -E "audio_base64|Files API|hydrate"'
  ```

### 4. 故障案例：慢轨工作区子域名无法访问、超时或沙盒进程排查
- **巡检网关监听与活跃沙盒单元**：
  ```bash
  # 检查本地工作区网关 (:18790) 与 Nginx SNI 反代 (:4443)
  ssh DMIT-US 'ss -tlnp | grep -E "18790|4443"'

  # 查看当前运行中的确定性命名 cgroups v2 沙盒构建任务 (maibot-fairy-cli-*) 与后端进程 (maibot-fairy-be-*)
  ssh DMIT-US 'systemctl list-units "maibot-fairy-*.scope" --state=running --no-pager'

  # 查看具体会话工作区构建日志
  ssh DMIT-US 'tail -n 60 /opt/maibot-brain/data/workspaces/*/projects/*/build.log'
  ```

### 5. 故障案例：验证微信撤回消息的内存防抖实时剔除与群聊出口熔断
- **机制说明**：
  1. **防抖窗口撤回同步**：当用户在发送消息后数秒内于微信端撤回（`kind=268445456`），`LedgerSource` 会立即调用 `WeChatRootDriver.revoke_committed` $\rightarrow$ `MaisakaHeartFlowChatting.revoke_message`，从待合批缓冲与内存 `_chat_history` 中剔除该 `msg_id` 并取消空批定时器。
  2. **群聊敏感锚点出口熔断（`OutputGuardrail`）**：群聊发包前自动校验是否意外包含精确美元账单或 `MAIBOT_GROUP_SECRET_ANCHORS` 机密词表，命中即物理拦截。
- **日志巡检命令**：
  ```bash
  ssh DMIT-US 'journalctl -u maibot-main -n 100 --no-pager | grep -E "已同步剔除撤回消息|OutputGuardrail"'
  ```

---

## 八、Phase 6 双子运行时与原生 WebUI 运维指南

1. **ST 微信预设模板自举检查**：
   - 版本库内权威模板位于 `plugins/st_preset_engine/templates/Meowssiah-WeChat-IM.json`，运行期自举同步至 `data/presets/Meowssiah-WeChat-IM.json`。
   - 若更新了模板，请核对 `data/presets/Meowssiah-WeChat-IM.json` 已同步覆盖，且 `[43]` 历史回顾、`[20~26]` 风格组、`[47] <summary>` 与 `[48] <comment>` 仍为启用状态。

2. **启用本地构建的原生 WebUI 面板（`MAIBOT_USE_LOCAL_DASHBOARD=1`）**：
   - 修改前端代码后需在 `dashboard/` 目录执行 `npm run build` 生成 `dashboard/dist`；
   - 在环境变量（参考 `deploy/embodied/media.env.example`）中配置 `MAIBOT_USE_LOCAL_DASHBOARD=1`，`src/webui/app.py`（`_is_local_dashboard_enabled()`）即优先挂载本地 `dashboard/dist` 构建产物。

3. **`/config/bot` 双子槽位与 `TwinEconomyConfig` 配置校验**：
   - 打开 WebUI `/config/bot` ->「身份与人格」，在顶部双子槽位控制台绑定 `younger_slot_profile`（妹妹）与 `elder_slot_profile`（姐姐）并启用 `twin_mode_enabled`；
   - `owner_private_prompt` 仅当 `not is_group_chat` 且 `MAIBOT_TRUTH_DB_PATH` 对应 `sender_trust_role == "OWNER"` 时才会挂载生效；
   - 在模型配置（`model_config.toml`）中为 `planner` / `planner_flash` / `replyer` / `replyer_elder` 配置真实有效的提供商密钥（未配置 `planner_flash` / `replyer_elder` 时系统会自动回退到默认任务槽位，但多模型路由需要独立模型条目）。

4. **`/chat` 监控页 `st_telemetry` 排障**：
   - 在 `/chat`（`maisaka-monitor.tsx`）展开对应轮次的 `PlannerToolResultCard`，可查看当前值班姐妹徽章（支持 `sister_younger` 🌸 妹妹值班 / `sister_elder` 🌙 姐姐值班）、实际调用模型、`thinking` 折叠思维链、`📏 小总结`、`🐱 猫猫留言` 以及 `📊 MVU 状态演进`（`<Analysis>` 归因与变量 Diff）；
   - 这些遥测字段通过 `src/maisaka/monitor/events.py` 挂载在 `tool` 顶层 `st_telemetry` 键上，确保不会影响微信出站 `visible_text` 的纯净 `<game>` 气泡。

5. **Persona V2 卡片模板自举与 3 层共享 MVU 运维**：
   - 内置角色卡片模板位于 `plugins/st_preset_engine/templates/personas/*.json`，启动或访问 `GET /api/webui/persona-v2/cards` 时自动自举同步至 `data/personas/*.json` 并登记至 `config/bot_config.toml`；
   - 双子核心卡（`maibot_xuyue` / `maibot_xuyan` 及其全量副本 `*_qianling_full`）共用 `shared_namespace = "twin_xu"`：在 `companion_truth.db` 的 `mvu_variables` 表中，`apartment_scene`（`PUBLIC`）与 `love_locked`（`PRIVATE` / `twin_harmony_locked`）写入 `card_id = 'shared:twin_xu'`，支持通过 `/config/bot`「Persona V2 立体人设工作台」在线查看、修改与重置实时变量（注：系统只读变量 `balance_tier` 与 `phone_holder` 由算力账本与值班槽位实时映射，不可由模型或手动覆盖）。
