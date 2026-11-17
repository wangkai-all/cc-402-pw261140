"""入口：python3 -m scripts.run_server [--port 8000] [--reset]"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from retention.app import App  # noqa: E402
from retention.http_api import serve  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--var", default=str(ROOT / "var"))
    ap.add_argument("--reset", action="store_true", help="清空 var 后全新启动")
    ap.add_argument("--heartbeat-timeout", type=float, default=15.0)
    args = ap.parse_args()

    var_dir = Path(args.var)
    if args.reset and var_dir.exists():
        shutil.rmtree(var_dir)
    app = App(var_dir, heartbeat_timeout=args.heartbeat_timeout)
    app.start_background()
    httpd = serve(app, args.host, args.port)
    print(f"留存治理控制台: http://{args.host}:{args.port}  (Ctrl+C 退出)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        app.shutdown()
        time.sleep(0.3)


if __name__ == "__main__":
    main()
