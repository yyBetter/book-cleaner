@echo off
chcp 65001 >nul
cd /d "%~dp0.."

echo [1/2] 同步轻量化基础依赖（含内置 RapidOCR，不含 DeepSeek-OCR 2 重型包）...
uv sync --python 3.12
if errorlevel 1 (
    echo [错误] 依赖同步失败。
    pause
    exit /b 1
)

echo [2/2] 使用 PyInstaller 打包绿色免安装目录 dist\book-cleaner\ ...
uv run --with pyinstaller pyinstaller --noconfirm --onedir ^
  --name book-cleaner ^
  --collect-all rapidocr_onnxruntime ^
  --add-data "book_cleaner\web;book_cleaner\web" ^
  book_cleaner\cli.py

if errorlevel 1 (
    echo [错误] PyInstaller 打包失败。
    pause
    exit /b 1
)

echo 打包完成！请将 dist\book-cleaner 整个文件夹复制到目标 Windows 电脑即可离线运行。
echo 注：若需使用 DeepSeek-OCR 2 本地包，目标电脑可在网页「OCR 引擎与大模型设置」按需检测并下载到 data\addons\。
pause
