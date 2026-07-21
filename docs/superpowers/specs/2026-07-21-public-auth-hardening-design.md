# 公网部署鉴权加固设计

## 目标

在不增加 IP 白名单、VPN、Caddy 二次登录等额外限制的前提下，为公网部署建立清晰的两条鉴权边界。

## 鉴权模型

- `WEBHOOK_PASSPHRASE` 仅用于 TradingView 向 `/webhook/tradingview` 发送信号。
- `DASHBOARD_TOKEN` 仅用于管理网页访问状态、账户数据和 `/control/*` 操作。
- 所有管理 API 始终校验 `X-Auth: DASHBOARD_TOKEN`，包括来自 `127.0.0.1` 的请求；不再根据客户端 IP 免鉴权。
- 为方便现有本地环境迁移，如果新变量缺失，可暂时回退到旧的 `WEBHOOK_SECRET`；云端 `.env` 必须配置两枚独立新密钥。

## 保留行为

- 不增加 IP 白名单、VPN 或 Caddy Basic Auth。
- testnet 测试工具继续保留，并继续由现有的 `DERIBIT_ENV=testnet` 限制；mainnet 仍拒绝模拟信号。
- 前端遇到 `401` 时继续弹出远程访问口令，并把令牌放在 `X-Auth` 请求头中。
- TradingView JSON 格式、信号去重、交易队列和 Deribit 执行逻辑不变。

## 启动与监听

- `server.py` 默认监听 `127.0.0.1:8300`，云端由 Caddy 反向代理。
- `start.ps1` 从后端 `.env` 读取 `DASHBOARD_TOKEN`，缺失时回退到 `WEBHOOK_SECRET`；健康检查自动发送 `X-Auth`，且不得打印令牌。

## 配置和文档

- `.env.example` 使用 `WEBHOOK_PASSPHRASE` 与 `DASHBOARD_TOKEN`。
- Pine 参数 `passphrase` 对应 `WEBHOOK_PASSPHRASE`。
- 管理网页输入的访问口令对应 `DASHBOARD_TOKEN`。
- 更新运行手册和连接测试脚本中的旧变量名。

## 验证

- 未带令牌的本机 `/status` 请求必须返回 `401`。
- 正确 `DASHBOARD_TOKEN` 能访问管理 API，错误令牌被拒绝。
- webhook 只接受 `WEBHOOK_PASSPHRASE`，不能使用不同的管理令牌。
- testnet 模拟信号仍可在正确管理令牌下使用。
- `start.ps1 -NoBrowser` 能完成带鉴权的前后端健康检查。
- 后端测试、前端测试与构建全部通过。
