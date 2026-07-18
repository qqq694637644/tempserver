# tempserver

一个面向 Windows 部署的轻量文件下载站，使用 Python、FastAPI 和服务端 HTML 页面实现。

## 功能

- 所有人无需登录即可查看文件名、文件大小并直接下载。
- 首页右下角有一个低可见度的小锁图标，点击进入管理员登录页。
- 管理员账号、密码、文件目录、会话密钥和安全选项均通过 `.env` 配置。
- 管理员登录后可以上传文件、覆盖同名文件和删除文件。
- 上传、覆盖和删除权限均由后端校验，不依赖隐藏入口保障安全。
- 不提供搜索、分类、下载限制或操作日志。
- 程序不主动限制上传文件的类型、大小和数量；实际容量仍受 Windows 文件系统、磁盘空间及部署环境限制。

## Windows 安装

建议使用 Python 3.11 或更高版本。

```powershell
git clone https://github.com/qqq694637644/tempserver.git
cd tempserver

py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

编辑 `.env`。示例文件故意将密码和会话密钥留空，未填写时程序会拒绝启动：

```dotenv
ADMIN_USERNAME=admin
ADMIN_PASSWORD=请设置至少12位的强密码
FILE_STORAGE_DIR=D:/tempserver/files
SESSION_SECRET=请使用下方命令生成随机值
SESSION_COOKIE_SECURE=false
LOGIN_MAX_ATTEMPTS=5
LOGIN_WINDOW_SECONDS=300
```

可使用下面的命令生成会话密钥：

```powershell
py -c "import secrets; print(secrets.token_urlsafe(48))"
```

不要提交 `.env`。程序会拒绝仓库中出现过的示例占位值、少于 12 位的管理员密码和可预测的会话密钥。启动时会自动创建 `FILE_STORAGE_DIR` 指定的目录。

`LOGIN_MAX_ATTEMPTS` 表示同一客户端在时间窗口内允许的失败登录次数，`LOGIN_WINDOW_SECONDS` 表示窗口秒数。默认是 5 次/300 秒，生产环境还应在反向代理层限制 `/admin` 路径。

本地纯 HTTP 使用 `SESSION_COOKIE_SECURE=false`。通过 HTTPS 对公网提供服务时必须设置为 `true`，并在反向代理中强制所有 HTTP 请求跳转到 HTTPS，否则浏览器不会在 HTTP 管理请求中发送管理员 Cookie。

## Windows 覆盖与删除行为

上传文件会保存为内部唯一版本，公开文件名通过原子清单映射到当前版本。覆盖或删除时只切换清单：

- 已经开始的下载继续读取旧版本；
- 新下载立即读取新版本，或在删除后立即返回 404；
- 不会尝试替换或删除正在被 Windows 下载句柄占用的文件。

旧版本和异常退出遗留的临时文件不会出现在下载列表中。程序启动时会清理超过 24 小时且不再被引用的内部文件。为避免多个进程相互清理，当前部署方式应只启动一个 Uvicorn worker。

## 启动

双击 `start.bat`，或在已激活虚拟环境的 PowerShell 中运行：

```powershell
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

然后访问：

- 下载页：`http://127.0.0.1:8000/`
- 管理页：点击下载页右下角的小锁图标，或访问 `http://127.0.0.1:8000/admin/login`

若服务器开放到公网，应在 FastAPI 前配置 HTTPS 反向代理、强制 HTTP 跳转 HTTPS、设置 `SESSION_COOKIE_SECURE=true`，并限制管理入口的网络访问范围。

## 测试

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest
```