# 桌面 App 套壳打包指引（Tauri 2.0 Sidecar / PyInstaller）

`book-cleaner` 的前后端与十万页 SQLite WAL 状态机已天然解耦，若需分发给多名工位操作员双击安装使用，无需重写任何业务逻辑，仅需两步即可封装为原生桌面 App（macOS `.app/.dmg` 或 Windows `.exe/.msi`）。

---

## 第一步：将 Python 引擎打包为单文件 Sidecar 可执行程序

```bash
cd book-cleaner
uv add --dev pyinstaller
# macOS / Linux 使用冒号分隔路径；Windows 请改用分号并加 --collect-all rapidocr_onnxruntime：
#   --collect-all rapidocr_onnxruntime --add-data "book_cleaner\web;book_cleaner\web"
uv run pyinstaller --noconfirm --onefile \
  --name book-cleaner-engine \
  --collect-all rapidocr_onnxruntime \
  --add-data "book_cleaner/web:book_cleaner/web" \
  book_cleaner/cli.py
```

验证打包产物独立运行正常：

```bash
./dist/book-cleaner-engine serve --host 127.0.0.1 --port 8765 --data-dir ~/BookCleanerData
curl http://127.0.0.1:8765/api/health
```

---

## 第二步：接入 Tauri 2.0 桌面外壳

1. 将 `dist/book-cleaner-engine`（按 Tauri target-triple 命名，如 `book-cleaner-engine-aarch64-apple-darwin`）放入 `src-tauri/binaries/`。
2. 在 `tauri.conf.json` 中声明 `externalBin: ["binaries/book-cleaner-engine"]`。
3. Rust `setup` 钩子启动 Sidecar：
   - 传入参数 `serve --host 127.0.0.1 --port <free_port> --data-dir <app_data_dir>`（如需局域网手机配对则将 `--host` 改为 `0.0.0.0`）。
   - 轮询 `GET http://127.0.0.1:<free_port>/api/health` 返回 `{"ok": true}` 后，将桌面 WebView 窗口导航至该本地地址。
4. 窗口关闭事件（`RunEvent::Exit`）向 Sidecar 发送终止信号，确保 SQLite WAL 与相机句柄安全释放。
