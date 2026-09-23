# CUHK-Shenzhen Web2API

CUHK-Shenzhen AI chat 的本地 HTTP 桥接服务。浏览器会话仅支持仓库管理的本地 Steel，不依赖云端浏览器服务。登录和聊天请求会在同一个浏览器页面上下文中执行。

## 启动（Windows PowerShell）

需要 Python 3.12、`uv` 和 Docker Compose。在仓库根目录执行：

```powershell
uv sync
Copy-Item .env.example .env
# 编辑 .env，填入 USERNAME 和 PASSWORD（或 CHAT_USERNAME / CHAT_PASSWORD）
.\scripts\local_steel.ps1 up
.\scripts\local_steel.ps1 verify
.venv\Scripts\python.exe src\cuhk_shenzhen_web2api\scripts\server.py 2>&1
```

服务监听 `http://127.0.0.1:8765`，首次请求时建立或恢复 Steel 会话并登录。通过 `GET /health` 检查状态；按 `Ctrl+C` 停止服务。浏览器后端默认是 `steel-local`，可以通过 `.env` 调整 `STEEL_EXECUTOR_URL`、`STEEL_EXECUTOR_TOKEN` 和 `STEEL_EXECUTOR_TIMEOUT`。旧的云端配置不再生效，显式选择非本地浏览器后端会报错。

详见 `deploy/steel/README.md` 和 `AGENTS.md`。不要提交 `.env` 或 `data/` 中的会话数据。
