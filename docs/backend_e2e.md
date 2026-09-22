# 后端 E2E 测试

`scripts/e2e_test.py` 是面向已经启动并完成登录的本地服务的在线验收脚本，不属于 CI 的离线检查。它会调用真实 CUHK chat 服务，消耗账号额度，并在 `data/` 下留下服务自身的会话状态。

脚本默认使用 `campus-affairs-qa`。这是配置中标记为 `requires_balance=false` 的校园事务模型，因此适合做在线验收；也可以通过 `--model` 显式覆盖。

先启动服务（服务端会按 `.env` 恢复或建立浏览器会话）：

```powershell
.venv\Scripts\python.exe src\cuhk_shenzhen_web2api\scripts\server.py 2>&1
```

另开 PowerShell 执行场景：

```powershell
# 1. 普通一问一答
.venv\Scripts\python.exe scripts\e2e_test.py --scenario qa 2>&1

# 2. 同一 chat_session_id 连续三轮，后两轮使用上一轮 approach_msg_idx
.venv\Scripts\python.exe scripts\e2e_test.py --scenario conversation 2>&1

# 3. 注册并调用 pwsh 工具，同时检查 /tools/log
.venv\Scripts\python.exe scripts\e2e_test.py --scenario tools 2>&1

# 三类全部执行
.venv\Scripts\python.exe scripts\e2e_test.py --scenario all 2>&1
```

## 三类断言

1. 普通问答检查 `/health`、`/model`、`POST /chat` 的会话 ID、assistant 索引和非空文本。
2. 连续对话固定三轮，检查会话 ID 不变、每轮都返回新的 `approach_msg_idx`，并且客户端确实把上一轮索引作为下一轮 `parent_idx` 发送。
3. 工具场景使用确定性的 `Write-Output 'web2api-e2e-pwsh'`。脚本先在本机执行一次命令确认 PowerShell 可用，再通过 `/tools` 注册 `pwsh`、通过 `/tools/call` 调用，并检查执行日志。

当前 `POST /tools` 的动态注册实现是仓库中明确标注的 placeholder handler，因此默认测试验证的是“注册—调用—日志”契约，并会打印这一限制；它不会把 placeholder 误报成真的 PowerShell 执行。若部署了真正的 pwsh handler，可加：

```powershell
.venv\Scripts\python.exe scripts\e2e_test.py --scenario tools --require-real-pwsh 2>&1
```

此选项在服务仍返回 placeholder 时会失败，便于后续把工具执行接入真实实现。
