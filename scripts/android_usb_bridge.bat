@echo off
chcp 65001 >nul

set "ADB_EXE="
where adb >nul 2>nul && set "ADB_EXE=adb"
if "%ADB_EXE%"=="" if exist "%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe" set "ADB_EXE=%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"
if "%ADB_EXE%"=="" if exist "C:\platform-tools\adb.exe" set "ADB_EXE=C:\platform-tools\adb.exe"

if "%ADB_EXE%"=="" (
    echo [错误] 未找到 adb.exe。
    echo 请安装 Android Platform-Tools，或直接在电脑端工作台点击「一键打通 USB 通道」。
    pause
    exit /b 1
)

echo 正在检查已连接的 Android 设备...
"%ADB_EXE%" devices

echo 正在打通 USB 本地通道 tcp:8765 -> tcp:8765 ...
"%ADB_EXE%" reverse tcp:8765 tcp:8765
if errorlevel 1 (
    echo [错误] 端口映射失败，请确认手机已开启「开发者选项 - USB 调试」并点击信任此电脑。
    pause
    exit /b 1
)

echo 正在手机端自动打开 Book Cleaner Android 拍摄页面...
"%ADB_EXE%" shell am start -a android.intent.action.VIEW -d "http://127.0.0.1:8765/mobile"
echo 完成！现在可直接在手机 Chrome 使用「压书手辅助全自动抓拍」。
pause
