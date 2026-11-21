@echo off
chcp 65001 >nul
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 uv 包管理器。
    echo 请先在 PowerShell 中运行以下命令安装 uv 后重新双击本脚本：
    echo powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    pause
    exit /b 1
)

echo [1/3] 正在检查并同步 Python 3.12 运行环境...
uv sync --python 3.12
if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络连接。
    pause
    exit /b 1
)

echo [2/3] 正在打开桌面工作台...
start "" "http://127.0.0.1:8765/"

echo [3/3] 启动 Book Cleaner 后台服务（关闭此窗口即停止服务）...
uv run book-cleaner serve --host 0.0.0.0 --port 8765
pause
