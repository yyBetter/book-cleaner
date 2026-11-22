# 超级扫描仪 · SuperScanner（本地离线纸质资料与多表批次数字化工作台）

> **一台电脑 + 外接高拍仪/摄像头（或 Android 手机），跑通十万页级「1 拍照 ➔ 2 复核 ➔ 3 表格 ➔ 4 导出」四步闭环。默认锁定 100% 本地涉密离线运行 + Ed25519 纯离线机器码授权，内置三大商业多表模板包，导出多 Sheet Excel (.xlsx) / CatBoost 特征宽表 / 可搜索 PDF / Markdown / EPUB 3 / RAG JSONL。**

> [!NOTE]
> **项目当前状态（阶段收尾归档 · `v0.1.0-closeout`）**：全流程功能、商业化打包与自动化测试均已闭环沉淀；因当前主力办公电脑（16 GB 内存、无大显存独立显卡）在本地离线运行高精度视觉大模型时算力受限，现暂作阶段性收尾归档，待后续 GPU / 算力条件具备后随时按指南恢复重启。详细功能清单、硬件复盘与重启指引见 **[docs/CLOSEOUT_AND_RESUME.md](docs/CLOSEOUT_AND_RESUME.md)**。

## 核心特性

1. **采集与光学前处理彻底解耦（两段式流水线）**
   - 翻拍瞬间仅运行 **L0 毫秒级质检**（分辨率无关 Laplacian 清晰度、亮度曝光、dHash 连续重复帧告警），立刻原子落盘写入 SQLite WAL 队列，**绝不阻塞操作员翻下一页**。
   - 针对**厚书无法摊平、也不用脚踏开关**的场景，内置 **「压书手辅助全自动抓拍」**：双手大拇指压住左右 16% 压书区不触发误拍；翻页手跨入中间正文区自动锁止；手撤回边缘稳住即自动挑出最清晰帧抓拍，并在后台擦除边缘手指。
   - 原生适配 **Windows 电脑（DirectShow 高拍仪 + 中文路径安全读写 + 托盘启动与一键批处理启动）** 与 **Android 手机（USB ADB 极速直连 / Wi-Fi `--auto-https` 实时连拍，`ImageCapture.takePhoto()` 浏览器内存直传电脑，不经过手机相册与本地存储 + 补光灯 + 防休眠）**。
2. **十万页级断点续传存储与 4 页签统一光学复核台**
   - `SQLite WAL` 状态机 + 两位十六进制 SHA-256 分片目录（`storage/book_<id>/<xx>/`），断电重启自动回收卡死锁状态并无损续跑；支持本地扫描目录与多页 PDF 批量入库去重及胶片条/多选/异常废片秒删。
   - **L1（本地 OpenCV）**：双页自动拆单页（L/R）、文档切边透视矫正、`±20°` 纠偏、**离线 `0°/90°/180°/270°` 四向自动扶正 + 手动 `90°` 旋转重识**、书页弯曲展平、边缘手指擦除与阴影泛黄漂白（洁净彩页 / 锐利灰度 / 黑白二值）。
   - **统一 `2 复核` 坐标锚定对照**：左侧支持原图/成片/拉帘对照与滚轮缩放，右侧自动还原 **GFM 标准 Markdown 管道符表格**并支持渲染/源码编辑切换；悬停或点击右侧段落与表格行时，左侧纸面原位自动框选高亮对应坐标块（支持 OCR 回退/异常显性告警与加载进度提示）。
3. **涉密严格离线锁 + 6 档离线 OCR 模型中心（L2 / L3）**
   - 默认开启 **`strict_offline=true` 涉密严格离线模式**（自动拦截一切非 `127.0.0.1 / localhost` 外发请求，且本地 Ollama 请求默认绕过系统代理）。
   - 右上角 **「OCR 设置」** 集成硬件自适应体检（含 4/8/16 GB 核显保留内存容差）与 6 档离线引擎一键切换/断点下载管理：**`RapidOCR (PP-OCRv4)` 内置包**、**`PP-OCRv5` 轻量包（~21 MB）与高精度包（~165 MB）**、**本机 Ollama `GLM-OCR 0.9B` / `Qwen2.5-VL 3B`**、**`DeepSeek-OCR 2` 旗舰 CUDA 本地隔离包**，并支持 `hybrid_smart` 混合升档与 `300s` 默认长超时；非涉密场景可关闭离线锁接入外部 Vision API 与可选 LLM 精校。
4. **多 Sheet 模板包、可校验数据清洗与五格式导出（`3 表格` & `4 导出`）**
   - **三栏可视化分拣台**：内置三大商业多表模板包（多表批次与多目标 CatBoost / 实验室记录 / 财务凭证台账），支持上传自有 `.xlsx` 导入多 Sheet 表头、页面自动匹配与别名学习。
   - **可校验稽核与导出前 PII 脱敏**：支持字段级物理上下限与 `>5` 倍突变标黄告警，以及跨格式一致的敏感词/PII 脱敏掩码。
   - **五格式一键导出**：**多 Sheet 成品 Excel（`.xlsx`，含业务子表、`异常待复核`、`清洗汇总报告`、`CatBoost_特征宽表` 与 `CatBoost_字段契约`）**、**可搜索 PDF**、**Markdown (`.md`)**、**EPUB 3 (`.epub`，原生表格渲染)**、**RAG JSONL (`.jsonl`)**。

---

## 快速启动

### Windows 电脑（推荐，无需手装 pyenv/Python）
1. 拉取或下载本项目源码：
   ```powershell
   git clone https://github.com/yyBetter/book-cleaner.git
   cd book-cleaner
   ```
2. 首次安装 `uv`（若电脑已有 Python 3.8+，用国内清华源安装极快；无需单独折腾 `pyenv`）：
   ```powershell
   pip install -U uv -i https://pypi.tuna.tsinghua.edu.cn/simple
   ```
   （若本机完全没装过 Python，也可用官方安装命令：`powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`，之后 `uv` 会自动通过阿里云 `npmmirror` 镜像拉取 Python，免手动配置）
3. 双击启动：
   - [start_windows_https.bat](start_windows_https.bat)（推荐：自动启用 `--auto-https`，Android Wi-Fi 扫码解锁实时压书自动抓拍与手机 0 存储直传）
   - 或 [start_windows.bat](start_windows.bat)（USB 直连 Android 手机 / 高拍仪）
   - 启动脚本已内置 **清华源 ➔ 阿里云 ➔ 中科大三镜像自动切换 + 阿里云 Python CDN**，总运行依赖仅约 **125 MB**（国内宽带通常 **15–40 秒**完成），二次启动自动离线秒开（`<1s`）；若出现 DNS 报错 `os error 11001`，请关闭异常代理/VPN 或执行 `ipconfig /flushdns` 后重试。
   - 详细配置指南见 [docs/android-windows-setup.md](docs/android-windows-setup.md)。

### macOS / Linux 命令行
```bash
cd book-cleaner
uv sync --no-dev
uv run --no-sync book-cleaner serve --host 0.0.0.0 --port 8765 --auto-https
```

启动后：
- 电脑端打开浏览器访问：`http://127.0.0.1:8765/`
- 点击顶部 **「导入演示样书 (7页)」** 可立即体验双页拆单、泛黄阴影漂白、弯曲展平、手指擦除及模糊/重复告警全流程；
- 或在 CLI 运行批量命令：
  ```bash
  # 批量导入本地扫描图片或 PDF 目录
  uv run book-cleaner import-folder --book-title "我的藏书" --folder /path/to/scans --split-spread

  # 导出多 Sheet Excel / 可搜索 PDF / Markdown / EPUB / JSONL
  uv run book-cleaner export --book-id 1 --format xlsx
  ```

---

## 硬件与手机无线拍摄建议

### 推荐硬件工位配置（性价比高、速度最快）
- **固定支架 + 4K 定焦高拍仪 / USB 摄像头**：避免手持抖动和焦距反复拉风箱。
- **双侧 45° 漫反射补光灯**：从物理源头减少反光和书脊重影。
- **USB 脚踏开关（映射为空格或回车键）**：双手只负责翻书压平，踩脚踏板直接触发快门与蜂鸣提示。

### 手机无线 / USB 零存储直传说明
- **推荐：Android USB 直连（`127.0.0.1`）或 Wi-Fi `--auto-https`**：手机 Chrome 打开 `/mobile` 后，直接调用 `ImageCapture.takePhoto()` / 实时视频帧在浏览器内存中抓拍并经网络直传电脑硬盘落盘，**完全绕过手机相册/存储（0 占用）**，支持压书手自动避让抓拍、常亮补光灯与屏幕防休眠。
- **普通 HTTP 模式**：手机浏览器如无法进入安全上下文，会自动提供系统原生相机拍照备用入口；若需指定外部证书也可传入：
  ```bash
  uv run book-cleaner serve --ssl-certfile cert.pem --ssl-keyfile key.pem
  ```

---

## 商业发版、打包与离线授权指南

- **打包干净源码发给 Windows 电脑（约 1 MB，自动排除 Mac 虚拟环境与数据）**：
  ```bash
  bash scripts/pack_for_windows.sh
  ```
- **一键构建 Windows 免配置托盘便携包与安装程序**：
  ```powershell
  powershell -ExecutionPolicy Bypass -File scripts\build_windows_release.ps1 -Version 0.2.0
  ```
  完整发版流程、Ed25519 私钥生成与给买家签发离线激活码教程见 [docs/RELEASE_WINDOWS.md](docs/RELEASE_WINDOWS.md)，商品化卖点与平台话术见 [docs/SALES_KIT.md](docs/SALES_KIT.md)。
- **轻量 CLI 一键启动托盘模式**：
  ```bash
  uv run --no-sync book-cleaner desktop
  ```

## 桌面端打包与自测

- **测试与静态检查**：
  ```bash
  make test
  make check-js
  ```
- **打包为独立桌面 App（Tauri / PyInstaller）**：详见 [docs/desktop-app-packaging.md](docs/desktop-app-packaging.md)。
