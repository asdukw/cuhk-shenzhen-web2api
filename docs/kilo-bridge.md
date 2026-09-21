# Kilo 校园 GLM 实验适配层

这是独立的 `kilo_bridge` 模块，不修改 v2 API、Kilo 配置或旧数据库。默认仅绑定 `127.0.0.1:8768`；模型固定 `glm-5.3`；并发 1、起始间隔 20 秒，队列最多 16 项。使用项目 `.env.v2` 的 API 密钥，只读指定的认证目录；实验数据保存在 `.web2api-v2/kilo-bridge/`。

## 风险及已验证边界

校园已有工具（例如搜索）可能在 tool_proxy=false 时仍执行。适配层发现这类事件会中止并标为 unknown，但不能撤销已经发生的上游搜索，也不能保证它在第一次事件到达前尚未执行。因此启动必须显式确认，初期只用人工测试文件，不能提交密码、密钥、未公开代码等敏感内容。

之前用户的探针仅证明 GLM 可完成一次文本格式的模拟工具往返；没有证明 Kilo 已能可靠编辑真实项目。本适配层提供的离线 HTTP/SSE 测试也不等同于 Kilo 插件端到端验收。

## 如何工作

1. 接收 Chat Completions 的 messages/tools；保留 system/developer/user/assistant/tool 的角色及调用 ID，校验工具结果必须对应未完成的历史调用。
2. 将完整结构化历史和工具说明序列化成文本。每次新建校园会话，避免校园续聊与客户端历史重复叠加。这不等于原生 system 优先级，仍受校园隐藏指令影响。
3. GLM 返回一个严格 JSON action，适配层检查类型、工具名、JSON Schema 参数、tool_choice，再转换成标准 tool_calls 或普通回答。
4. 适配层绝不执行工具。Kilo 执行前的授权、目录限制和命令确认仍由 Kilo 管理。保持自动批准关闭。

只支持一次返回一个工具调用，客户端允许并行也不会自动并发。流式请求返回等待心跳，完整 action 校验通过后才发 SSE delta、finish_reason 和 [DONE]；这是**缓冲 SSE，不是真正逐字流式**。不会伪造 token 使用量，usage 不可用。校园 thought 事件不发给 Kilo，也不保存到实验数据库。

## 启动（由用户执行）

项目目录：`<PROJECT_DIR>`。

项目虚拟环境已增加 jsonschema 及其依赖，未改全局 Python。新部署可以执行 `uv sync --extra kilo --group dev` 安装锁定依赖（不需要在当前机器重复安装）。

使用上次成功人工登录的目录，例如：

```powershell
.\start_kilo_bridge.ps1 -AuthDirectory '.web2api-v2/auth-agent-<SESSION_ID>' -AcknowledgeUpstreamTools
```

启动时检查身份与模型目录，失败就退出，不自动登录或降级。若认证已过期，使用原 diagnose 命令在**新目录**人工登录，再把新目录传给启动脚本。不自动选取“最新”认证文件，不覆盖旧认证。

不要同时运行多个使用同一校园账号的在线测试/服务；这个实验实例与 v2 的发送限速并非账号级共享。数据目录有进程锁，禁止多个 worker；没有后台服务、自动重启或开机启动。

## Kilo 手动添加实验提供方

版本兼容：以下界面步骤最初在 Kilo 7.7.2 验证；本机现安装 7.7.5，尚未针对 7.7.5 重新逐项验收。协议选项（OpenAI Compatible / Chat Completions）保持不变，其余界面措辞可能随版本微调，先确认再填写。

保留原 DeepSeek 配置，新增 Custom provider，不直接替换原项：

- Provider ID：`campus-glm-experimental`
- Provider API：**OpenAI Compatible / Chat Completions**，不是 Responses 或 Anthropic Messages
- Base URL：`http://127.0.0.1:8768/v1`
- API key：本项目 `.env.v2` 的 `WEB2API_V2_API_KEY`，不要用校园密码
- Model ID：`glm-5.3`
- Tools/tool_call：启用；reasoning/图片：不要启用
- 首轮输出预算：2048 tokens。上下文窗口（context window）设保守值：建议 `32000`（不超过约 40000），让 Kilo 在请求体逼近本机 256 KiB 上限**之前**就自动压缩，不是对校园模型真实容量的声明
- 自动压缩：启用，并依赖它收缩上下文；**不要**对已经涨很大的会话手动 `/compact`（实测手动整段压缩会一次性把未裁剪历史+全部工具塞进来，直接 413）
- 自动批准：关闭；不要打开生产项目或含秘密的目录

版本界面可能不同，先确认 API 协议选项。当前适配器不实现 Responses API，也不支持图片、音频、任意扩展字段、schema $ref/远程引用；遇到未支持的输入返回 400，不能把它当成需要反复重试的暂时错误。完整请求/编码后文本上限 256 KiB，最大 256 条消息、64 个工具；超限 413，不静默裁剪。

### 把上下文压进 256 KiB（避免 /compact 触发 413）

256 KiB 是**字节**上限，而 Kilo 的压缩阈值按 **token** 计；对代码/英文为主的 transcript，经验比约 3.5 字节/token，262144 字节约等于 7.5 万 token 的硬顶。留足工具目录与密集内容的余量，把 provider 的 context window 设到 `32000`（区间 24000–40000），即可让自动压缩在安全位置触发。

调好后用桥接层自身读数验证，而不是猜 token：

- `GET /requests/{id}` 的 `diagnostics.encoded_bytes` 就是我们实际编码后送出的字节数。看**压缩前最后一条**常规轮次，应稳定 `< 262144` 并留余量；仍偏高就线性下调 context window。
- `diagnostics.input_composition.tool_results` 是主要增长点（大文件读取等工具输出）。若单条工具结果就有几十 KB，即便轮次不多也会顶到上限——在 Kilo/agent 侧限制单次读取体量比放宽窗口更有效。
- 校准可复用只读捕获代理：正常跑一段任务后观察 `requests.jsonl`，确认压缩前请求已落到 256 KiB 内。

`MAX_BODY=256 KiB` 是适配层的刻意安全上限，本方案不改它，只调整 Kilo 触发压缩的时机使其落在上限内。

官方自定义提供方说明：https://kilo.ai/docs/code-with-ai/agents/custom-models

## 无 Kilo 的小规模在线验收

先不要启动常驻服务。在项目终端执行（目录不可已存在）：

```powershell
.\.venv\Scripts\python.exe -m cuhk_shenzhen_web2api.kilo_bridge.smoke --auth-dir '.web2api-v2/auth-agent-<SESSION_ID>' --output-dir '..\..\outputs\kilo-smoke-NEW-NAME' --acknowledge-upstream-tools
```

最多两轮真实校园请求，在进程内通过适配层 HTTP 接口进行模拟工具往返，不监听端口、不读写测试文件、不执行命令；结果和独立数据库写入新输出目录。第一轮要求工具，第二轮回传随机值并要求普通回答。遇到失败停止，不重试。通过后才在 Kilo 中用一次性无敏感测试项目验证读取文件，再逐步测试经确认的修改与测试命令。

## 故障、重试与存储

- 密钥保护除 /health 外全部路由。没有远程工具执行、文件路径读取或删除接口，没有通配 CORS。
- 服务将完整请求历史持久化到独立 SQLite。虽然日志不输出这些内容，数据库仍含发送的代码/文本，需视为敏感数据妥善保存；不要放网络共享。
- Idempotency-Key 相同且输入一致返回已有记录，冲突/执行中的重复返回 409。没提供 key 时按规范化完整请求哈希去重，因此完全相同的输入会复用结果，不会再次生成。
- 改变请求或提供新的 key 会形成新请求。客户端若自行改变正文重试，本地不能识别为同一次操作。不要在 unknown 后换 key 盲目重发。
- 提交后中断、校园内置工具活动为 unknown；不重发。模型返回格式错误或参数不匹配为协议错误；不自动修补或要求模型重答。校园明确未提交的连接失败/限流仍采用复用调度器的最多 3 次有限重试。
- GET `/requests/{id}` 只返回上游状态与诊断，不暴露完整历史。上游 done 不等于 action 校验成功，以聊天接口是否成功为准。SSE 的 HTTP 200 也不等于成功，应检查 error/finish_reason。
- 认证恢复复用 v2 的机制（见下节），但发送门控不变：非 authenticated、paused 或 maintenance 期间不投递，unknown 永不自动重发。

## 认证恢复与对账

适配层复用 v2 的认证恢复原语，无需重启即可人工刷新身份，且全程保留校园内置工具的安全过滤。所有端点受密钥保护、仅本机；不自动登录、不选取“最新”Cookie、不覆盖旧认证目录，也不打印或落库密钥与 storage_state 内容。

- GET `/ready` 返回聚合状态码：`ready`（已认证且未暂停）、`waiting_login`（未认证或已暂停、无近期失败）、`recovering`（正在 drain 或换发）、`failed`（换发/校验后仍有错误）。HTTP 仅在 `ready` 时返回 200，其余 503。`authentication` 字段是传输层上报的原始状态；`identity_last_verified_at` 只有在 `/auth/check` 探针或 `/auth/reload` 换发**真正确认**认证后才写入时间戳，缓存的 authenticated 不会伪装成实时验证，未验证过为 null。
- POST `/auth/check` 在 drain（暂停派发、等待在途任务清空）下调用一次 `probe`。确认 authenticated 则 `engine.resume()` 恢复派发并记录验证时间；不新提交校园请求、不触发登录。用于人工在浏览器侧完成登录后，让服务核对身份并复投队列。
- POST `/auth/reload` 请求体 `{"directory": "<auth_root 下的新目录名>"}`。在 `auth_lock` 串行保护下用带过滤的工厂换发传输层：先在新目录构建候选并 `authenticate`，成功才原子替换 `engine.transport`、`resume`、关闭旧连接；失败返回 409 并把 `/ready` 标为 `failed`，保留旧传输不降级。目录名非法（含路径分隔符或 `.`/`..`）返回 400。
- POST `/requests/{rid}/reconcile` 仅处理状态为 `unknown` 且带 chat_session_id 的记录：在 drain 下向 `/getHistoryItem/` 拉取该会话历史，用严格证据（匹配会话、`approach_msg_idx` 对齐、role=approach、status=finished 及 parent_idx）判断校园侧是否真的完成，命中则把该 unknown 落为 done 并补写 result，否则原样返回。非 unknown 或缺会话直接短路返回 `not_unknown_or_missing_session`；记录不存在返回 404。该端点只读核对、绝不重发，unknown 在证据不足时保持不变。

运维流程（由用户执行，人工登录）：

```powershell
.\.venv\Scripts\python.exe -m cuhk_shenzhen_web2api.v2.reauth --directory auth-recovery-YYYYMMDD-HHMM --server http://127.0.0.1:8768
```

`reauth` 在 `auth_root` 下新建目录并走 diagnose 人工登录，登录成功后自动向 `--server` 发出 `/auth/reload` 完成热换发；不指定 `--server` 默认打到 v2 的 8767，本实验须显式指向 8768。firecrawl 后端不支持本地手动恢复，`reauth` 会直接拒绝。也可先手动登录新目录，再自行 `POST /auth/reload`；或登录后仅用 `POST /auth/check` 核对复用现有目录。换发后 `/ready` 的 `identity_last_verified_at` 更新即表示身份已实时验证。

## 结构化诊断

为定位「上游失败」与「动作校验失败」不再混淆，接口与持久化记录新增以下脱敏信息（旧字段全部保留，只增不改）：

- 400/413 拒绝响应的 `detail` 现为 `{message, code}`。`message` 文案不变；`code` 为脱敏原因：本地协议拒绝返回结构化短码（如 `unsupported_tool_choice`、`duplicate_json_key`），无法安全归一的消息级校验回退为 `request_rejected`；超限为 `request_body_too_large`、`encoded_transcript_too_large`。绝不回显正文、密钥或异常原文。
- 聊天接口（非流式 502 与 SSE error frame）的 `error` 新增 `code`：上游未产出有效回答时为落库的上游错误码，动作校验失败时为对应协议短码。文案保持原样。
- GET `/requests/{id}` 新增：
  - `action_validation`：上游 `done` 但动作校验失败时记录其 code，否则为 null。用于区分「上游已完成」与「Kilo 仍拿到 error」。
  - `diagnostics`：`upstream_http_status`（上游 HTTP 状态，若可捕获）、`known_not_submitted`（是否确定未提交，true 才可安全重投）、`upstream_received`（是否收到过任何事件）、`encoded_bytes`（编码后输入字节）、`approaching_size_limit`（当 `encoded_bytes ≥` 软阈值 `MAX_BODY*0.85`≈218 KiB 时为 true，提示在真正 413 前应更早压缩；只读派生、不强制、不改上限）、`attempts`、`elapsed_ms.{queue,run,total}`。旧记录缺字段时返回 null。
  - `diagnostics.input_composition`：把 `encoded_bytes` 按 `system_preamble`（固定协议前言，恒定）、`tool_definitions`、`message_history`（非 tool 结果消息）、`tool_results`（role=tool 结果，长任务的主要增长点）、`structural_and_control`（JSON 结构与 model/max_tokens/tool_choice 等控制字段）拆分，各桶相加严格等于 `total`。均为字节整数，不含 token 估算、不含正文；用于判断长任务输入被什么撑大（工具目录 vs 历史 vs 工具输出），不据此自动裁剪或扩大上限。
- 归因示例（如历史 `req_d349f851…` 类笼统失败）：先看 `upstream_status`（done→看 `action_validation`；unknown→看 `known_not_submitted` 与 `upstream_received`；error→看 `error`/`upstream_http_status`），再决定是本地协议问题还是上游问题。诊断字段与 error 一样不触发自动重发；unknown 仍须人工核实。
- 诊断以脱敏标量写入 `requests.fields`，不改变状态机、不覆盖 result/error，也不含消息正文；相关脱敏由离线测试断言。

## /compact 实测形态

一次真实 /compact 脱敏捕获（仅结构，无正文）确认：Kilo 把**整段未裁剪历史**（样例 111 条消息、41 assistant、65 tool 结果）连同**完整工具目录**（25 个工具、tool_choice=auto）以普通 chat/completions 重发，`max_tokens=32000`、stream=true，请模型写摘要。适配层不把它当特殊字段，按普通 transcript 处理；离线回归 `test_compact_request_shape_round_trips_as_transcript` 复刻该形态并校验通过（保留长程 tool_call/result 配对）。

限制：该样例请求体 593 KB 超过本机 `MAX_BODY=256 KiB`，被明确判 413（`request_body_too_large`）且不投递——三项字段上限（消息 256、工具 64、max_tokens 32768）均通过，唯一卡点是体积。/compact 本身是长任务收缩上下文的手段，若在收缩前就因体积被拒，自动压缩路径对超长会话不可用。当前保持“明确 413、不静默裁剪、不投递”的安全行为；提高上限或更早触发压缩涉及成本与上游限制，属独立决策，需另行确认。详见该次捕获目录内的 `T2.2_readback.md`。

## 回退

在 Kilo 切回原提供方即可，不需要修改 v2、删除文件或 Git 回滚。是否停止实验服务由用户决定。所有实验数据保留，不覆盖或自动合并旧数据库。
