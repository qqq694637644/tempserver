# Windows 长期运行部署

`start.bat` 适合调试，不负责开机启动、崩溃恢复或日志轮转。公网长期运行建议采用以下结构：

```text
Internet -> HTTPS 反向代理 -> 127.0.0.1:8000 -> tempserver
```

## 前置要求

1. 按主 README 创建虚拟环境、安装 `requirements.lock.txt` 并配置 `.env`。
2. 设置 `SESSION_COOKIE_SECURE=true`。
3. 将 `FILE_STORAGE_DIR` 放在专用数据盘目录。
4. 先在 PowerShell 中运行一次，确认 `/healthz` 返回 200。
5. Uvicorn 只监听 `127.0.0.1`，不要直接监听公网网卡。

## 使用 NSSM

NSSM 可把 Uvicorn 包装为 Windows 服务并在崩溃后自动重启。以下示例假设项目位于 `C:\tempserver`：

```powershell
nssm install tempserver "C:\tempserver\.venv\Scripts\python.exe" "-m uvicorn app.main:app --host 127.0.0.1 --port 8000"
nssm set tempserver AppDirectory "C:\tempserver"
nssm set tempserver AppStdout "C:\tempserver\logs\tempserver-out.log"
nssm set tempserver AppStderr "C:\tempserver\logs\tempserver-error.log"
nssm set tempserver AppRotateFiles 1
nssm set tempserver AppRotateOnline 1
nssm set tempserver AppRotateBytes 10485760
nssm set tempserver AppExit Default Restart
nssm set tempserver Start SERVICE_AUTO_START
nssm start tempserver
```

请先创建 `C:\tempserver\logs`，并确保服务账户对项目目录、日志目录和 `FILE_STORAGE_DIR` 有读写权限。

检查状态：

```powershell
nssm status tempserver
Invoke-RestMethod http://127.0.0.1:8000/healthz
```

更新代码前先停止服务，完成更新和测试后再启动：

```powershell
nssm stop tempserver
# 更新代码、安装锁定依赖、运行测试
nssm start tempserver
```

## 使用任务计划程序

不安装服务包装器时，可在“任务计划程序”中创建“计算机启动时”任务：

- 程序：`C:\tempserver\.venv\Scripts\python.exe`
- 参数：`-m uvicorn app.main:app --host 127.0.0.1 --port 8000`
- 起始位置：`C:\tempserver`
- 选择“无论用户是否登录都运行”
- 失败时每 1 分钟重启，至少重试 3 次

任务计划程序的日志轮转和进程恢复能力弱于 NSSM，公网长期运行更推荐服务包装器。

## 反向代理要求

反向代理至少应做到：

- TLS 终止和证书自动续期；
- 所有 HTTP 请求跳转到 HTTPS；
- 上传请求体上限不高于应用的 `MAX_UPLOAD_REQUEST_BYTES`；
- `/admin/login` 登录速率限制；
- 访问日志与日志轮转；
- 将应用上游固定为 `127.0.0.1:8000`；
- 定期请求 `/healthz`，存储不可用时告警。

不要配置多个 Uvicorn worker，也不要启动多个 tempserver 实例共享同一个 `FILE_STORAGE_DIR`。
