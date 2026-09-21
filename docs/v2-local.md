# 本机传输与可靠队列（独立 v2）

v2 默认端口 8767，旧服务 8766、旧 data/ 和原启动脚本不变。没有在线验证报告前，不能把 v2 的离线通过视为校园直连已成功。

## 1. 准备与人工登录

以下命令在仓库根目录的 PowerShell 中由用户执行。依赖只进入项目虚拟环境；浏览器下载到项目目录。不需要 GPU、管理员权限或全局安装。

```powershell
uv sync --extra local --locked
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path (Get-Location) '.playwright-browsers'
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m cuhk_shenzhen_web2api.v2.diagnose --data-dir .web2api-v2/auth-first --backend httpx --chat
```

诊断会打开专用浏览器。人工完成 SSO 后，在终端按 Enter。`--chat` 明确允许两轮短聊天；不加该参数只检查身份和配置，不能证明聊天可用。每次诊断必须使用全新的目录。`storage_state.json` 是登录凭据，不要上传、提交或分享。报告不含身份正文、Cookie 或密钥。

如果 HTTPX 校验失败，可使用另一个新目录，以 `--backend playwright --chat` 单独验证浏览器请求。不要混用网络出口，也不要复制旧 Firecrawl Cookie 来假定本机可用。未验证的后端不自动切换。

诊断需要项目浏览器二进制。缺失时先执行上面的安装命令。程序不自动安装浏览器或修改系统代理。

## 2. 配置与启动

参考 `.env.v2.example`，创建原先不存在的 `.env.v2`，填入长随机 `WEB2API_V2_API_KEY`、诊断的 `WEB2API_V2_AUTH_DIR`、后端和实际模型 ID。不要使用示例密钥。模型 ID 以当前校园目录为准；新模式不猜测模型别名。

也可以设置当前终端的 `WEB2API_V2_*` 环境变量；这些值优先于 `.env.v2`。不会读取旧 `.env` 的用户名或密码。

```powershell
.\start_v2.ps1
```

只运行一个实例。进程锁阻止同一数据目录被第二个调度器接管。默认数据目录 `.web2api-v2/runtime`，默认并发 1，发送起始间隔 20 秒。所有服务进程需由用户启动；脚本不会终止旧服务。

```powershell
$headers = @{Authorization="Bearer $env:WEB2API_V2_API_KEY"}
Invoke-RestMethod http://127.0.0.1:8767/health
Invoke-RestMethod http://127.0.0.1:8767/ready -Headers $headers
Invoke-RestMethod http://127.0.0.1:8767/v1/models -Headers $headers
```

如果密钥只写入 `.env.v2`，请在调用端单独设置相同密钥；服务端读配置不会自动给 PowerShell 设置环境变量。

`/health` 只证明本机存活；`/ready` 展示脱敏认证、队列、并发和流式能力。身份检查用 `POST /auth/check`，不创建登录流程。现在可使用 `python -m cuhk_shenzhen_web2api.v2.reauth --directory auth-NEW-NAME` 人工登录并申请热载入；仅接受 AUTH_ROOT 的直接子目录。切换先暂停调度、等待在途完成、验证新后端，失败保留旧后端。热载入不修改配置文件，下次重启前需人工更新 AUTH_DIR。旧数据库保留，未提交任务继续排队；未知结果不重发。

Firecrawl 回退需显式配置 `WEB2API_V2_BACKEND=firecrawl`、`WEB2API_V2_FIRECRAWL_API_KEY`、`WEB2API_V2_FIRECRAWL_SESSION_ID`；只复用已登录会话，不自动登录，不写旧会话文件。Firecrawl 保持串行、缓冲输出。Playwright 默认仍缓冲且并发 1；增量模式和验收门槛参见 `v2-optimization.md`。

## 3. API 与可靠性

- 原生 `/chat`、`/response`、`/chat/stream` 接收 message、approach_id、conversation 等字段。HTTPX 会实时转发上游 NDJSON。
- 文本兼容接口 `/v1/chat/completions`、`/v1/responses` 支持单条用户文本、流式输出和显式 conversation；previous_response_id 可续接本模式生成的已完成响应。包含 system/developer 消息或多条内联历史的输入返回 400，不会丢弃部分输入后继续调用。不是完整 OpenAI 协议。
- 新模式不执行远程工具，也不接受本机文件路径。已有 image_ids/file_ids 可透传；上传入口和完整图片/工具兼容仍使用旧模式，尚未在 v2 实现。
- `/jobs` 支持 independent/thread；`pause_seconds=0` 确实不追加任务间隔，独立任务可并发。正数表示同一任务两项间在上一项完成后等待。
- 实时与批量共用 FIFO；同一实际校园会话串行。严格 FIFO 会在队首等待同一会话或任务间隔时阻塞后续项，优先保证顺序，不是最高吞吐调度。
- 队列上限 1000（queued + running）；批量入队全有或全无。拥塞返回 429。
- `Idempotency-Key` 防止重复提交；输入冲突返回 409。执行中的重复流式连接不接管原请求。查询 `/requests/{id}` 可看结果。
- 崩溃、提交后超时、流断开等不确定状态记为 unknown，不自动重发。只有明确未提交的连接故障或明确 429 拒绝才最多尝试三次。认证失败暂停调度。
- `POST /requests/{id}/reconcile` 只查历史、不重发聊天。必须匹配会话、助手索引和明确完成证据才能事务性恢复；当前严格历史结构尚待校园在线诊断确认，不匹配保持 unknown。同名会话或指向同一校园会话的别名会被阻止继续发送，返回 conversation_unresolved。需要独立任务时显式使用全新 conversation。
- SQLite 是本机磁盘数据库；不要放网络共享，不要启动多个 worker。任务和结果会长期保留，当前无自动清理。

所有接口（除最小 `/health`）需要 Bearer 或 X-API-Key，包括文档。Swagger 页面本身受保护，需要能附加认证头的客户端访问。

## 4. 导入与在线分档验收

旧 JSON 只读导入到新目录：

```powershell
.\.venv\Scripts\python.exe -m cuhk_shenzhen_web2api.v2.migrate --source data/chat_session --output-dir .web2api-v2/import-first
```

查看 migration-report.json 的冲突和无效项后，再显式将 DATA_DIR 指向导入目录。历史 running 项变成 unknown，不自动执行。不会修改源文件。

小规模联网验收由用户运行，每档最多 12 条。先用默认并发 1；通过后调整并发并重启 v2，再进行下一档。保持模型一致。任何失败停止继续投递；已在途请求仍可能完成。

```powershell
.\.venv\Scripts\python.exe -m cuhk_shenzhen_web2api.v2.benchmark --stage 1 --count 12 --model YOUR_ACTUAL_MODEL --output-dir .web2api-v2/bench-1
# 并发配置改为 2 并由用户重启 v2 后：
.\.venv\Scripts\python.exe -m cuhk_shenzhen_web2api.v2.benchmark --stage 2 --model YOUR_ACTUAL_MODEL --previous .web2api-v2/bench-1/benchmark.json --output-dir .web2api-v2/bench-2
```

报告统计首文字延迟（包含队列等待）、总时长、吞吐、成功率与独立会话检查。输出 token 上限通过 params 请求，是否支持由校园模型决定；客户端另设输出长度保护。无法据这 12 条推断日配额或长期稳定性。

## 5. 跨设备与验证命令

Tailscale 安装和组网需要用户另行确认。完成后把 HOST 设置为本机的 Tailscale IPv4 地址。程序只允许回环或 100.64.0.0/10 地址；该检查不代替 Tailscale 身份验证或 ACL。保留 API 密钥，不开放公网端口，不使用 0.0.0.0。远端设备先验证 /health，再带密钥验证 /ready 和单条聊天。

离线测试命令（无校园请求）：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_v2*.py' -v
.\.venv\Scripts\ruff.exe check src tests
.\.venv\Scripts\ruff.exe format --check src tests
.\.venv\Scripts\pyright.exe --pythonpath .venv/Scripts/python.exe
.\.venv\Scripts\python.exe -m compileall -q src
```

回退无需删除数据库或改 Git 历史：继续使用旧服务 8766 即可。v2 的新结果留在独立目录，不自动合并回旧 JSON。
