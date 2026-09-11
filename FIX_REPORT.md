# 12306FairTicket v1.0.1 修复与验证说明

版本：1.0.1。日期：2026-09-11。源码基线：GitHub `Tuan-Space/12306FairTicket` 的 `main`，提交 `e3d482f6f8dd9223820dab50ab95585de30a11a3`。发布包包含 Windows 程序、运行依赖及构建信息。

## 修复内容

- **席别勾选**：原列表项使用零宽度 size hint，实际 Qt 测试复现 `visualItemRect` 宽度为 0。现使用有效单元宽度，修复两列换行，支持点击复选框、文字、项内空白和键盘空格切换；一次操作只发出一次变更。拖动只调整顺序，保留全部 10 个席别和勾选状态，并避免 Qt 原生拖动重复删除来源项。
- **铺位引导**：未选择卧铺时提示在上方勾选硬卧、软卧或高级软卧；“去选择卧铺席别”会滚动并聚焦列表。选好卧铺且数量合法后自动消除错误。取消卧铺后保留数量，可重新选择或点击“清空铺位偏好”。
- **多项输入**：乘车人与优先车次共用解析器，支持 `,，、;；`、换行、制表符及其混用；忽略空项和首尾空白，保留姓名内部空格、顺序和重复项。重复乘车人仍报错，车次转为大写。界面读取、实时校验、配置导入保存及后端构建使用相同字符串拆分规则，兼容 JSON v1/v2。
- **构建**：显式收集 Conda 基础 Python 环境的 OpenSSL DLL，避免 PyInstaller 从其他软件的 PATH 目录取同名文件。普通 Python 安装仍使用原有收集流程。

## 验证结果

- 完整测试：**190 passed，11 subtests passed**。包含鼠标/键盘点击、两列缩放、席别排序、三种卧铺及座卧混选、提示跳转、偏好清空、各种分隔符、人数/重复/车次错误、配置保存导入，以及原有 CLI、订单流程模拟测试。
- 修复测试窗口的销毁清理，避免主题回归反复处理已关闭窗口；未改变应用关闭行为。
- 深浅主题在 Qt 离屏渲染的 100% 和 150% 缩放下验证，并检查 Windows 原生平台渲染。测试截图使用虚构乘车人和模拟配置。
- Windows 便携版通过构建脚本的启动检查。另在 `PATH` 仅含 Windows System32、独立配置目录、不同工作目录的子进程中验证深浅主题启动和截图；用便携包内 `_ssl.pyd`、OpenSSL DLL 与 certifi 证书创建 TLS 上下文，全程不访问网络。
- **验证范围**：拖放回归替代了 `QDrag.exec` 的系统循环，实际验证后续 Qt 事件、MIME 数据和列表重排；未完成 Windows OLE 原生鼠标拖放的端到端验证。测试没有登录 12306 或提交真实订单。

详细日志和截图位于源码目录的 `artifacts`：`test-results.txt`、`test-results.xml`、`build-windows.txt`、`build-dependencies.txt`、`visual-*`、`portable-check`。

## 运行与构建

便携版：打开 `dist\12306FairTicket\12306FairTicket.exe`。复制或解压时保留整个目录，尤其是旁边的 `_internal` 文件夹。

源码运行：

```powershell
.\.venv\Scripts\python.exe gui.py
```

本地验证和构建：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\scripts\build_windows.ps1 -Python ".\.venv\Scripts\python.exe" -SkipTests
```

环境为 Windows 11 x64、Python 3.12.12、PySide6 6.8.3、PyInstaller 6.22.2。准确依赖版本见 `artifacts\build-dependencies.txt`。
