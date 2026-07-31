# BTC 期权卖方系统

## 一键启动（推荐）

在项目根目录打开 PowerShell，只需要输入：

```powershell
.\start.ps1
```

脚本会先安全停止属于本项目的旧前后端，确认端口释放后重新启动服务；
后端 `/status` 与前端网页均检查成功后，才会自动打开浏览器。

如果只想启动和检查、不自动打开浏览器：

```powershell
.\start.ps1 -NoBrowser
```

## 手动启动

首次安装：

```powershell
python -m pip install -r web/backend/requirements.txt
cd web/frontend
npm install
```

终端一，启动后端：

```powershell
cd "C:\Users\jianh\Desktop\BTC期权卖方策略优化\web\backend"
python server.py
```

终端二，启动前端：

```powershell
cd "C:\Users\jianh\Desktop\BTC期权卖方策略优化\web\frontend"
npm run dev
```

然后访问 <http://127.0.0.1:5173>。Vite 会把 API 请求代理到
`http://127.0.0.1:8300`。

管理页面不要求访问口令。TradingView webhook 继续使用独立的
`WEBHOOK_PASSPHRASE` 验证信号来源。

系统采用单一策略权威：TradingView Pine 负责产生开仓/平仓订单信号，
Python 后端只做鉴权、去重、执行安全校验、Deribit 选腿与下单。打开监控台
顶部“TV 接入”页，即可复制 Webhook URL 和完整 Alert JSON 到 TradingView。
每套 TradingView Alert 填写便于识别的 `source_name`，页面会自动生成稳定的
`source_id`；“策略信号”页会按来源汇总开仓/退出信号、执行结果和关联仓位，
不在后端重复计算任何 Pine 策略指标。Pine 的 `strategy("…")` 只是在
TradingView 中显示的脚本标题，不需要与 `source_name` 完全相同。

V2 Pine 提供两个独立版本：

- `pine/btc_credit_spread_v2_chart.pine`：`BTC期权卖方V2·图表周期版`，
  推荐放在 3 分钟图，每根已收盘图表 K 线判断一次；
- `pine/btc_credit_spread_v2_2h.pine`：`BTC期权卖方V2·固定2小时版`，
  始终使用已收盘的 2 小时 BTC K 线判断。

两版都会在主图标出 `PUT开`、`CALL开` 与 `平仓`，DVOL 档位和退出仍固定
使用已收盘的 12 小时 DVOL 数据。每个版本应建立自己的 TradingView Alert，
并使用不同的 `source_id`，方便后台分别识别和管理。

策略权威边界：TradingView 独立完成 DVOL/HV/CCI/布林判断；后端拉取的
Deribit DVOL 仅用于只读诊断，不参与 V2 信号的批准、拒绝或补发。Pine
主图的 `DVOL数据` 必须达到 `就绪 1460/1460` 才可能开仓，历史不足时失败关闭。

只有修改后端代码并需要自动重新加载时，才使用
`python server.py --reload`。

## 生产式启动

```powershell
cd web/frontend
npm run build
cd ../backend
python server.py
```

构建产物写入 `web/backend/static/`，此时只需访问
<http://127.0.0.1:8300>，不需要单独运行 Vite。

策略配置、TradingView Alert 和上线说明见
[`docs/运行手册.md`](docs/运行手册.md)。
