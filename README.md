# 12306 Fair Ticket

一个面向 12306 开售场景的本地抢票辅助工具，提供 Windows 图形界面和命令行两种使用方式。

程序会在开售前完成服务器校时、扫码登录、站点与乘车人准备，并在目标时间附近按设定频率查询。发现符合条件的票后，可自动提交订单；支付仍需前往 12306 官方渠道完成。

> 本项目不能保证出票，不包含自动支付、验证码绕过、风控规避、自动取消重下或多任务并发。请仅用于本人购票，并遵守 12306 的服务规则。

## 图形界面

![12306 Fair Ticket 图形界面](assets/gui_overview.png)

主要功能：

- 二维码直接显示，包含扫码状态和有效期倒计时；
- 使用 12306 服务器时间校准开售倒计时；
- 站名自动补全、有效性检查和手动更新；
- 日历选日期，开始与停止时间可分别设置时、分、秒；
- 车次和席别优先级配置，席别支持拖动排序；
- 支持座位关系与上、中、下铺数量偏好；
- 基础参数与高级参数分区，错误字段会给出明确提示；
- 动态显示当前任务阶段，日志自动脱敏并异步刷新；
- 可以保存或导入全部可编辑参数的 JSON 配置；
- GUI 不保存 Cookie、二维码、Token、证件号或手机号。

## 下载与运行

前往仓库右侧的 **Releases**，下载：

```text
12306FairTicket-Windows-x64.zip
```

解压完整目录后运行：

```text
12306FairTicket.exe
```

不要只复制 EXE。当前版本采用 Windows `onedir` 便携目录，程序运行还需要同目录中的 `_internal` 文件夹。

首次使用建议先关闭“自动提交”，以“仅监控”方式完成扫码和参数检查。

## 座位与铺位偏好

位置设置属于**软偏好**：

- 二等座支持 `A/B/C/D/F`；
- 一等座支持 `A/C/D/F`；
- 特等座支持 `A/C/F`；
- 商务座根据 12306 返回的实际布局处理，未知布局只使用共同位置 `A/F`；
- 卧铺可按人数设置下铺、中铺和上铺数量。

所选位置数量必须为零或等于乘车人数。车型或接口不支持选座、选铺时，程序会清除位置偏好并继续提交，由 12306 随机分配；不会为了换位置自动取消订单或重复下单。

## 实测示例

下面是图形界面成功提交订单后的示例。它只说明流程可以完成，不代表任何车次都能成功出票。

![图形界面成功提交订单示例](assets/user_result.png)

看到订单号后，请立即前往 12306 App 或官方网站核对并完成支付。

## 从源码运行

需要 Python 3.12。

```powershell
git clone https://github.com/Tuan-Space/12306FairTicket.git
cd 12306FairTicket
python -m pip install -r requirements-gui.txt
python gui.py
```

命令行模式仍然兼容原来的 `config.py`：

```powershell
python -m pip install -r requirements.txt
python main.py --validate-config
python main.py
```

更完整的 GUI 参数、隐私边界和构建说明见 [GUI 使用说明](README_GUI.md)，执行流程见 [抢票原理说明](docs/抢票原理说明.md)。

## 配置与隐私

GUI 顶部的“保存为…”会生成 version 2 JSON，保存路线、时间、乘车人姓名、车次、席别、位置偏好和高级参数；“导入…”兼容 version 1 和 version 2。

配置文件不会保存：

- Cookie 或登录会话；
- 二维码、Token；
- 证件号码、手机号；
- 缓存内容和程序运行路径。

GUI 每次启动都重新扫码，同一次运行期间只在内存中复用登录会话。不要上传 `.runtime`、本地配置或日志中未经确认的个人信息。

## 性能说明

图形界面不会改变核心查询频率或开售时间：

- 查询、登录和下单协议运行在后台线程；
- 开售等待使用服务器校时和单调时钟；
- 界面定时器只负责显示倒计时；
- 高频日志与指标批量刷新，磁盘日志异步写入。

实际效果仍会受到网络质量、12306 服务端压力、余票数量和放票策略影响。

## 测试与构建

全部自动测试都使用模拟响应，不会提交真实订单：

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

构建 Windows 便携目录：

```powershell
.\scripts\build_windows.ps1 -Python ".\.venv\Scripts\python.exe"
```

输出位置：

```text
dist\12306FairTicket\12306FairTicket.exe
```

## 开源依赖

第三方组件与许可证信息见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
