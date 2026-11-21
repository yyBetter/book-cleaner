@echo off
chcp 65001 >nul
cd /d "%~dp0.."

set "UV_PYTHON_INSTALL_MIRROR=https://registry.npmmirror.com/-/binary/python-build-standalone"
set "UV_HTTP_TIMEOUT=120"
set "UV_LINK_MODE=copy"

where uv >nul 2>nul
if not errorlevel 1 goto :uv_ok
echo [错误] 未检测到 uv 包管理器，请先安装 uv：
echo   pip install -U uv -i https://pypi.tuna.tsinghua.edu.cn/simple
pause
exit /b 1

:uv_ok
if not exist ".venv\Scripts\book-cleaner.exe" goto :sync_mirrors
echo [1/2] 检测到本地环境，尝试离线快速校验...
uv sync --no-dev --offline >nul 2>nul
if not errorlevel 1 goto :build_exe

:sync_mirrors
echo [1/2] 通过国内镜像同步轻量化基础依赖（含内置 RapidOCR，不含 DeepSeek-OCR 2 重型包）...
uv sync --no-dev --default-index https://pypi.tuna.tsinghua.edu.cn/simple
if not errorlevel 1 goto :build_exe

echo [提示] 清华源不可用，切换阿里云镜像...
uv sync --no-dev --default-index https://mirrors.aliyun.com/pypi/simple/
if not errorlevel 1 goto :build_exe

echo [提示] 阿里云不可用，切换中科大镜像...
uv sync --no-dev --default-index https://mirrors.ustc.edu.cn/pypi/simple/
if not errorlevel 1 goto :build_exe

echo [错误] 三个国内镜像均同步失败，请检查网络或代理设置。
pause
exit /b 1

:build_exe
echo [2/2] 使用 PyInstaller 打包绿色免安装目录 dist\book-cleaner\ ...
uv run --no-sync --default-index https://pypi.tuna.tsinghua.edu.cn/simple --with pyinstaller pyinstaller --noconfirm --onedir ^
  --name book-cleaner ^
  --collect-all rapidocr_onnxruntime ^
  --add-data "book_cleaner\web;book_cleaner\web" ^
  book_cleaner\cli.py
if not errorlevel 1 goto :build_done

echo [错误] PyInstaller 打包失败。
pause
exit /b 1

:build_done
echo 打包完成！请将 dist\book-cleaner 整个文件夹复制到目标 Windows 电脑即可离线运行。
echo 注：若需使用 DeepSeek-OCR 2 本地包，目标电脑可在网页「OCR 引擎与大模型设置」按需检测并下载到 data\addons\。
pause
