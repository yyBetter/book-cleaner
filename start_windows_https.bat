@echo off
chcp 65001 >nul
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 uv 包管理器。
    echo 请先在 PowerShell 中运行以下命令安装 uv：
    echo powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    pause
    exit /b 1
)

echo [1/3] 正在同步 Python 3.12 运行环境...
uv sync --python 3.12
if errorlevel 1 (
    echo [错误] 依赖安装失败。
    pause
    exit /b 1
)

echo [2/3] 正在启动 HTTPS 工作台（供 Android 手机 Wi-Fi 解锁实时压书自动抓拍）...
echo 提示：手机首次访问如遇浏览器「非私密连接」提示，点「高级 - 继续前往」即可。
start "" "https://127.0.0.1:8765/"

echo [3/3] 启动 Book Cleaner HTTPS 服务...
uv run book-cleaner serve --host 0.0.0.0 --port 8765 --auto-https
pause
