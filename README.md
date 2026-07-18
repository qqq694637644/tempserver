# tempserver

面向 Windows 公网部署的轻量文件下载站，使用 Python、FastAPI 和服务端 HTML 页面实现。项目定位是少量、小文件公开下载，不适合作为大文件网盘或海量文件索引。

## 功能与安全边界

- 所有人无需登录即可查看文件名、文件大小并直接下载。
- 首页右下角有一个低可见度的小锁图标，点击进入管理员登录页。
- 管理员登录后可以上传、覆盖同名文件和删除文件。
- 上传路由先验证服务端管理员会话，再解析 multipart；匿名请求不会触发上传文件解析或临时文件创建。
- 管理员会话在服务端登记，退出登录会立即撤销原会话 Cookie。
- 管理页面禁止缓存，并设置 CSP、禁止 iframe、MIME 嗅探、Referrer Policy 等基础安全响应头。
- 不提供搜索、分类、下载次数限制或操作日志。

默认公网限制：

- 单文件最多 50 MiB；
- 单次请求最多 100 MiB；
- 单次最多 20 个文件；
- 站点最多 500 个公开文件。

这些限制可以通过 `.env` 调整，但项目仍按“小文件、少量文件”设计。

## Windows 安装

建议使用 Python 3.11 或 3.13。

```powershell
git clone https://github.com/qqq694637644/tempserver.git
cd tempserver

py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.lock.txt
Copy-Item .env.example .env
```

`requirements.lock.txt` 固定生产依赖，`requirements-dev.lock.txt` 在此基础上固定测试依赖。更新依赖时应重新运行完整测试后再更新锁定文件。

## 公网配置

编辑 `.env`。示例文件故意将密码和会话密钥留空，未填写时程序会拒绝启动：

```dotenv
ADMIN_USERNAME=admin
ADMIN_PASSWORD=请设置至少12位的强密码
FILE_STORAGE_DIR=D:/tempserver-public-files
SESSION_SECRET=请使用下方命令生成随机值
SESSION_COOKIE_SECURE=true
SESSION_MAX_AGE_SECONDS=28800
LOGIN_MAX_ATTEMPTS=5
LOGIN_WINDOW_SECONDS=300
MAX_UPLOAD_BYTES=52428800
MAX_UPLOAD_REQUEST_BYTES=104857600
MAX_FILES_PER_UPLOAD=20
MAX_PUBLIC_FILES=500
```

生成会话密钥：

```powershell
py -c "import secrets; print(secrets.token_urlsafe(48))"
```

不要提交 `.env`。程序会拒绝示例占位值、少于 12 位的管理员密码和明显可预测的会话密钥。

`FILE_STORAGE_DIR` 必须是专用目录，不能设置为项目根目录、`app` 目录或它们的上级目录。若目录根部存在 `.env` 或 `.env.*`，程序会拒绝启动；上传和公开下载同样禁止这些文件名。建议创建一个新的空目录，只让 tempserver 使用。

## 上传临时空间

Starlette 会将超过 1 MiB 的 multipart 文件放入临时文件。本项目把该临时目录改到：

```text
FILE_STORAGE_DIR/.tempserver-data/multipart
```

因此不会使用系统盘 `%TEMP%`。上传成功前仍可能同时存在 multipart 临时文件和内部写入临时文件，但占用被 `MAX_UPLOAD_REQUEST_BYTES` 限制。默认最坏情况约为单次请求大小的两倍，另加现有文件版本；请给文件盘预留空间。

上传采用内部唯一版本：已开始的下载继续读取旧版本，新请求立即读取新版本；删除后新请求立即返回 404。超过 24 小时且不再被引用的内部临时文件和旧版本会在启动时清理。

## 启动与健康检查

开发或临时运行可双击 `start.bat`，或在已激活虚拟环境的 PowerShell 中运行：

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

健康检查：

```text
GET http://127.0.0.1:8000/healthz
```

存储可用时返回 `200 {"status":"ok"}`，存储目录运行中消失时返回 503。首页会显示中文维护提示，而不是返回未处理的 500。

正式公网部署不要直接把 Uvicorn 暴露到互联网。应使用 HTTPS 反向代理，将请求转发到 `127.0.0.1:8000`，强制 HTTP 跳转 HTTPS，并保持 `SESSION_COOKIE_SECURE=true`。反向代理还应配置连接数、请求速率和请求体大小限制，且请求体上限不得高于应用的 `MAX_UPLOAD_REQUEST_BYTES`。

仅在本机使用纯 HTTP 调试管理员登录时，可临时设置 `SESSION_COOKIE_SECURE=false`；公网配置必须改回 `true`。

当前存储清理和服务端会话按单进程设计，请只启动一个 Uvicorn worker。Windows 长期运行方案见 [deploy/windows-service.md](deploy/windows-service.md)。

## 管理员会话

管理员 Cookie 包含签名后的随机会话 ID，服务端内存中保存有效会话。退出登录会撤销该 ID，复制退出前 Cookie 也不能再次访问管理页。服务重启会让所有管理员会话失效，需要重新登录。

`SESSION_MAX_AGE_SECONDS` 默认 8 小时。若怀疑 `SESSION_SECRET` 泄露，应立即更换密钥并重启服务。

登录失败默认按直接连接 IP 限制为 5 次/300 秒。若前方存在反向代理，应用看到的可能是代理 IP，因此公网反向代理仍应单独对 `/admin/login` 做限流。

## 页面规模

页面不提供搜索和分页，因此 `MAX_PUBLIC_FILES` 默认限制为 500。管理员上传新文件达到上限后会被拒绝；同名覆盖不增加数量。手工向存储目录放入超额文件不受上传检查保护，页面只保证按配置上限输出，禁止把该目录当作海量文件仓库。

## 测试

```powershell
python -m pip install -r requirements-dev.lock.txt
python -m pytest
python -m compileall -q app tests
```

GitHub Actions 在 Windows 上验证 Python 3.11 和 3.13。
