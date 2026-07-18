# tempserver

一个面向 Windows 部署的轻量文件下载站，使用 Python、FastAPI 和服务端 HTML 页面实现。

## 功能

- 所有人无需登录即可查看文件名、文件大小并直接下载。
- 首页右下角有一个低可见度的小锁图标，点击进入管理员登录页。
- 管理员账号、密码、文件目录和会话密钥均通过 `.env` 配置。
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

编辑 `.env`：

```dotenv
ADMIN_USERNAME=admin
ADMIN_PASSWORD=请替换为强密码
FILE_STORAGE_DIR=D:/tempserver/files
SESSION_SECRET=请替换为至少32位的随机字符串
```

可使用下面的命令生成会话密钥：

```powershell
py -c "import secrets; print(secrets.token_urlsafe(48))"
```

不要提交 `.env`。程序启动时会自动创建 `FILE_STORAGE_DIR` 指定的目录。

## 启动

双击 `start.bat`，或在已激活虚拟环境的 PowerShell 中运行：

```powershell
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

然后访问：

- 下载页：`http://127.0.0.1:8000/`
- 管理页：点击下载页右下角的小锁图标，或访问 `http://127.0.0.1:8000/admin/login`

若服务器开放到公网，建议在 FastAPI 前配置 HTTPS 反向代理，并限制管理入口的网络访问范围。

## 测试

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest
```