<#
.SYNOPSIS
    超级扫描仪 · SuperScanner Windows 一键免配置桌面端与安装包构建脚本
.DESCRIPTION
    1. 通过国内镜像校验/安装运行时依赖（含 desktop 托盘与 PyInstaller）；
    2. 生成多分辨率桌面应用图标；
    3. 使用 PyInstaller 打包免配置托盘桌面版（dist\超级扫描仪\超级扫描仪.exe）；
    4. 生成绿色免安装便携压缩包（自带 portable.flag，数据就地保存）；
    5. 如本机已安装 Inno Setup 6（ISCC.exe），自动编译标准 Windows 安装程序 (.exe)。
#>

param(
    [string]$Version = "0.2.0",
    [switch]$SkipInstaller,
    [switch]$SkipSync
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

$env:UV_PYTHON_INSTALL_MIRROR = "https://registry.npmmirror.com/-/binary/python-build-standalone"
$env:UV_HTTP_TIMEOUT = "120"
$env:UV_LINK_MODE = "copy"

Write-Host "=== 超级扫描仪 · SuperScanner Windows 商业发行打包流水线 v$Version ===" -ForegroundColor Cyan

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "未检测到 uv，请先执行: pip install -U uv -i https://pypi.tuna.tsinghua.edu.cn/simple"
    exit 1
}

if (-not $SkipSync) {
    Write-Host "[1/5] 同步基础依赖与桌面托盘组件 (pystray)..." -ForegroundColor Yellow
    $synced = $false
    foreach ($idx in @(
        "https://pypi.tuna.tsinghua.edu.cn/simple",
        "https://mirrors.aliyun.com/pypi/simple/",
        "https://mirrors.ustc.edu.cn/pypi/simple/"
    )) {
        uv sync --no-dev --extra desktop --default-index $idx
        if ($LASTEXITCODE -eq 0) {
            $synced = $true
            break
        }
    }
    if (-not $synced) {
        Write-Error "依赖同步失败，请检查网络连接或代理配置。"
        exit 1
    }
} else {
    Write-Host "[1/5] 跳过依赖同步 (-SkipSync)" -ForegroundColor DarkGray
}

$BuildDir = Join-Path $RepoRoot "build"
$IconPath = Join-Path $BuildDir "superscanner.ico"
New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null

Write-Host "[2/5] 生成桌面端与应用托盘图标 -> $IconPath" -ForegroundColor Yellow
uv run --no-sync python -m book_cleaner.desktop_launcher --export-icon "$IconPath"
if ($LASTEXITCODE -ne 0) {
    Write-Error "生成应用图标失败。"
    exit 1
}

Write-Host "[3/5] 使用 PyInstaller 打包「超级扫描仪.exe」免黑框托盘桌面版..." -ForegroundColor Yellow
$DistAppDir = Join-Path $RepoRoot "dist\超级扫描仪"
if (Test-Path $DistAppDir) {
    Remove-Item -Recurse -Force $DistAppDir
}

uv run --no-sync --default-index https://pypi.tuna.tsinghua.edu.cn/simple --with pyinstaller pyinstaller `
    --noconfirm `
    --onedir `
    --noconsole `
    --name "超级扫描仪" `
    --icon "$IconPath" `
    --collect-all rapidocr_onnxruntime `
    --hidden-import pystray._win32 `
    --add-data "book_cleaner\web;book_cleaner\web" `
    book_cleaner\desktop_launcher.py

if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller 打包失败。"
    exit 1
}

Write-Host "[4/5] 组装绿色免安装便携版与说明文件..." -ForegroundColor Yellow
$ReleaseRoot = Join-Path $RepoRoot "dist\release"
$PortableDir = Join-Path $ReleaseRoot "超级扫描仪_v${Version}_Windows_绿色便携版"
$PortableZip = Join-Path $ReleaseRoot "超级扫描仪_v${Version}_Windows_绿色便携版.zip"

New-Item -ItemType Directory -Force -Path $ReleaseRoot | Out-Null
if (Test-Path $PortableDir) { Remove-Item -Recurse -Force $PortableDir }
if (Test-Path $PortableZip) { Remove-Item -Force $PortableZip }

Copy-Item -Recurse -Force $DistAppDir $PortableDir
New-Item -ItemType File -Force -Path (Join-Path $PortableDir "portable.flag") | Out-Null

$QuickGuide = @"
超级扫描仪 · SuperScanner v$Version（Windows 绿色免安装便携版）
================================================================

1. 启动方式
   直接双击「超级扫描仪.exe」，无需安装 Python 或 Git。
   软件会自动在右下角系统托盘常驻，并打开浏览器工作台：http://127.0.0.1:8765/

2. 系统托盘右键菜单
   - 打开工作台：重新唤起主操作台网页
   - 打开手机扫码地址：查看局域网配对页面（或使用工作台内二维码）
   - 查看数据目录：打开本便携版同目录下的 data\（包含数据库与导出文件）
   - 安全退出：优雅停止后台识别线程与服务

3. 离线激活授权
   点击工作台右上角「免费体验版 · 激活」，复制您的本机机器码（SS-XXXX-XXXX-XXXX），
   获取离线激活码并粘贴即可永久断网解锁不限页导出与多目标 CatBoost 全量导出。

4. 数据完全本地存储
   所有图片、识别文本、Excel 导出文件与授权状态均保存在本目录 data\ 下，100% 断网可用。
"@
Set-Content -Path (Join-Path $PortableDir "使用说明.txt") -Value $QuickGuide -Encoding UTF8

Compress-Archive -Path $PortableDir -DestinationPath $PortableZip -Force
Write-Host "已生成便携版压缩包: $PortableZip" -ForegroundColor Green

if (-not $SkipInstaller) {
    $IsccCandidates = @(
        (Get-Command iscc -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    ) | Where-Object { $_ -and (Test-Path $_) }

    $IsccExe = $IsccCandidates | Select-Object -First 1
    if ($IsccExe) {
        Write-Host "[5/5] 检测到 Inno Setup，正在生成 Windows 标准安装程序 (.exe)..." -ForegroundColor Yellow
        $IssFile = Join-Path $RepoRoot "scripts\superscanner_installer.iss"
        & $IsccExe "/DMyAppVersion=$Version" "$IssFile"
        if ($LASTEXITCODE -ne 0) {
            Write-Error "Inno Setup 安装包构建失败。"
            exit 1
        }
        Write-Host "标准安装程序已输出到 dist\release\" -ForegroundColor Green
    } else {
        Write-Host "[5/5] 未检测到 Inno Setup 6 (ISCC.exe)，跳过 .exe 安装器构建（便携版 Zip 已就绪）。" -ForegroundColor DarkYellow
    }
} else {
    Write-Host "[5/5] 已按参数跳过安装器构建 (-SkipInstaller)。" -ForegroundColor DarkGray
}

Write-Host "=== Windows 发行构建全部完成！产物位于: $ReleaseRoot ===" -ForegroundColor Cyan
