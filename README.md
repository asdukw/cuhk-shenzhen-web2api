# CUHK-Shenzhen Web2API

CUHK-Shenzhen AI chat 的本地 HTTP 桥接服务。浏览器会话只支持仓库管理的本地 Steel，不依赖云端浏览器服务，也不再需要 Docker。登录和聊天请求会在同一个浏览器页面上下文中执行。

## 启动（Windows PowerShell）

需要 Python 3.12、`uv`、Node.js 22+、npm 和 Google Chrome/Chromium。首次运行先从源码安装 Steel：

```powershell
uv sync
.\scripts\local_steel.ps1 setup
Copy-Item .env.example .env
# 编辑 .env，填入 CHAT_USERNAME / CHAT_PASSWORD（或 USERNAME / PASSWORD）
uv run gateway
```

`gateway` 会启动本地 Steel API 和 Node executor，等待服务就绪；退出时只停止由本次 gateway 启动的 Node 进程。如果 Node 服务已经由 `local_steel.ps1 up` 启动，则保留它们。服务监听 `http://127.0.0.1:8765`，首次请求时建立或恢复 Steel 会话并登录。通过 `GET /health` 检查状态；按 `Ctrl+C` 停止服务。

日常也可以分开管理 Steel：

```powershell
.\scripts\local_steel.ps1 up
.\scripts\local_steel.ps1 status
.\scripts\local_steel.ps1 verify
.\scripts\local_steel.ps1 logs
.\scripts\local_steel.ps1 down
```

浏览器后端默认是 `steel-local`，可以通过 `.env` 调整 `STEEL_EXECUTOR_URL`、`STEEL_EXECUTOR_TOKEN`、`STEEL_EXECUTOR_TIMEOUT`、`STEEL_API_PORT`、`STEEL_CDP_PORT`、`STEEL_HEADLESS`、`CHROME_EXECUTABLE_PATH` 和 `CHROME_USER_DATA_DIR`。旧的云端配置不再生效，显式选择非本地浏览器后端会报错。

详见 `deploy/steel/README.md` 和 `AGENTS.md`。不要提交 `.env` 或 `data/` 中的会话数据。
