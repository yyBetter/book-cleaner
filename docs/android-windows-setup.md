# Android 手机 + Windows 电脑搭配及「压书手全自动翻拍」指南

面向 **厚重精装书无法摊平、无需脚踏开关、采集端为 Android 手机、主机为 Windows 电脑** 的完整实战配置指南。

---

## 一、不用脚踏开关，双手按住书怎么自动拍？

厚书必须用手压平，不能指望“手彻底拿开”再拍。系统在 [camera.py](../book_cleaner/camera.py) 与 [smart_trigger.js](../book_cleaner/web/static/smart_trigger.js) 中内置了 **「分区感知压书辅助自动抓拍」**：

1. **左右 16% 压书安全区（蓝色半透明带）**
   - 把书页边缘对准左右两侧蓝色区域，**你的左右大拇指可以一直稳稳压住蓝区**，该区域的细微抖动和肤色**不会阻止抓拍**；
   - 拍完后后台 OpenCV `remove_edge_fingers` 会自动把左右蓝区压书的手指擦除还原为干净纸色。
2. **中间 68% 正文净空区（中心虚线框）**
   - 当你伸手进入中间正文区翻页时，系统检测到肤色遮挡正文（边框变橙色：`手在正文区`），**死锁不拍**；
   - 翻完页后手指退回左右蓝区并压稳约 `0.38s`（边框变绿并读条），系统从稳态连帧中**自动挑出最清晰的一帧**抓拍入库。
3. **防同页误连拍（dHash 去重门禁）**
   - 抓拍后自动记录正文区感知哈希；若手指稍微松动晃了一下、正文其实没翻页，系统直接显示 `页内容未变，已跳过重复拍摄`，绝不重复拍废片。

---

## 二、Windows 电脑端启动（1 分钟搞定，无需安装 pyenv）

1. **安装 `uv`（仅首次需要，无需手动安装 `pyenv` 或重装 Python）**：
   - 若电脑已有 Python，直接用国内清华源安装 `uv`（数秒完成）：
     ```powershell
     pip install -U uv -i https://pypi.tuna.tsinghua.edu.cn/simple
     ```
   - 若电脑未安装过 Python，在 PowerShell 运行：
     ```powershell
     powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
     ```
   - 项目已原生兼容 Python **3.11 – 3.13**，`uv` 会优先复用本机 Python，或自动从阿里云 `npmmirror` 镜像下载轻量运行环境。
2. **双击启动脚本**（已内置 **清华源 ➔ 阿里云 ➔ 中科大三镜像自动切换**，总依赖约 **125 MB**，国内网络通常 15–40 秒装完，之后每次运行自动离线秒开）：
   - **USB 有线直连手机 / 外接摄像头**：双击 [start_windows.bat](../start_windows.bat)
   - **Wi-Fi 无线连拍并开启实时自动抓拍**：双击 [start_windows_https.bat](../start_windows_https.bat)（自动生成局域网自签证书，无需手动装 OpenSSL）
3. **如遇 `os error 11001`（DNS 解析失败）或手机 Wi-Fi 连不上 Windows**：
   - 若提示 DNS 异常，先关闭异常代理/VPN 或在 CMD 执行 `ipconfig /flushdns` 后重试；
   - 若手机连不上电脑，以管理员身份在 PowerShell 执行一次 [setup_windows_firewall.ps1](../scripts/setup_windows_firewall.ps1) 放行内网 8765 端口（确保 Windows 网络设为「专用网络」）。

---

## 三、Android 手机连拍的两种接法（全程不落手机存储）

> **零存储直传链路：** `Android 摄像头传感器 ➔ 浏览器内存 Blob (ImageCapture.takePhoto() / 实时帧) ➔ USB / Wi-Fi 直传 Windows 硬盘落盘`。  
> 只要处于 **USB 直连（127.0.0.1）** 或 **Wi-Fi HTTPS（`--auto-https`）** 下，网页会自动隐藏系统文件上传框，照片全程只存在手机内存并立即经网络推送至电脑释放，**不写入手机相册 / DCIM，拍完 10 万页手机剩余空间依然 0 占用**。

### 方案 A：Android USB 数据线直连（强烈推荐 10 万页主力方案）
- **优势**：走 `adb reverse` 本地回环（`http://127.0.0.1:8765/mobile`），**无需 HTTPS 证书警告、不走 Wi-Fi 零卡顿、边拍边给手机供电**，且 Chrome 直接解锁 4K 实时取景 + 压书自动抓拍 + 常亮补光灯（Torch）+ 屏幕防休眠。
- **用法**：
  1. Android 手机开启 `设置 -> 关于手机 -> 连续点击版本号 -> 开发者选项 -> USB 调试`；
  2. USB 线插到 Windows 电脑（手机弹窗点「允许 USB 调试」）；
  3. 在电脑工作台「Android 手机无线 / USB 连拍」标签页点击 **「一键打通 USB 通道并在手机打开」**（或直接双击 [android_usb_bridge.bat](../scripts/android_usb_bridge.bat)）。

### 方案 B：Wi-Fi 局域网扫码连接（免数据线）
- **HTTPS 自动连拍**：电脑用 `start_windows_https.bat` 启动后，手机 Chrome 扫码打开 `https://<Win局域网IP>:8765/mobile`（首次点「高级 → 继续前往」信任本机局域网），即可启用与 USB 相同的实时取景 + 压书自动抓拍 + 补光灯。
- **普通 HTTP 模式**：手机扫码后使用 Android 原生相机大按钮直拍原图回传，不占用手机相册容量。
