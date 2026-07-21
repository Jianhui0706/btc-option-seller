"""BTC 期权卖方系统后端启动入口。

从 web/backend 目录运行：python server.py
开发模式自动重载：python server.py --reload
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn


BACKEND_DIR = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 BTC 期权卖方系统 FastAPI 后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8300)
    parser.add_argument("--reload", action="store_true", help="代码变化时自动重载")
    args = parser.parse_args()

    sys.path.insert(0, str(BACKEND_DIR))
    uvicorn.run(
        "src.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        app_dir=str(BACKEND_DIR),
    )


if __name__ == "__main__":
    main()
