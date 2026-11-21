@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "UV_PYTHON_INSTALL_MIRROR=https://registry.npmmirror.com/-/binary/python-build-standalone"
set "UV_HTTP_TIMEOUT=120"
set "UV_LINK_MODE=copy"

where uv >nul 2>nul
if not errorlevel 1 goto :uv_ok
echo [错误] 未检测到 uv 包管理器。
echo 方案 1（国内推荐，若电脑已装 Python）：
echo   pip install -U uv -i https://pypi.tuna.tsinghua.edu.cn/simple
echo 方案 2（PowerShell 安装）：
echo   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
pause
exit /b 1

:uv_ok
if not exist ".venv\Scripts\book-cleaner.exe" goto :sync_mirrors
echo [1/3] 检测到本地环境，尝试离线快速校验...
uv sync --no-dev --offline >nul 2>nul
if not errorlevel 1 goto :env_ready

:sync_mirrors
echo [1/3] 正在通过国内镜像同步轻量化运行环境（约 125 MB，自动跳过 dev 包）...
echo 尝试镜像 1/3：清华源 (pypi.tuna.tsinghua.edu.cn)...
uv sync --no-dev --default-index https://pypi.tuna.tsinghua.edu.cn/simple
if not errorlevel 1 goto :env_ready

echo [提示] 清华源不可用，切换镜像 2/3：阿里云 (mirrors.aliyun.com)...
uv sync --no-dev --default-index https://mirrors.aliyun.com/pypi/simple/
if not errorlevel 1 goto :env_ready

echo [提示] 阿里云不可用，切换镜像 3/3：中科大 (mirrors.ustc.edu.cn)...
uv sync --no-dev --default-index https://mirrors.ustc.edu.cn/pypi/simple/
if not errorlevel 1 goto :env_ready

echo.
echo [错误] 三个国内镜像均安装失败（常见原因：DNS 异常、代理失效或断网）。
echo 请按顺序排查：
echo   1. 检查 Wi-Fi / 网线是否能正常打开国内网页；
echo   2. 若开启了 VPN/代理导致 DNS 解析失败 (os error 11001)，请关闭代理或切换节点后重试；
echo   3. 可在 CMD 执行 ipconfig /flushdns 后重新双击本脚本。
pause
exit /b 1

:env_ready
echo [2/3] 正在启动 HTTPS 工作台（供 Android 手机 Wi-Fi 解锁实时压书自动抓拍）...
echo 提示：手机首次访问如遇浏览器「非私密连接」提示，点「高级 - 继续前往」即可。
start "" "https://127.0.0.1:8765/"

echo [3/3] 启动 Book Cleaner HTTPS 服务...
uv run --no-sync book-cleaner serve --host 0.0.0.0 --port 8765 --auto-https
pause
