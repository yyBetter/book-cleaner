# Windows 商业发版打包与离线发码运维手册

本手册面向 **超级扫描仪 · SuperScanner** 的发行与交付人员，说明如何安全生成商户签名私钥、构建 Windows 免配置客户端（绿色便携版 + 安装包），以及在日常售卖中 10 秒为买家签发纯离线机器码激活凭证。

---

## 一、首次环境准备：生成并绑定商户 Ed25519 私钥

本软件采用 **Ed25519 非对称加密**，**客户端程序内部只嵌入 32 字节公钥**，商户私钥始终离线保存在您自己电脑的 `keys/` 目录中（`keys/`、`*.pem`、`*.key` 已被 `.gitignore` 严格忽略，严禁随客户端或公开仓库分发）。

在开发机仓库根目录执行：

```bash
uv run python scripts/issue_license.py keygen --write-public-key
```

执行后：
1. 生成商户签名私钥：`keys/vendor_ed25519.pem`（请务必复制一份到 U 盘或加密网盘冷备份！一旦丢失，此前公钥签发体系下将无法再给新机器码发码）。
2. 自动将对应的 Base64URL 公钥写入 [book_cleaner/licensing.py](../book_cleaner/licensing.py) 中的 `EMBEDDED_PUBLIC_KEY_B64`。
3. 随后再打包的 Windows 客户端与源码构建包，都将只认这把私钥签发出的离线激活码。

> 如需重测验证：在打包前跑一次 `uv run pytest -v`，确保全部单元测试通过。

---

## 二、Windows 一键打包（绿色便携版 + 标准安装版）

在 **Windows 10 / Windows 11 (64 位)** 电脑上：

1. 安装 `uv`（若未安装）：
   ```powershell
   pip install -U uv -i https://pypi.tuna.tsinghua.edu.cn/simple
   ```
2. （可选）如需生成 `.exe` 安装向导程序，提前安装 [Inno Setup 6](https://jrsoftware.org/isinfo.php)。如只发绿色便携压缩包可跳过此步。
3. 以 PowerShell 执行打包流水线：
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\build_windows_release.ps1 -Version 0.2.0
   ```
4. 等待约 2～4 分钟，产物会统一输出到 `dist\release\`：
   - `超级扫描仪_v0.2.0_Windows_绿色便携版.zip`：解压即用，内置 `portable.flag`，所有图片/数据库/导出 Excel 均保存在程序同目录 `data\` 下，拷到 U 盘即带走。
   - `超级扫描仪_v0.2.0_Windows_安装包.exe`（需 Inno Setup 6）：标准引导安装，自动创建桌面快捷方式，用户数据安全保存在 `%LOCALAPPDATA%\SuperScanner\data\`，重装或升级不丢数据。

### 买家运行体验说明
- 买家**无需安装 Python、Git 或命令行工具**。
- 双击 **「超级扫描仪.exe」** 后，右下角系统托盘常驻白色光圈图标，自动拉起本地 `127.0.0.1:8765` 并打开默认浏览器。
- 右键托盘图标支持：**打开工作台 / 打开手机扫码地址 / 查看数据目录 / 安全退出**。
- 如双击时软件已在后台运行，程序会自动唤醒工作台网页而不会重复启动端口冲突。

---

## 三、日常发货：为买家签发纯离线激活码

买家下载「免费体验版」安装运行后：
1. 买家可免费完整体验 **拍照采集、图像净化、OCR 识别、双栏复核、3 套商业多表分拣预览**；导出时体验版每次导出前 **5 页**。
2. 买家决定购买后，点击工作台右上角 **「免费体验版 · 激活」**，点击 **「一键复制」**，把 `SS-XXXX-XXXX-XXXX` 形式的本机机器码发给您。
3. 您在自己电脑上运行一条发码命令（全程离线，无需网络）：

   ```bash
   # 1. 签发个人专业买断版（永久有效，推荐写上平台订单号方便追溯）
   uv run python scripts/issue_license.py issue ^
     --machine-code SS-ABCD-EFGH-2345 ^
     --tier pro ^
     --note "闲鱼订单-20261122-001"

   # 2. 签发企业多表工位版（例如带 1 年有效期，如需永久则去掉 --expires）
   uv run python scripts/issue_license.py issue ^
     --machine-code SS-ABCD-EFGH-2345 ^
     --tier enterprise ^
     --expires 2027-12-31 ^
     --note "某某研究院实验室-工位1"
   ```

4. 命令会直接输出以 `SS1.` 开头的完整激活码以及一段**可直接复制发送给买家的标准发货话术**。
5. （可选）发货前您也可以自己用同一脚本校验一下：
   ```bash
   uv run python scripts/issue_license.py verify --machine-code SS-ABCD-EFGH-2345 --token "SS1.xxx.yyy"
   ```

---

## 四、买家常见售后与排障速查

| 现象 | 原因 | 一句话解答方案 |
| :--- | :--- | :--- |
| Windows Defender / 杀毒软件提示“未知发布者” | 独立个人开发者软件尚未购买高价 EV 代码签名证书 | 点「更多信息 ➔ 仍要运行」；便携版放非系统盘如 `D:\SuperScanner\` 更省心 |
| 粘贴激活码提示“与本机不匹配” | 买家重装系统、更换主板或复制机器码时少字符 | 让买家复制当前界面最新 `SS-XXXX-XXXX-XXXX` 机器码，按新机器码免费/按售后规则重签一码即可 |
| 导出显示只有前 5 页 | 尚未激活，处于免费体验版状态 | 点击右上角 **「免费体验版 · 激活」** 粘贴 `SS1.` 离线激活码即刻解锁全量导出 |
| 默认 8765 端口被其他软件占用 | 本机已有服务占用了 8765 | 启动器会自动顺延探测 `8766+` 空闲端口并打开对应浏览器地址，无需人工改配置 |
| 电脑重装或换机，历史扫描资料怎么迁移 | 数据全在本地文件夹 | 托盘右键点击 **「查看数据目录」**，把整个 `data\` 文件夹拷贝到新电脑对应数据目录即可无缝恢复 |
