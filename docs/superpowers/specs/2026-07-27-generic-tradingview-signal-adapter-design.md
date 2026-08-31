# TradingView 通用信号适配与 MACD 1 分钟测试设计

## 目标

把 TradingView 的“策略判断”与后端的“期权执行参数”彻底解耦：

- TradingView 只发送信号来源、信号码和目标仓位变化。
- 后端把不同 TradingView 格式机械转换为统一动作。
- Deribit 执行层根据后端配置完成选腿、数量、独立开仓和平仓。
- 新增或更换 TradingView 策略时，不再修改 Webhook 解析与下单代码。
- 先用 TradingView MACD 的高频信号，在 Deribit Testnet 跑通双侧独立开仓和平仓链路。

## 范围

本次包含：

1. 极简 TradingView 信号协议和旧协议兼容。
2. 内置 MACD Strategy 仓位变化适配。
3. Testnet 默认执行配置。
4. 双侧独立开仓、精确平仓和单来源占用规则。
5. TradingView 接入页和策略信号页简化。
6. 停止发送和展示空闲心跳，只保留关键交易信号。
7. 自动化测试与 MACD 1 分钟测试说明。

本次不包含：

- 评价或优化 MACD 策略收益。
- 在 Mainnet 运行 MACD 测试信号。
- 为每种指标编写专用后端逻辑。
- 同时管理多笔期权卖方仓位。

## 职责边界

### TradingView

TradingView 决定：

- 什么时候产生交易信号。
- 信号代码是什么，例如 `MacdLE`、`MacdSE`。
- 目标策略仓位是 `long`、`short` 还是 `flat`。
- 自定义 Pine 明确发送的 `OPEN` 或 `CLOSE` 意图；旧 `REVERSE` 输入兼容转换为目标侧 `OPEN`。

TradingView 不再决定：

- 期权价差类型的具体选腿参数。
- Delta、DTE、数量、管理分组和对冲参数。
- Deribit 下单步骤和失败恢复方式。

### 信号适配器

适配器只进行确定性格式转换，不计算 MACD、DVOL、HV、CCI、布林带或其他行情条件。

优先使用自定义信号中的明确动作；没有明确动作时，根据 TradingView 的前后仓位转换：

| 前一仓位 | 当前仓位 | 标准动作 |
|---|---|---|
| `flat` | `long` | `OPEN_LONG` |
| `flat` | `short` | `OPEN_SHORT` |
| `long` | `flat` | `CLOSE_LONG` |
| `short` | `flat` | `CLOSE_SHORT` |
| `long` | `short` | `OPEN_SHORT`（已有 LONG 对应价差保持不变） |
| `short` | `long` | `OPEN_LONG`（已有 SHORT 对应价差保持不变） |

相同仓位到相同仓位不是新的交易意图，应作为无操作信号拒绝或忽略，不能重复开仓。

### 执行后端

执行后端负责：

- 口令鉴权、字段校验、幂等去重和 Testnet 限制。
- 把标准动作放入交易命令队列。
- 根据后端执行配置选择价差方向、Delta、DTE、数量、分组和对冲参数。
- 调用 Deribit 完成真实开平仓。
- 保存原始信号、标准动作和执行结果。

## 极简信号协议

TradingView 接入页生成的消息只保留必要信息：

```json
{
  "schema_version": "2",
  "passphrase": "<server configured secret>",
  "source_id": "<automatically generated stable alert id>",
  "source_name": "MACD Strategy 1m Test",
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

规则：

- 用户只需填写一次 `source_name`。
- `source_id` 由接入页自动生成并写入 JSON，不要求用户理解或手动管理。
- `signal_code` 直接保存 TradingView 的订单 ID。
- `explicit_action` 对内置 MACD 可以为空；适配器用前后仓位推导动作。
- `bar_time + source_id + signal_code + 标准动作` 组成幂等身份。
- 旧版 `strategy`、`tv_id`、`order_id`、`action`、`marketPosition` 和 `prevMarketPosition` 继续兼容。

## 后端执行配置

每个信号来源绑定一个后端执行配置；未单独配置的 Testnet 来源使用 `TESTNET_DEFAULT`。

首个默认配置：

- 环境：只允许 `Deribit Testnet`。
- `OPEN_LONG`：执行独立的 `PUT_CREDIT`。
- `OPEN_SHORT`：执行独立的 `CALL_CREDIT`。
- 卖腿 Delta 和 DTE：使用后端 `leg_selection` 配置的明确默认值。
- 数量：使用后端风险预算结果，不读取 TradingView 的合约数量。
- 管理分组、Delta 风险比例、触发比例和撤销线：使用后端配置。

执行配置决定“怎么交易”，但不决定“什么时候交易”。

## 动作执行

### 开仓

`OPEN_LONG` 或 `OPEN_SHORT`：

1. 检查交易开关、环境和现有仓位。
2. 读取执行配置。
3. 选择期权腿并计算安全数量。
4. 下单并确认成交。
5. 保存父交易与来源身份。

### 平仓

`CLOSE_LONG`、`CLOSE_SHORT` 或通用 `CLOSE`：

1. 优先按 `source_id + spread_type` 定位该来源对应方向的全部活跃父交易。
2. 兼容旧消息的 `tv_id/trade_id`。
3. 只有当前全系统恰好一笔活跃交易时，才允许旧消息无身份回退。
4. 平掉对应交易并确认结果。

没有匹配仓位时返回明确的幂等无操作结果，不触发其他仓位。

### 来源占用与双侧共存

1. 同一时间只允许一个 `source_id` 占用后端交易账户。
2. 该来源可按每条 OPEN 信号叠加 `PUT_CREDIT` / `CALL_CREDIT`，方向变化只开新仓。
3. 其他来源在已有交易处于开仓、持仓或平仓状态时拒绝执行。
4. 同来源重复同侧视为独立加仓批次；每批受单笔风险预算约束，所有活跃批次共享
   组合风险上限。部分平仓按剩余数量释放组合额度；明确的分侧 CLOSE 关闭该来源
   对应侧的全部批次。

## 心跳处理

- Pine 脚本不再发送 `STATUS` 空闲心跳。
- 接入页不再要求选择“订单成交和 `alert()` 函数调用”，只要求“订单成交”。
- 策略信号页不再显示“近期在线”“连接正常”“连接超时”和连接回报数量。
- 旧 `STATUS` 消息可继续被兼容接收，但不进入关键交易信号列表，也不影响策略状态。
- 页面不能因为长时间没有信号就声称连接异常；稀疏信号策略没有事件属于正常情况。

## 页面设计

### TradingView 接入页

用户可见设置缩减为：

1. 信号来源名称。
2. Webhook URL。
3. 自动生成、可复制的极简 Alert JSON。

账户名、`source_id`、口令和执行参数不再作为需要用户反复理解的来源设置展示。口令仍由服务器写入复制内容，`source_id` 自动生成。

### 策略信号页

按 `source_id` 汇总，按 `source_name` 展示：

- 卡片主标题：TradingView 信号来源名称。
- 卡片副信息：Testnet、标的、周期和内部来源码。
- 汇总：开仓、平仓、接受、拒绝和活跃仓位数量。
- 最近关键事件：信号码、标准动作、仓位变化、接收时间和执行结果。
- 展开详情同时显示原始字段与标准化字段，便于排错。

首次收到关键交易信号时自动发现来源，不要求预先注册。

## 错误处理

- 无法推导动作：`unknown_action`。
- 仓位没有变化：`no_position_change`。
- 非 Testnet 的测试来源：`test_source_requires_testnet`。
- 开仓时已有不允许的仓位：保留现有安全拒绝原因。
- 平仓没有匹配来源仓位：返回幂等无操作结果。
- 平仓同时匹配多笔同侧仓位：拒绝并要求提供可唯一定位的来源/方向。
- 重复事件：返回 `duplicate`，不重复执行。

所有结果写入信号日志，HTTP 接口快速返回接收状态；真实交易继续在单 worker 队列中执行。

## 测试

自动化测试覆盖：

1. 内置 MACD 的 `flat → long` 产生一个 `OPEN_LONG`。
2. `long → short` 产生一个独立 `OPEN_SHORT`，不关闭已有 `PUT_CREDIT`。
3. 同来源的 `PUT_CREDIT` 与 `CALL_CREDIT` 可同时存在。
4. 其他来源以及同来源重复同侧开仓会被拒绝。
5. 重复 MACD bar 信号只执行一次。
6. 缺少 Delta、DTE、数量时使用 Testnet 后端默认配置。
7. 旧版复杂消息继续正常转换。
8. `STATUS` 不出现在关键交易信号和来源状态中。
9. 接入页只要求来源名称，并生成有效的极简 JSON。
10. 策略信号页展示来源名称、信号码和标准动作，不显示连接在线状态。

手工验收：

1. 后端明确运行在 Deribit Testnet，交易开关开启。
2. TradingView 加载内置 MACD Strategy，图表周期设置为 1 分钟。
3. Alert 条件选择该策略的“订单成交”。
4. 粘贴系统生成的 Webhook URL 和 JSON。
5. 观察 `MacdLE/MacdSE` 到达并分别建立 PUT/CALL 信用价差，已有另一侧保持不变。
6. 发送明确的分侧平仓信号，核对只关闭对应 PUT 或 CALL；重复信号不产生重复订单。

## 验收标准

- 更换 TradingView 策略时，只需设置来源名称并复制统一 JSON，不改后端代码。
- 策略信号页显示 TradingView 来源名称和真实信号码。
- 信号中不再携带 Delta、DTE、数量、分组和对冲参数。
- MACD 信号源能在 Testnet 驱动 PUT/CALL 独立开仓，并由明确平仓信号分侧退出。
- 系统不发送、不依赖、不展示空闲心跳。
- Mainnet 不接受 MACD 测试来源。
