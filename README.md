# BTC 期权卖方系统完整详解

> 本文以当前仓库代码和 `web/backend/config/base.yaml` 为准，面向系统使用者、策略审核者和运维人员，说明安装、TradingView 设置、选腿算量、自动/人工对冲、α/β 公式、平仓恢复及故障排查。
>
> 这是交易系统说明，不构成投资建议。当前系统应先在 Deribit Testnet 完整验证。标为“待审核”或“占位”的参数不能直接视为适合实盘。

## 1. 项目是什么

这是一个“TradingView 产生信号 + Python 后端安全执行 + Deribit 真实交易 + 网页监控”的 BTC 期权卖方系统。

职责边界：

```text
TradingView：判断为什么现在开仓或退出
后端：判断信号能否安全执行，选择合约、计算数量、下单并管理风险
Deribit：保存真实订单、成交、余额和持仓
```

后端展示的 DVOL/HV/CCI/布林结果仅供诊断，不能覆盖 TradingView 的策略判断。

系统支持：

- PUT/CALL 信用价差；
- TradingView Pine 信号接入；
- Deribit 期权双腿执行；
- BTC-PERPETUAL 动态 Delta 对冲；
- 单笔风险、组合风险、保证金、日亏损和行情异常风控；
- 正常平仓、子组平仓、到期结算、残余处理和全账户强平；
- SQLite 台账、恢复和网页监控。

## 2. 策略交易结构

### 2.1 PUT 信用价差

```text
卖出较高行权价 PUT
买入较低行权价 PUT 作为保护
```

适合策略判断 BTC 不会大跌的场景。组合通常为正 Delta，BTC 下跌时通常需要卖出 BTC-PERPETUAL 对冲。

### 2.2 CALL 信用价差

```text
卖出较低行权价 CALL
买入较高行权价 CALL 作为保护
```

适合策略判断 BTC 不会大涨的场景。组合通常为负 Delta，BTC 上涨时通常需要买入 BTC-PERPETUAL 对冲。

### 2.3 为什么不用裸卖

保护腿把尾部风险限制在价差宽度内。分腿执行始终遵守：

```text
先买保护腿 → 再卖收权腿
```

卖腿失败时系统尝试回滚已成交保护腿，避免留下裸卖。

## 3. 系统架构

```text
TradingView Pine
  ├─ btc_credit_spread_v2_chart.pine（跟随图表周期）
  └─ btc_credit_spread_v2_2h.pine（固定 2 小时）
          │ HTTPS Webhook
          ▼
FastAPI 后端 web/backend（默认 8300）
  ├─ 鉴权、格式标准化、幂等去重
  ├─ 账户和风险检查
  ├─ 选腿、算量、双腿执行与恢复
  ├─ 优先级命令队列
  ├─ 实时指数激活和周期扫描
  ├─ α/β 对冲状态机
  └─ SQLite 台账
          │ WebSocket / API
          ▼
Deribit Testnet 或 Mainnet

网页监控台
  └─ 开发模式 5173；生产构建后由 8300 提供
```

### 3.1 命令优先级

| 顺序 | 命令 | 作用 |
|---:|---|---|
| 1 | `FORCE_CLOSE` | 风险/人工强平 |
| 2 | `NORMAL_CLOSE`、`GROUP_CLOSE`、恢复 | 平仓与恢复 |
| 3 | `HEDGE_RELEASE` | 归零策略对冲 |
| 4 | `HEDGE_REBALANCE` | 调整永续对冲 |
| 5 | `SCAN` | 周期扫描，重复扫描合并 |
| 6 | `OPEN` | 新开仓最低优先级 |

单 worker 串行执行，防止开仓、平仓和对冲同时修改账户。

### 3.2 数据库

SQLite 位于 `web/backend/data/`。运行服务按环境和 Deribit Client ID 哈希生成账户隔离数据库。数据库、日志、`.env` 和执行报告都不会上传 GitHub。

## 4. 目录说明

| 路径 | 作用 |
|---|---|
| `pine/` | TradingView Pine v6 策略 |
| `web/backend/server.py` | 后端入口 |
| `web/backend/src/webhook.py` | TV 消息解析和标准化 |
| `web/backend/src/engine/` | 开仓、平仓、风控、扫描和对冲 |
| `web/backend/src/execution/` | Combo、分腿和 Maker/IOC 执行 |
| `web/backend/src/exchange/` | Deribit 行情、账户和永续接口 |
| `web/backend/src/store/` | SQLite schema 和读写层 |
| `web/backend/config/` | 策略、风险和环境配置 |
| `web/frontend/` | 监控台和前端测试 |
| `web/backend/scripts/` | 连通性、模拟信号和手动开仓 |
| `deploy/cloud/` | systemd/Caddy 部署文件 |
| `start.ps1` | Windows 一键启动 |

## 5. 安装与配置

### 5.1 要求

- PowerShell；
- Python 3.10+；
- Node.js/npm；
- Deribit Testnet 账户和 API Key；
- TradingView 账户；
- 生产 Webhook 使用的公网 HTTPS 域名。

### 5.2 安装依赖

```powershell
python -m pip install -r web/backend/requirements.txt
Set-Location web/frontend
npm install
Set-Location ../..
```

### 5.3 创建 `.env`

```powershell
Copy-Item web/backend/.env.example web/backend/.env
```

编辑：

```dotenv
DERIBIT_ENV=testnet
DERIBIT_TESTNET_CLIENT_ID=你的TestnetClientID
DERIBIT_TESTNET_CLIENT_SECRET=你的TestnetClientSecret
DERIBIT_MAINNET_CLIENT_ID=
DERIBIT_MAINNET_CLIENT_SECRET=
WEBHOOK_PASSPHRASE=足够长的随机字符串
TRADING_ACCOUNT_NAME=account_1
```

| 字段 | 含义 |
|---|---|
| `DERIBIT_ENV` | `testnet` 为测试网；`mainnet` 为实盘 |
| `*_CLIENT_ID/SECRET` | Deribit API 凭据，严禁提交 Git |
| `WEBHOOK_PASSPHRASE` | TradingView Webhook 独立口令 |
| `TRADING_ACCOUNT_NAME` | 账户标识，防止 Alert 发错账户 |

## 6. 启动系统

### 6.1 一键启动

```powershell
.\start.ps1
```

不自动打开浏览器：

```powershell
.\start.ps1 -NoBrowser
```

脚本验证依赖、停止属于本项目的旧进程、释放 8300/5173 端口、启动服务并检查健康状态。它不会杀掉无法确认归属的未知进程。

- 监控台：<http://127.0.0.1:5173>
- 后端状态：<http://127.0.0.1:8300/status>

### 6.2 手动开发启动

终端一：

```powershell
Set-Location web/backend
python server.py --reload
```

终端二：

```powershell
Set-Location web/frontend
npm run dev
```

### 6.3 单端口运行

```powershell
Set-Location web/frontend
npm run build
Set-Location ../backend
python server.py
```

前端构建到 `web/backend/static/`，之后访问 <http://127.0.0.1:8300>。

### 6.4 启动时后端做什么

1. 加载 `.env`、`base.yaml` 和环境覆盖；
2. 打开账户隔离数据库；
3. 连接并认证 Deribit；
4. 加载数据库中的运行时 α、β、复核周期、期限和两侧参数；
5. 检查 α/β 是否形成有效迟滞，无效则清除覆盖并回退默认；
6. 核对 SQLite 与交易所真实持仓；
7. 启动实时 BTC 指数监控、命令 worker 和扫描器。

## 7. 首次启动顺序

1. 确认页面环境是 `testnet`；
2. 核对权益、保证金、期权、永续和挂单；
3. 确认 `force_close_active=false`；
4. 确认扫描状态正常；
5. 运行：

   ```powershell
   Set-Location web/backend
   python scripts/check_connectivity.py
   ```

6. 完成 TradingView 设置；
7. 用 Testnet 小额验证；
8. 最后开启交易：

   ```powershell
   Invoke-RestMethod -Method Post `
     -Uri "http://127.0.0.1:8300/control/trading?enabled=true"
   ```

关闭新开仓：

```powershell
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8300/control/trading?enabled=false"
```

关闭交易开关只阻止新开仓，不会平已有仓位。

## 8. TradingView 策略原理

### 8.1 两个 Pine 版本

| 文件 | 信号周期 | 说明 |
|---|---|---|
| `btc_credit_spread_v2_chart.pine` | 跟随图表周期 | 推荐 3 分钟图；换周期会改变 HV/布林的实际时间跨度 |
| `btc_credit_spread_v2_2h.pine` | 固定已完成 2h K 线 | 主图周期变化不改变信号周期 |

两版 DVOL 都使用已完成的 12 小时数据。

### 8.2 DVOL 档位

默认：

```text
L1=40%，L2=60%，L3=85%
历史窗口=730天=1460根12h bar
```

| DVOL 历史分位 | tier | 行为 |
|---|---:|---|
| 样本不足 | -1 | 不开仓 |
| `< L1` | 0 | 不开仓 |
| `L1 ≤ 分位 < L2` | 1 | 进入后续判断 |
| `L2 ≤ 分位 < L3` | 2 | 进入后续判断 |
| `≥ L3` | 3 | 进入后续判断 |

Pine 状态表必须显示 `DVOL数据 就绪 1460/1460`。

### 8.3 HV 公式

```text
r_t = ln(Close_t / Close_(t-1))
HV = stdev(r, hv_len) × sqrt(一年包含的当前周期bar数) × 100
```

默认 `hv_len=20`，在最近 `252` 根信号周期 K 线中计算 HV 分位，并要求：

```text
HV分位 ≥ 30%
```

图表周期版换周期后，20/252 根 K 线对应的时间跨度也变化，需要重新回测。

### 8.4 CCI

```text
CCI周期=20
输入源=hlc3=(high+low+close)/3
要求 abs(CCI)<101
```

不能把输入源改成 `close`，阈值附近可能导致完全不同的判定。

### 8.5 布林形态

```text
中轨=SMA(close,20)
上轨=中轨+2×stdev(close,20)
下轨=中轨-2×stdev(close,20)
```

PUT：

```text
最低价<下轨 AND 收盘价>开盘价
```

CALL：

```text
最高价>上轨 AND 收盘价<开盘价
```

最终开仓需要：配置有效、DVOL 完整且 tier>0、HV 达标、CCI 达标、对应布林形态成立、当前 Pine 实例无虚拟持仓、K 线确认收盘。

### 8.6 DVOL 退出

默认退出窗口为 60 天，即 120 根 12h DVOL。当前完成 DVOL 低于该窗口的第 30 百分位时，Pine 产生退出订单。后端不会另算一套 DVOL 自动退出。

## 9. TradingView 逐步设置

### 9.1 加载两个独立实例

Pine 的 `trade_side` 只能选 `PUT` 或 `CALL`。把同一脚本添加到图表两次：

```text
实例A：trade_side=PUT
实例B：trade_side=CALL
```

分别创建 Alert。PUT 实例只产生 PUT 开/平仓，CALL 实例只产生 CALL 开/平仓。

注意：当前后端是**全账户单活动父交易**模式。已有任何活动信用价差时，另一条新开仓会被 `REJECT_ACTIVE_POSITION_EXISTS` 拒绝。两个 Alert 是两个候选方向，不代表账户能同时开两笔策略价差。

### 9.2 添加脚本

1. 打开 TradingView BTC 图表；
2. 打开 Pine Editor；
3. 粘贴仓库 Pine 源码；
4. 保存并添加两份到图表；
5. 一份设 PUT，另一份设 CALL；
6. 核对 DVOL/HV/CCI/布林和退出参数；
7. 等待 DVOL 显示 `1460/1460`。

### 9.3 生成 Alert 内容

1. 打开监控台 **TV 接入**；
2. 分别填写来源名，例如 `BTC V2 2H PUT` 和 `BTC V2 2H CALL`；
3. 让页面生成各自 `source_id`；
4. 复制 Webhook URL 和完整 JSON。

当前控制台生成 Schema V2：

```json
{
  "schema_version": "2",
  "passphrase": "服务器配置的WEBHOOK口令",
  "source_id": "自动生成的稳定ID",
  "source_name": "用户填写的来源名称",
  "signal_code": "{{strategy.order.id}}",
  "explicit_action": "{{strategy.order.alert_message}}",
  "previous_position": "{{strategy.prev_market_position}}",
  "market_position": "{{strategy.market_position}}",
  "symbol": "{{ticker}}",
  "timeframe": "{{interval}}",
  "bar_time": "{{time}}",
  "event_time": "{{timenow}}"
}
```

不要替换 `{{strategy.*}}` 占位符。

### 9.4 创建 Alert

PUT/CALL 分别操作：

1. Condition 选择对应 Pine 实例；
2. 事件选择 **订单成交**；
3. Webhook URL 粘贴系统地址；
4. Message 粘贴完整 JSON；
5. 设置持续运行并保存；
6. 检查 TradingView Alert 日志。

方向推导：

```text
long→PUT_CREDIT
short→CALL_CREDIT
OPEN/CLOSE 由 alert_message 传递
```

### 9.5 source_id

`source_id` 用于幂等、防重复下单、关联 OPEN/CLOSE 和按来源汇总。PUT/CALL 两个 Alert 各自保留自己的 ID。重建 Alert 时从接入页重新复制完整消息。

### 9.6 修改脚本后

修改 Pine、参数、方向、订单 ID 或 Alert JSON 后必须删除并重建 Alert，因为 TradingView 保存的是创建时快照。

### 9.7 公网地址

TradingView 不能访问 `127.0.0.1`。生产 URL 必须类似：

```text
https://你的域名/webhook/signal
```

用 Caddy/Nginx 把 443 反代到 8300。

### 9.8 当前 Mainnet 限制

Schema V2 通用消息不携带 `sell_delta` 和 DTE。Testnet 会使用配置中最保守的默认 tier 验证通用适配器；Mainnet 不会自动使用这个测试档位，缺少选腿参数的开仓会被拒绝。

因此当前 TV 接入向导是 Testnet 优先流程。迁移 Mainnet 前必须正式确定选腿参数传递/配置口径，不能只改 `.env`。

## 10. Webhook 到开仓

### 10.1 接收

1. 校验字段；
2. 恒定时间比较 `passphrase`；
3. 推导标准 OPEN/CLOSE；
4. 推导 PUT/CALL；
5. 生成 `signal_id`；
6. 写 SQLite；
7. 重复 ID 按幂等处理；
8. OPEN 入队；
9. 快速返回 202，避免 TradingView 超时重发。

信号 ID 核心：

```text
source_id + TradingView订单ID + 标准动作 + bar_time
```

### 10.2 开仓检查

- 强平锁和交易开关；
- 是否已有活动交易；
- 是否存在系统外卖方期权；
- 选腿参数是否合法；
- 是否有符合 DTE、Delta、盘口和权利金的合约；
- 单笔/组合风险额度；
- 数量是否至少 0.1 张。

## 11. 选腿和风险算量

### 11.1 tier 默认映射

| tier | 卖腿目标绝对 Delta | DTE |
|---:|---:|---:|
| 1 | 0.15 | 30–45 天 |
| 2 | 0.20 | 20–35 天 |
| 3 | 0.25 | 14–28 天 |

这些是待审核口径。

### 11.2 选腿

卖腿要求：

```text
0.05≤|Delta|≤0.70
||Delta|-目标Delta|≤0.10
```

PUT 买腿行权价低于卖腿；CALL 买腿高于卖腿；两腿同到期、都有双边报价。

```text
credit_est_btc=卖腿best_bid-买腿best_ask
```

默认至少 `0.0005 BTC/张`。同一卖腿下，当前规则选择权利金最便宜的合格保护腿。

### 11.3 最大损失

设：

```text
W=|卖腿行权价-买腿行权价|
K_buy=买腿行权价
C=净权利金BTC/张
```

CALL：

```text
max_loss_btc=max(0,W/K_buy-C)
```

PUT 使用 1.3 占位压力系数：

```text
max_loss_btc=max(0,1.3×W/K_buy-C)
```

`1.3` 是待审核安全系数，不是市场定律。

### 11.4 数量

```text
Q=账户权益BTC
r_trade=2%
r_portfolio=6%
R_active=活动价差剩余最大风险

R_trade=Q×r_trade
R_remaining=max(Q×r_portfolio-R_active,0)
R_entry=min(R_trade,R_remaining)
qty_raw=R_entry/max_loss_per_contract_btc
qty=向下取整到0.1张
```

TradingView 的回测虚拟数量不决定真实 Deribit 数量。

## 12. 开仓执行与 E

执行顺序：Combo 优先；剩余量拆组；每组先买保护腿再卖收权腿；卖腿失败回滚保护腿；保存报价、订单、成交和费用。

默认：

```text
n_exec_groups=5
attempt_waits=[3,8,15]秒
```

`E` 是开仓时 BTC 指数价。当前实现在保护腿和卖腿都完整成交后采样 E，而不是信号到达时采样，避免五批执行期间的价格变化污染 BEP 和 α/β 价格线。

## 13. BEP 公式

设：

```text
E=双腿完成后采样的BTC指数价
K_s=卖腿行权价
C_btc=实际成交净权利金BTC/张
C_usd=C_btc×E
B=BEP
```

CALL：

```text
B=K_s+C_usd
```

PUT：

```text
B=K_s-C_usd
```

BEP 使用实际双腿成交净权利金，不使用下单前盘口估算值。

## 14. α：对冲激活系数

### 14.1 定义与公式

α 表示 BTC 从开仓指数价 E 向 BEP B 走了多少比例时激活动态对冲。

```text
A=E+α×(B-E)
```

A 为激活价。

| α | 激活位置 | 含义 |
|---:|---|---|
| 0.00 | E | 开仓价即激活 |
| 0.20 | E 到 B 的 20% | 提前激活 |
| 0.50 | 中点 | 走完一半风险距离 |
| 1.00 | B | 到 BEP 激活 |
| 1.50 | 越过 BEP 50% 同等距离 | 更迟激活 |
| 2.00 | 越过 BEP 一倍距离 | 当前允许上限 |

当前 YAML 默认：

```text
α=bep_activation_progress=1.00
```

旧审核稿出现过 α=0.20，那是历史口径，不是当前默认。数据库运行时设置可覆盖 YAML，实际值必须查看 `GET /api/runtime_settings`。

### 14.2 激活方向

CALL 通常 B>E：

```text
指数价>=A → 激活
```

PUT 通常 B<E：

```text
指数价<=A → 激活
```

### 14.3 数字例子

CALL：

```text
E=100,000，B=110,000，α=1.00
A=100,000+1.00×10,000=110,000
```

BTC 涨至 110,000 或以上激活。

PUT：

```text
E=100,000，B=90,000，α=1.00
A=100,000+1.00×(-10,000)=90,000
```

BTC 跌至 90,000 或以下激活。

### 14.4 价格来源

默认 `activation_price_source=REALTIME_INDEX`，订阅 Deribit `deribit_price_index.btc_usd` 实时 tick。实时路径负责快速激活，周期扫描作为兜底。

**激活不等于立即下单。** 它只把状态从 `HEDGE_INACTIVE` 改为 `HEDGE_ACTIVE`；之后还要满足 Delta 阈值、最小金额、冷却和执行通道条件。

## 15. β：对冲撤销恢复比例

### 15.1 定义与公式

β 表示价格从 BEP B 向开仓价 E 恢复多少比例后进入撤销区。

```text
R=B-β×(B-E)
 =E+(1-β)×(B-E)
```

R 为撤销线。

| β | 撤销位置 | 含义 |
|---:|---|---|
| 0.00 | B | 回到 BEP 即进入撤销区 |
| 0.25 | 从 B 恢复 25% | 较早撤销 |
| 0.50 | B 与 E 中点 | 恢复一半 |
| 0.75 | 从 B 恢复 75% | 更接近开仓价 |
| 1.00 | E | 完全回到开仓价 |

当前默认：

```text
β=release_recovery_pct=0.50
```

### 15.2 撤销方向

CALL：

```text
完整复核Bar收盘价<=R → 进入撤销等待
```

PUT：

```text
完整复核Bar收盘价>=R → 进入撤销等待
```

### 15.3 数字例子

CALL：

```text
E=100,000，B=110,000，β=0.50
R=110,000-0.50×10,000=105,000
```

对冲激活后，完整复核 Bar 回到 105,000 或以下才进入撤销等待。

PUT：

```text
E=100,000，B=90,000，β=0.50
R=90,000-0.50×(-10,000)=95,000
```

对冲激活后，完整复核 Bar 回到 95,000 或以上才进入撤销等待。

### 15.4 旧参数兼容

旧 `hedge_release_progress` 表示“撤销时仍剩多少 E 到 B 风险距离”：

```text
old_progress=1-β
β=1-old_progress
```

旧值 `0.05` 等价于 β=0.95，并不等于当前 β=0.50。当前明确配置 `release_recovery_pct`，所以新 β 优先生效；旧字段只在新字段不存在时回退。

## 16. α 与 β 的迟滞关系

激活线占 E→B 距离的 α，撤销线占 E→B 距离的 `1-β`。为保证撤销线比激活线更靠近开仓价：

```text
1-β<α
即 α+β>1
```

归一化迟滞：

```text
hysteresis_gap=α+β-1
```

实际美元迟滞宽度：

```text
H=|B-E|×(α+β-1)
```

当前默认：

```text
α=1.00，β=0.50，gap=0.50
```

即迟滞区宽度是 E 到 B 距离的 50%，用于降低价格抖动导致的频繁开关。

无效例子：

```text
α=0.20，β=0.50
α+β=0.70<=1
```

此时激活区和撤销区重叠，API 会拒绝保存；启动发现数据库遗留无效组合时会清除运行时覆盖并回退默认。

## 17. Delta 对冲公式

### 17.1 Delta

```text
option_delta=相关期权腿Delta合计
futures_delta=BTC-PERPETUAL Delta
net_delta=option_delta+futures_delta
```

目标是把策略衍生品净 Delta 调到接近 0，不是消除全部 PnL 风险。Delta 会受价格、Gamma、期限和隐含波动率影响，必须持续复核。

### 17.2 有效触发阈值

旧阈值：

```text
T_legacy=equity_btc×delta_account_risk_pct×delta_trigger_pct
```

默认 `2%×10%=0.2%` 权益 Delta。

两侧外层阈值：

```text
T_outer=equity_btc×rebalance_outer_equity_pct
```

默认 `rebalance_outer_equity_pct=2%`。

最终：

```text
T_effective=max(T_legacy,T_outer,10/index_price)
```

默认通常由 2% 外层阈值主导。

### 17.3 目标订单

当 `|net_delta|>=T_effective`：

```text
order_btc=-net_delta
order_usd_raw=order_btc×index_price
order_usd=四舍五入到10 USD合约面值
```

例：

```text
net_delta=+0.30 BTC
BTC指数=100,000 USD
order_usd≈-30,000 USD
```

系统卖出约 30,000 USD BTC-PERPETUAL，使净 Delta 接近 0。

### 17.4 最小调仓额

默认还要求：

```text
|order_usd|>=5,000 USD
```

10 USD 是合约取整精度，5,000 USD 是策略抑制小额交易门槛，不是同一概念。

### 17.5 PUT/CALL 分账、净额执行

`hedge_scope=SIDE_BOOK` 时两侧分别计算 Delta、阈值和冷却，但同一 Deribit 子账户的 BTC-PERPETUAL 只有一个净仓：

```text
PUT建议 -20,000 USD
CALL建议 +12,000 USD
最终净调整 -8,000 USD
```

完全抵消时记录 `NETTED_BETWEEN_BOOKS`。当前开仓层只允许一个活动父交易，因此双侧分账主要服务恢复、历史仓位和扩展。

## 18. 对冲状态机

```text
HEDGE_INACTIVE
  │ 触及α激活线
  ▼
HEDGE_ACTIVE
  │ Delta和金额门槛满足
  ▼
HEDGE_REBALANCING
  │ 部分成交
  ▼
HEDGE_RETRYING
  │ 多轮未收敛
  ▼
HEDGE_DEGRADED

HEDGE_ACTIVE/HEDGE_DEGRADED
  │ 完整Bar进入β撤销区
  ▼
HEDGE_RELEASE_PENDING
  │ 连续确认满足
  ▼
HEDGE_CLOSING
  │ 策略归属永续归零
  ▼
HEDGE_INACTIVE
```

| 状态 | 含义 |
|---|---|
| `HEDGE_INACTIVE` | 未触及激活线，目标为 0 |
| `HEDGE_ACTIVE` | 已激活，监控 Delta |
| `HEDGE_REBALANCING` | 正发送对冲单 |
| `HEDGE_RETRYING` | 部分/零成交后补单 |
| `HEDGE_DEGRADED` | 多轮未收敛，高优先级重试 |
| `HEDGE_RELEASE_PENDING` | 进入撤销区，等待确认 |
| `HEDGE_CLOSING` | 正在归零策略永续 |

## 19. 自动对冲执行

### 19.1 激活门

父交易可管理、有 BEP、有对冲状态、未强平、未冻结、未在人工取消冷却中、价格触及 α 线。

### 19.2 下单门

激活后还需：本侧 Delta 超阈值、金额至少 5,000 USD、不在冷却或达到紧急倍数、没有撤销/调仓占用通道。

### 19.3 冷却

```text
rebalance_cooldown_sec=360秒
emergency_trigger_multiplier=2.0
```

普通调仓后 6 分钟不重复调仓；`|net_delta|>=2×T_effective` 时可绕过冷却。

### 19.4 IOC 重试

```text
初始5 tick
最多3轮
间隔1.5秒
每轮增加5 tick：5/10/15
```

每轮重读真实仓位和 Delta。三轮仍未达标进入 `HEDGE_DEGRADED`，默认每 15 秒高优先级重试。

## 20. β 撤销流程

默认：

```text
review_bar_minutes=1
release_confirmation_bars=1
β=0.50
```

1. 实时 tick 不负责撤销；
2. 完整 1 分钟 Bar 进入 β 撤销区；
3. 进入 `HEDGE_RELEASE_PENDING` 并记录本 Bar；
4. 后续连续完整 Bar仍在撤销区，确认数增加；
5. 达到确认数后入队 `HEDGE_RELEASE`；
6. 只冲销系统记录为本策略归属的 `futures_usd`；
7. 成功后回到 `HEDGE_INACTIVE`；
8. 中途离开撤销区或重新突破 α 线，取消撤销并回到 ACTIVE。

默认第一根只进入等待，至少需要下一根完整 Bar继续满足，不是触线立即归零。

## 21. 人工对冲

### 21.1 一键对冲

```text
POST /control/manual_hedge
```

仅 Testnet。它绕过 α 价格门，核对实际永续与系统归属，激活仓位，按最新 `-net_delta` 用五批 Maker-first 执行；成交后交回正常 β 自动撤销。

### 21.2 一键取消

```text
POST /control/manual_hedge_release
```

仅 Testnet。它只关闭系统归属永续。实际永续与系统归属不一致时拒绝，以免误关人工仓位。成功后至少等待 10 秒才允许自动重新激活。

人工操作不会关闭期权价差、不会处理无法归属的人工永续、不会绕过队列。

## 22. 平仓与到期

TradingView CLOSE 根据来源和方向定位目标交易。默认 `keep_buy_leg_on_normal_exit=false`，正常退出平买卖两腿。

执行分组与管理子组不同：

```text
n_exec_groups=5             # 下单节奏
group_split_count_default=4 # 持仓管理
```

单独平子组：

```text
POST /control/close_group?group_id=N
```

默认 `expiry_policy=HOLD_TO_EXPIRY`，正常持有到 Deribit 到期结算。`expiry_close_dte` 仅兼容提前平仓模式。

前端可锁定新开仓到期日；该日期凑不出合格双腿就拒绝，不会静默换日期。

## 23. 强平与残余

### 23.1 全账户强平

```text
POST /control/force_close?reason=manual
```

顺序：

```text
关闭交易开关
→ 打断可中断执行
→ 撤挂单
→ 平期权腿
→ 归零永续
→ 标记FORCE_CLOSED
```

这是危险的全账户操作，不是普通策略退出。

### 23.2 强平锁

强平后 `force_close_active` 保持有效。人工确认账户清空后：

```text
POST /control/clear_force_close
```

解除锁后仍需单独开启交易。

### 23.3 保护腿残余

卖腿已平、保护腿暂时无法成交时：

- 重试：`POST /control/residual/retry`；
- 人工隔离：`POST /control/residual/quarantine`。

隔离表示接受长腿留到以后成交或到期；它不参与新策略名额和 Delta 对冲，但仍是真实持仓。

## 24. 监控台怎么看

### 24.1 系统健康

查看环境、交易开关、强平锁、Deribit 认证、扫描时间/错误、执行队列。

### 24.2 账户

核对权益、可用资金、保证金率、指数价、期权/永续真实仓位、挂单，以及策略归属永续是否等于交易所实际永续。

### 24.3 对冲字段

| 字段 | 含义 |
|---|---|
| `open_index_price` | E |
| `bep_price` | B |
| `activation_progress` | α |
| `activation_price` | A |
| `release_line` | R |
| `option_delta` | 期权 Delta |
| `futures_delta` | 永续 Delta |
| `net_delta` | 二者之和 |
| `effective_trigger` | 有效外层阈值 |
| `futures_usd` | 策略归属永续金额 |
| `state` | 对冲状态 |
| `reason_code` | 未激活/未调仓/等待原因 |

### 24.4 常见事件

- `REJECT_TRADING_DISABLED`；
- `REJECT_ACTIVE_POSITION_EXISTS`；
- `REJECT_EXISTING_OPTION_SHORT`；
- `REJECT_NO_VALID_LEGS`；
- `REJECT_PORTFOLIO_RISK_LIMIT`；
- `BEP_HEDGE_ACTIVATED`：名称保留旧称，实际是 α 激活线；
- `HEDGE_RELEASE_PENDING`；
- `DELTA_HEDGE_REBALANCE`；
- `IOC_RETRIES_EXHAUSTED`；
- `FORCE_CLOSE_MANUAL`。

## 25. 运行时设置

`GET /api/runtime_settings` 返回实际生效值、YAML 默认和数据库覆盖。覆盖保存在 `system_state`，重启继续生效。

| 参数 | YAML 默认 | 说明 |
|---|---:|---|
| `bep_activation_progress` | 1.00 | α，允许 0–2 |
| `release_recovery_pct` | 0.50 | β，允许 0–1 |
| `review_bar_minutes` | 1 | 1/3/5/10/15/30/60 分钟 |
| `release_confirmation_bars` | 1 | 允许 1–10 |
| `forced_expiry_ts` | 空 | 锁定新开仓到期日 |
| `rebalance_outer_equity_pct` | 0.02 | 两侧 Delta 外层比例 |
| `min_rebalance_usd` | 5000 | 最小调仓额 |
| `rebalance_cooldown_sec` | 360 | 普通冷却 |
| `emergency_trigger_multiplier` | 2.0 | 紧急绕过倍数 |

修改 α/β 时原子校验 `α+β>1`。传 null/空字符串会清除覆盖并回到 YAML。修改会影响所有适用的未关闭仓位，实盘应有变更审计和双人确认。

## 26. 常用 API

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/status` | 系统、扫描、执行和活动交易 |
| GET | `/account` | 余额、保证金、持仓和挂单 |
| GET | `/trades` | 交易列表 |
| GET | `/events?limit=50` | 最近事件 |
| GET | `/api/signals` | TV 信号活动流 |
| GET | `/api/tradingview_setup` | Webhook 向导 |
| GET | `/api/entry_conditions` | 只读策略诊断 |
| GET/POST | `/api/runtime_settings` | 读取/修改 α、β、复核和期限 |
| GET | `/hedge_stats?days=7` | 对冲统计 |
| GET | `/api/hedge_ledger` | 对冲归属流水 |
| POST | `/webhook/signal` | 当前 TV 入口 |
| POST | `/webhook/tradingview` | 旧 Alert 兼容 |
| POST | `/control/trading?enabled=...` | 开关新开仓 |
| POST | `/control/manual_hedge` | Testnet 一键对冲 |
| POST | `/control/manual_hedge_release` | Testnet 一键取消 |
| POST | `/control/close_group?group_id=N` | 平子组 |
| POST | `/control/force_close?reason=...` | 全账户强平 |
| POST | `/control/clear_force_close` | 清强平锁 |

## 27. 测试

后端：

```powershell
Set-Location web/backend
python -m pytest -q
```

前端构建：

```powershell
Set-Location web/frontend
npm run build
```

脚本：

```powershell
Set-Location web/backend
python scripts/check_connectivity.py
python scripts/manual_open.py --type PUT_CREDIT --tier 2
python scripts/manual_open.py --type PUT_CREDIT --tier 2 --qty 0.1 --real
python scripts/e2e_signal_test.py
python scripts/e2e_signal_test.py --open
python scripts/e2e_signal_test.py --exit
```

`manual_open.py` 默认 dry-run；`--real` 产生所选环境的真实订单，执行前必须确认 `.env`。

### 27.1 α/β Testnet 验证

1. 建小额价差；
2. 核对 E、B、A、R；
3. 用一键对冲验证人工激活和五批执行；
4. 观察 β 撤销线和完整 Bar 确认；
5. 验证人工取消后的 10 秒冷却；
6. 验证部分成交和永续归属；
7. 必要时临时使用 `debug.bep_override/trigger_override`；
8. 测试后清除 debug 并重启。

## 28. 常见问题

### 页面打不开

检查 8300/5173、前后端日志，并单独请求 `/status`。Deribit 网络连接失败也会影响后端启动。

### Webhook 401

检查 JSON 口令与 `.env`、是否复制旧服务器口令、修改 `.env` 后是否重启。

### TV 有图标，后端没信号

检查 Alert 是否选“订单成交”、URL 是否公网可达、Alert 是否仍是旧脚本快照、JSON 是否完整、TV 日志是否发送成功。

### 信号到了但不开仓

检查交易开关、活动交易、账户外卖方期权、Mainnet 通用 V2 选腿限制、DTE/Delta/盘口、最低权利金、风险额度和最小 0.1 张。

### 已激活但没永续单

可能是 Delta 未超阈值、金额低于 5,000 USD、在冷却、两侧净额抵消、执行通道占用、对冲冻结或强平锁，并不一定是故障。

### 进入撤销区但没有马上归零

默认第一根 Bar 只进入 `HEDGE_RELEASE_PENDING`，还要下一根完整 1 分钟 Bar连续确认。

### 实际永续与系统归属不一致

不要强行开/关对冲。检查同账户人工交易、其他程序、未记账成交和 `/api/hedge_ledger`。系统会拒绝可能误关人工仓位的操作。

### 重启状态异常

不要删除数据库。保存日志和交易所成交记录，让恢复逻辑核对 OPENING/CLOSING、孤儿挂单、晚到成交和残余腿。

## 29. 云部署

```text
TradingView
  → HTTPS 443 / Caddy或Nginx
  → 127.0.0.1:8300 / FastAPI
  → Deribit
```

应配置 systemd 自动重启、TLS、管理页面认证、`.env` 权限、SQLite 备份、日志轮转和 `/status` 告警。

管理页面当前没有应用级口令，公网部署必须额外保护。Mainnet 前必须解决通用 V2 的正式选腿口径。

## 30. 当前重点风险

以下是当前实现，不代表完成实盘定标：

1. tier Delta/DTE；
2. PUT 最大损失 1.3 压力系数；
3. 买腿选最便宜合格保护腿；
4. 单笔 2%、组合 6% 风险；
5. 日亏损 5%、保证金 60%、行情缺失 5 轮；
6. α=1.00、β=0.50；
7. Delta 外层 2%；
8. 最小调仓 5,000 USD；
9. 冷却 360 秒、紧急倍数 2；
10. 默认持有到期；
11. 全账户单活动父交易；
12. Schema V2 TV 开仓的 Testnet-only 默认选腿行为。

## 31. 代码方法简述

- `webhook.py`：TV 消息转统一 OPEN/CLOSE；
- `orchestrator.py`：开仓检查、选腿、算量和执行；
- `leg_selector.py`：Delta/DTE、腿和损失公式；
- `spread_executor.py`：Combo 与保护腿优先执行；
- `commands.py/worker.py`：串行优先级队列；
- `bep.py`：BEP、α 激活线、β 撤销线；
- `hedge.py`：Delta、分账、调仓和撤销状态机；
- `realtime_hedge.py`：实时指数激活；
- `scanner.py`：周期风控和完整 Bar 复核；
- `risk.py`：一致性、强平和启动恢复；
- `store/`：SQLite 台账。

## 32. 实盘前清单

- [ ] 环境明确为 Testnet/Mainnet；
- [ ] `.env` 密钥未进入 Git；
- [ ] Deribit API 使用最小权限；
- [ ] TV 两个实例方向正确；
- [ ] 两个 Alert 均为“订单成交”；
- [ ] Webhook 使用 HTTPS；
- [ ] JSON 占位符未修改；
- [ ] DVOL 达到 1460/1460；
- [ ] Pine 参数经过回测；
- [ ] `/api/runtime_settings` 核实实际 α/β；
- [ ] 理解 `α+β>1` 和迟滞宽度；
- [ ] Delta 阈值、5,000 USD 和冷却适合账户规模；
- [ ] tier、PUT 1.3 和买腿规则已审核；
- [ ] 完成开仓、拒绝、部分成交、对冲、撤销、平仓和强平测试；
- [ ] 完成重启恢复和永续不一致测试；
- [ ] 管理页面有认证；
- [ ] 数据库、日志和服务器有备份告警；
- [ ] Mainnet 前完成正式选腿口径；
- [ ] 明确全账户强平可能处理账户内全部 BTC 期权和永续。

## 33. 日常最简流程

```text
启动
→ 核对环境、扫描、Deribit和真实仓位
→ 检查两个TradingView Alert
→ 检查α、β、复核周期和到期日
→ 开启交易
→ 观察信号到达和开仓执行
→ 观察E、B、A、R及Delta
→ 关注事件、重试和残余
→ 停止新开仓时关闭trading_enabled
→ 普通退出用策略退出/子组平仓
→ 全账户强平仅用于明确紧急情况
```
