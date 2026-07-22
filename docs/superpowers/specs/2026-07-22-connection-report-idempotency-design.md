# TradingView 连接回报幂等设计

## 目标

消除同一 TradingView 警报在同一根 2 小时 K 线上重复触发 `STATUS` 时产生的重复连接回报，同时确保开仓、退出与连接回报可以在同一根 3 分钟 K 线收盘时全部发送。

## 已确认事实

- TradingView 当前只有一个启用中的 BTC 警报。
- 2026-07-22 16:03:02 的两条 `STATUS` 请求仅相差 9 毫秒。
- 两条请求的 `strategy`、`bar_ts` 和业务 payload 完全相同。
- TradingView 日志自身也记录了两次触发，因此重复不是前端渲染造成的。
- 开仓与退出信号已有 `signal_id` 幂等保护；连接回报目前每次请求都会插入新行。

## 方案比较

### 方案 A：只修改 Pine

在 Pine 内记录最近发送连接回报的 `bar_ts`。优点是尽量从源头消除重复；缺点是无法防御 TradingView 平台重复执行、重复投递或并行告警快照。

### 方案 B：只修改后端

后端按 `(strategy, bar_ts)` 去重。优点是最终展示与计数稳定；缺点是 TradingView 日志仍可能出现重复，而且源头没有主动保护。

### 方案 C：Pine 与后端双层幂等（采用）

Pine 负责正常情况下只调用一次连接回报；后端负责最终一致性。该方案保留 `alert.freq_all`，不会吞掉同一轮中的 OPEN、EXIT 或 STATUS 消息。

## Pine 设计

- 新增持久状态 `lastStatusT`，保存最近已发送连接回报的 2 小时 `bar_ts`。
- 连接回报条件增加 `na(lastStatusT) or t2 != lastStatusT`。
- 成功调用 `alert(f_status_json(), alert.freq_all)` 后立即把 `lastStatusT` 更新为 `t2`。
- OPEN、EXIT 的条件、顺序、JSON 和频率保持不变。
- 不把 STATUS 改为 `alert.freq_once_per_bar`，因为它可能抑制同一根 3 分钟 bar 中更早或更晚的其他 `alert()` 调用。

## 后端设计

- `pine_heartbeats` 对有效的 `(strategy, bar_ts)` 建立唯一索引。
- 启动迁移在建立索引前删除已有重复项：每组保留最早接收的一条，保证历史计数回归真实 2 小时 bar 数。
- `record_pine_heartbeat()` 使用 `INSERT OR IGNORE`，并返回布尔值：`True` 表示新连接回报，`False` 表示重复回报。
- 即使重复，Webhook 仍返回 HTTP 200 和成功状态，并附带 `duplicate: true`，防止上游继续重试。
- `bar_ts` 或 `strategy` 缺失时不应用组合键去重，以保留诊断信息；正常 Pine payload 两者均存在。
- 只有新连接回报更新 `system_state.pine_status` 和历史保留清理；重复请求不改变可见状态。

## 数据流

1. 3 分钟图在新 2 小时 bar 的首根 3 分钟 K 线收盘时计算 STATUS。
2. Pine 检查 `lastStatusT`，同一个 `t2` 只调用一次 `alert()`。
3. 后端鉴权并解析 STATUS。
4. SQLite 尝试按 `(strategy, bar_ts)` 插入。
5. 新记录进入信号中心；重复记录返回成功但不进入历史、计数或列表。

## 测试与验收

- Pine 源码契约测试验证存在 `lastStatusT` 守卫，并保留四类 `alert.freq_all` 调用。
- Repo 测试验证相同 `(strategy, bar_ts)` 只插入一次，不同 bar 或不同策略仍分别插入。
- Webhook 测试验证第二次相同 STATUS 返回成功且标记为重复。
- 迁移测试验证旧数据库中的重复连接回报会被压缩后再建立唯一索引。
- 完整后端测试、前端测试和前端构建全部通过。
- 云端部署后 `/api/signals` 中同一 `(strategy, bar_ts)` 最多出现一次。

## 用户后续操作

云端部署完成后，用户需要在 TradingView 中把更新后的 Pine 代码保存为新版本，并删除或暂停当前 BTC 警报后重新创建。TradingView 警报使用创建时的脚本快照，仅保存脚本不会更新现有警报。

