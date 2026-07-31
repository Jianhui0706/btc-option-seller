# 上游基线

本目录以老师指定的开源项目为基线：

- 仓库：<https://github.com/mmmy/deribit_webhook>
- 分支：`main`
- 提交：`247c75bc05bd3fdf319085c75603ee0cad48711e`
- 导入日期：2026-07-23

导入时保留了上游源码和文档，但没有复制上游仓库自己的 `.git` 目录。
这样，本目录可以直接由当前总项目统一追踪，后续适配也能清楚地区分于原系统。

## 基线验证

```bash
npm ci
npm test -- --runInBand
npm run build
```

导入时结果：6 个测试套件、64 个测试全部通过，TypeScript 编译通过。

## 后续改造边界

TradingView 负责计算策略条件并产生明确的开仓、平仓信号；本服务负责：

1. 验证和去重 TradingView webhook；
2. 把信号转换为确定的 Deribit 执行动作；
3. 选择期权合约并执行开仓、平仓；
4. 保存信号、订单、成交和错误结果；
5. 提供必要的状态查询和通知。

后端不再独立计算 TradingView 指标，也不自行产生策略入场或退出信号。
