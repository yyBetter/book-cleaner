# Book Cleaner · 纸质书籍智能采集与清洗工坊

> **一台电脑 + 外接高拍仪/摄像头（或 Android 手机），跑通十万页级「采集质检 ➔ 流水线图像净化与离线 OCR ➔ 对比抽检复核 ➔ 数据清洗与自定义表头映射 ➔ 多 Sheet Excel (.xlsx) / PDF / Markdown / EPUB 导出」五步闭环。**

## 核心特性

1. **采集与清洗彻底解耦（两段式流水线）**
   - 翻拍瞬间仅运行 **L0 毫秒级质检**（分辨率无关 Laplacian 清晰度、亮度曝光、dHash 连续重复帧告警），立刻原子落盘写入 SQLite WAL 队列，**绝不阻塞操作员翻下一页**。
   - 针对**厚书无法摊平、也不用脚踏开关**的场景，内置 **「压书手辅助全自动抓拍」**：双手大拇指压住左右 16% 蓝区不触发误拍；翻页手跨入中间正文区自动锁止；手撤回边缘稳住 `0.38s` 即自动挑出最清晰帧抓拍，并在后台擦除边缘手指。
   - 原生适配 **Windows 电脑（DirectShow 高拍仪 + 中文路径安全读写 + 一键批处理启动）** 与 **Android 手机（USB ADB 极速直连 / Wi-Fi `--auto-https` 实时连拍，`ImageCapture.takePhoto()` 浏览器内存直传电脑，不经过手机相册与本地存储 + 补光灯 + 防休眠）**。
2. **十万页级断点续传存储**
   - `SQLite WAL` 状态机 + 两位十六进制 SHA-256 分片目录（`storage/book_<id>/<xx>/`），断电重启自动回收卡死锁状态并无损续跑。
   - 支持本地存量扫描目录与多页 PDF 批量入库，自动去重。
3. **离线优先四级流水线 + DeepSeek-OCR 2 按需加载包**
   - **L1（本地 OpenCV）**：双页自动拆单页（L/R）、文档切边透视矫正、±12° 纠偏、书页弯曲展平、边缘手指/拇指擦除、书脊阴影与泛黄底色漂白（支持洁净彩页 / 锐利灰度 / Sauvola 黑白二值）。
   - **L2（多引擎 OCR 路由）**：
     - 基础包默认内置 **`RapidOCR`（PP-OCRv4，15MB，纯 CPU 零成本离线）**，保证基础安装包轻量不臃肿；
     - 在右上角 **「OCR 引擎与大模型设置」** 中，提供 **DeepSeek-OCR 2 本地按需加载包**：先自动检测目标电脑是否有 **NVIDIA CUDA 显卡（≥ 8 GB 显存）、≥ 16 GB 内存及 ≥ 12 GB 磁盘**，达标后才单独下载安装到隔离目录 `data/addons/deepseek_ocr2/`（绝不污染基础依赖）；
     - 不满足本地显卡条件时，仍可安心用内置 RapidOCR，或填写 **DeepSeek-OCR 2 视觉 API** 配合 **智能混合模式**（普通页 RapidOCR，低置信/复杂表格页自动升级 DeepSeek-OCR 2），且任意故障自动回退 RapidOCR。
   - **L3（可选 LLM 精校）**：默认关闭；支持按需仅对低置信度或疑似乱码页调用任意 OpenAI 兼容接口（OpenAI / DeepSeek / Ollama / 通义千问等），节省 80%+ 成本。
4. **数据清洗、表头映射与五格式导出**
   - **混合型数据清洗引擎**：自动全角转半角、日期统一 `YYYY-MM-DD`、剔除重复页眉页脚与乱码控制符、条目去重，并将书内正文条目与表格明细自动拆轨提取。
   - **可自定义 Excel 表头模板**：工作台「4. 数据清洗与表头映射」支持自由增删列（内置字段 / 正则提取 / 键值对）或直接上传现有 `.xlsx` 导入首行表头。
   - **五格式一键导出**：**多 Sheet 成品 Excel（`.xlsx`：正文结构化条目 + 提取表格明细 + 异常标黄待复核 + 汇总报告）**、**可搜索 PDF**、**Markdown (`.md`)**、**EPUB 3 (`.epub`)**、**RAG JSONL (`.jsonl`)**。

---

## 快速启动

### Windows 电脑（推荐）
1. 拉取或下载本项目源码：
   ```powershell
   git clone https://github.com/yyBetter/book-cleaner.git
   cd book-cleaner
   ```
2. 首次安装 `uv`（PowerShell 执行一次即可）：
   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```
3. 双击启动：
   - [start_windows_https.bat](start_windows_https.bat)（推荐：自动启用 `--auto-https`，Android Wi-Fi 扫码解锁实时压书自动抓拍与手机 0 存储直传）
   - 或 [start_windows.bat](start_windows.bat)（USB 直连 Android 手机 / 高拍仪）
   - 详细配置指南见 [docs/android-windows-setup.md](docs/android-windows-setup.md)。

### macOS / Linux 命令行
```bash
cd book-cleaner
uv sync --python 3.12
uv run book-cleaner serve --host 0.0.0.0 --port 8765 --auto-https
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

## 跨电脑迁移与打包

- **打包干净源码发给 Windows 电脑（约 1 MB，自动排除 Mac 虚拟环境与数据）**：
  ```bash
  bash scripts/pack_for_windows.sh
  ```
- **在 Windows 上打免安装离线 `.exe` 文件夹**（仅含轻量 `RapidOCR`，DeepSeek-OCR 2 仍保持按需检测下载）：
  双击运行 [scripts/build_windows_exe.bat](scripts/build_windows_exe.bat)，详见 [docs/desktop-app-packaging.md](docs/desktop-app-packaging.md)。

## 桌面端打包与自测

- **测试与静态检查**：
  ```bash
  make test
  make check-js
  ```
- **打包为独立桌面 App（Tauri / PyInstaller）**：详见 [docs/desktop-app-packaging.md](docs/desktop-app-packaging.md)。
