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
