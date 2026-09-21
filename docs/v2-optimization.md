# v2 顺序优化与验收

代码已提供离线验证能力，不代表校园在线验收完成。旧数据、旧登录目录和运行中服务不自动改变。默认保持 Playwright 原有 buffered / 并发 1 / 20 秒；不要跳过基准直接升档。

## 1. 基准

本轮代码需要由用户确认后重启 v2 才生效。一次只启动一个服务进程，使用现有 `start_v2.ps1`。不覆盖 `.env.v2`，只在确认后人工修改需要的配置项。

使用带密钥的 `/v1/models` 取得真实模型 ID，然后运行：

```powershell
.\run_v2_benchmark.ps1 -Model '实际模型ID'
```

每次脚本生成 UUID 新报告目录。每档默认 12 条，固定标识回显提示、请求 max_tokens=64；校园是否遵守 token 参数需在线确认。报告分开记录客户端首段/总耗时，服务端排队/首字/生成/总耗时的中位数和最大值，以及成功率、吞吐、429 次数和 unknown 数量。服务端生成耗时定义为进入传输到终态，不是纯解码时间。旧请求缺少时间字段时返回 null，不推造数值。

第一档通过后，确认配置与重启，再分别将间隔改为 10、5，并运行：

```powershell
.\run_v2_benchmark.ps1 -Model '实际模型ID' -Interval 10 -Previous '.web2api-v2/上一档/benchmark.json'
# 10 秒档通过且服务已改为 5 秒后：
.\run_v2_benchmark.ps1 -Model '实际模型ID' -Interval 5 -Previous '.web2api-v2/10秒档/benchmark.json'
```

脚本校验服务实际配置、模型和前档参数，不修改配置。出现失败、429、unknown 或回显/会话异常停止继续投递；已在途请求仍会收尾。上游明确未提交的拒绝仍遵循调度器原有有限重试，但任何 429 都使本档验收失败。

## 2. 增量流式

新增每请求独立 ReadableStream、AbortController 和最多 8 项浏览器队列；单行 64 KiB、累计 2 MiB。Python 消费速度向浏览器施加背压。断连清理不表示校园已停止生成，仍按 unknown 处理。非流式接口聚合相同事件。

先记录 buffered 基准，再以新目录人工诊断（会发送两轮聊天）：

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PWD '.playwright-browsers'
.\.venv\Scripts\python.exe -m cuhk_shenzhen_web2api.v2.diagnose --data-dir .web2api-v2/auth-stream-NEW --backend playwright --stream-mode incremental --chat --inspect-history --model '实际模型ID'
```

第一轮数字输出用于观察首段是否早于结束，第二轮验证续聊。报告 `stream_verified=true` 才可将 WEB2API_V2_STREAM_VALIDATION 指向该目录的 report.json，并设置 WEB2API_V2_STREAM_MODE=incremental。仅设置 incremental 可实验，但未配置通过报告时 `/ready.streaming=false`，并发仍限制 1。诊断中的 streaming 是该临时实例启动时的声明，stream_verified 是本次实测结论。

由用户确认重启后，用相同模型、条数、间隔重跑基准并通过 -Previous 引用 buffered 报告。首字体验改善与吞吐提升分别比较，不自动改默认。

## 3. 认证恢复和结果核实

```powershell
.\.venv\Scripts\python.exe -m cuhk_shenzhen_web2api.v2.reauth --directory auth-renew-NEW
```

新目录必须不存在、位于配置 AUTH_ROOT 下。人工登录，不读取或填写密码。恢复命令通过现有 API 密钥向服务发送目录名；服务不接受任意路径。等待在途结束上限 180 秒，失败不丢队列、不换旧后端。切换后人工更新 AUTH_DIR，保证未来重启也使用新认证；命令不会覆盖配置。

unknown 核实接口：`POST /requests/请求ID/reconcile`，鉴权同其他接口。只查询 `/getHistoryItem/`，不会再次发送聊天。严格适配器目前要求顶层 chat_session_id、唯一 self_idx、role=approach、status=finished、匹配 parent_idx 及文本 items。实际校园结构需由上面的 --inspect-history 验证；诊断只输出结构键名、匹配结论/原因，不落盘完整历史。若不匹配，应提供脱敏结构样例另行适配；保持 unknown，不放宽证据条件。新 parent 已推进等冲突也拒绝恢复。

## 4. 并发与私有设备

完成流式和认证恢复在线验收后，确认后将并发改为 2；间隔和模型保持通过配置，用 -Concurrency 2 和前一档报告执行最多 12 条。吞吐未提升至少 10% 时报告建议保持前档；该阈值只是小样本筛选，不是统计显著性。并发 2 无异常且有改善后才单独测试 -Concurrency 4。服务不会自动升档；配置允许实验 4 不代表已验收。

Tailscale 本机已安装，但本轮只读检查显示 NoState、无私有 IP。未执行登录/组网/防火墙修改。需用户确认并完成 Tailscale 登录，再将 HOST 绑定实际本机 Tailscale IPv4；不使用 0.0.0.0。另一台设备须验证 /health、无密钥访问 /ready 被拒绝、带密钥 /v1/models 和一次短聊天。此阶段尚未完成。

## 5. 回退与边界

保留原配置数据目录；如需退回缓冲模式，确认后设置 STREAM_MODE=buffered、CONCURRENCY=1，并由用户重启。恢复 20 秒间隔即可回到保守配置。新报告不覆盖旧结果，数据库和未知项不删除，不自动合并旧模式数据。

完整 system、多轮内联历史和工具调用仍明确拒绝，不静默忽略。不宣称恰好一次、每日千条或长期高并发可用。校园验收、认证热切换、历史核实与远端设备验证分别记录，不能用本地模拟测试代替。
