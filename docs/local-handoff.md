# 本地任务交接（cuhk_shenzhen_web2api）

给本机定时任务 / 脚本用的接口说明。Cursor **不能**填 `127.0.0.1`（会报 Access to private networks is forbidden）。本机 `curl`、PowerShell、任务计划程序可以。

## 现在怎么跑

- 项目：`C:\Users\<you>\projects\cuhk_shenzhen_web2api`
- 服务：**http://127.0.0.1:8766**（8765 被别的「学业助手」占用）
- 密钥：项目根目录 `.env` 里的 `WEB2API_API_KEY`（不要写进 git）
- 每次请求带：`Authorization: Bearer <WEB2API_API_KEY>`
- `/health` 不需要 Key

启动（会话过期或机器重启后）：

```powershell
cd C:\Users\<you>\projects\cuhk_shenzhen_web2api
.\.venv\Scripts\python.exe src\cuhk_shenzhen_web2api\scripts\login.py --manual
.\.venv\Scripts\python.exe src\cuhk_shenzhen_web2api\scripts\server.py --port 8766
```

登录要在 Firecrawl **live view** 里完成港中深 SSO，不是你自己的 Chrome。目标站：`https://ai.cuhk.edu.cn/chat/`。

探活：

```powershell
curl http://127.0.0.1:8766/health
```

应返回 `url: https://ai.cuhk.edu.cn/chat/` 和当前用户。

## 模型

推荐：`cuhk-haiku`（实际 `claude-haiku-4-5`）

| 你传的名字 | 实际模型 |
|---|---|
| `cuhk-haiku` | claude-haiku-4-5 |
| `cuhk-gpt` | gpt-5.6-luna |
| `cuhk-glm-flash` | glm-5.3-flash |
| `cuhk-deepseek` | deepseek-v4-pro |

原生名也可以：`claude-haiku-4-5`、`glm-5.3-flash` 等。`GET /v1/models` 可列出。

## 怎么选接口

| 场景 | 用这个 |
|---|---|
| 单次问答 | `POST /chat` |
| 长期任务、要记住上下文 | `POST /chat` + 固定 `conversation` |
| 大批量（几十上百条） | `POST /jobs`，再轮询 `GET /jobs/{id}` |
| OpenAI SDK / 兼容客户端 | `POST /v1/chat/completions`，同样带 `conversation` |

**不要**对 `/chat` 并行狂打。通道是一条 Firecrawl 云浏览器，免费档大约 3 次/分钟，服务端串行。批量走 `/jobs`。

## 1. 长期任务（命名会话）

同一 `conversation` 会续港中深服务端上下文，只发这一轮新内容。

```powershell
$KEY = (Select-String -Path .env -Pattern '^WEB2API_API_KEY=').Line.Split('=',2)[1]
$headers = @{ Authorization = "Bearer $KEY"; "Content-Type" = "application/json" }

# 第一天 / 第一步
Invoke-RestMethod http://127.0.0.1:8766/chat -Headers $headers -Method POST -Body (@{
  conversation = "daily-digest"
  approach_id  = "cuhk-haiku"
  message      = "从今天起做每日摘要。先记住格式：标题 + 三条要点。"
} | ConvertTo-Json)

# 之后每天同一名字即可
Invoke-RestMethod http://127.0.0.1:8766/chat -Headers $headers -Method POST -Body (@{
  conversation = "daily-digest"
  approach_id  = "cuhk-haiku"
  message      = "补上今日变化，沿用昨天的格式。"
} | ConvertTo-Json)
```

OpenAI 形态（字段 `conversation`，或头 `X-Conversation-Id`）：

```json
POST /v1/chat/completions
{
  "model": "cuhk-haiku",
  "conversation": "daily-digest",
  "messages": [{"role": "user", "content": "继续昨天的任务：……"}]
}
```

- 列出线程：`GET /conversations`
- 丢掉上下文、重新开：`DELETE /conversations/daily-digest`

回复里会有 `conversation`、`chat_session_id`、`approach_msg_idx`、`ctx_token_cnt`。港中深自己的窗口满了会截断。

## 2. 大批量（队列）

一次丢进去，后台按 `pause_seconds`（默认 20s）串行跑。POST 立刻返回 `id`。

互相独立的很多条：

```json
POST /jobs
{
  "mode": "independent",
  "approach_id": "cuhk-haiku",
  "pause_seconds": 20,
  "items": [
    {"id": "a", "message": "总结这段：……"},
    {"id": "b", "message": "翻译这段：……"}
  ]
}
```

同一条长任务按步骤：

```json
POST /jobs
{
  "mode": "thread",
  "conversation": "report-20260916",
  "approach_id": "cuhk-haiku",
  "items": [
    {"message": "先列提纲"},
    {"message": "按提纲写第一章"},
    {"message": "写第二章"}
  ]
}
```

```powershell
$job = Invoke-RestMethod http://127.0.0.1:8766/jobs -Headers $headers -Method POST -Body ($body | ConvertTo-Json -Depth 6)
# 轮询直到 completed / failed
Invoke-RestMethod "http://127.0.0.1:8766/jobs/$($job.id)" -Headers $headers
```

- `GET /jobs` 最近任务摘要
- `DELETE /jobs/{id}` 取消还没跑的
- 落盘：`data/chat_session/jobs/`（gitignored）

100 条大约要半小时以上，这是限速，不是接口写慢。

## 3. 任务计划程序建议

1. 开机或登录后先保证 8766 在跑（上面的 `server.py`）。
2. 定时脚本只 `POST /chat` 或 `POST /jobs`，不要在任务里重新 login（除非探活失败）。
3. 先 `GET /health`；若不是 `/chat/` 或连不上，这次跳过并告警，不要死循环重试把额度打爆。
4. 长期任务用**固定** conversation 名（按业务，如 `daily-digest`、`course-sync`）。
5. 批量用 `/jobs`，脚本里 sleep 轮询即可。

探活失败常见原因：Firecrawl 浏览器会话过期（大约 1 小时量级 TTL）。需要再跑 `login.py --manual` 后重启 `server.py`。命名 conversation 映射还在，但旧 `chat_session_id` 可能失效，那时 `DELETE /conversations/<name>` 再开一条。

## 约束（写进脚本注释里）

- 本机才能打 8766；Cursor Override Base URL 填 localhost 会被 Cursor 云端拦截。
- 单会话、串行、~3 次/分钟。
- 不要把 `.env`、cookies、`data/` 提交进 git。
- 这是你自己的校园账号会话，不是港中深官方 API Key。

## 常用路径速查

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 会话是否还在 `/chat/` |
| POST | `/chat` | 单轮；加 `conversation` 续聊 |
| POST | `/jobs` | 批量入队 |
| GET | `/jobs/{id}` | 批量进度与每条 `text`/`error` |
| GET | `/conversations` | 本地命名线程 |
| DELETE | `/conversations/{name}` | 重置线程 |
| GET | `/v1/models` | 模型列表 |
| POST | `/v1/chat/completions` | OpenAI 兼容 |
