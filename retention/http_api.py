"""纯标准库 HTTP 服务：JSON API + 静态网页（web/index.html）。"""
from __future__ import annotations

import json
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .app import App

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class Handler(BaseHTTPRequestHandler):
    app: App = None  # 由 main 注入

    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass

    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _error(self, e: Exception, status: int = 400) -> None:
        self._send_json({"error": type(e).__name__, "message": str(e),
                         "trace": traceback.format_exc(limit=2)}, status)

    # ---------- 路由 ----------
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/api/state":
                return self._send_json(self.app.snapshot())
            if path == "/api/preview":
                return self._send_json(self.app.reaper.evaluate())
            if path == "/api/events":
                return self._send_json({"events": self.app.events.list(300)})
            return self._static(path)
        except Exception as e:
            self._error(e, 500)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self._read_json()
            app = self.app

            if path == "/api/seed":
                return self._send_json(app.seed_demo())
            if path == "/api/partitions":
                import random
                rng = random.Random()
                records = [
                    {"order_id": f"X{rng.randint(1000, 9999)}",
                     "amount": round(rng.uniform(10, 500), 2),
                     "customer": f"cust_{rng.randint(1, 50)}"}
                    for _ in range(8)]
                info = app.storage.create_partition(body["zone"], body["business_day"], records)
                return self._send_json(info)
            if path == "/api/clock/advance":
                vt = app.clock.advance(float(body.get("seconds", 0)))
                app.events.emit("api", "CLOCK_ADVANCED",
                                {"seconds": body.get("seconds"), "virtual_now": vt})
                return self._send_json({"virtual_now": vt, "virtual_now_iso": app.clock.iso()})
            if path == "/api/policy":
                r = app.policy.set_retention(body["zone"], int(body["retention_days"]),
                                             body.get("note", "网页调整留存期"))
                return self._send_json(r)
            if path == "/api/tasks":
                r = app.executor.create_task(body["name"], [int(x) for x in body["partition_ids"]])
                return self._send_json(r)
            if path == "/api/tasks/lost":
                return self._send_json(app.executor.trigger_lost(int(body["task_id"])))
            if path == "/api/tasks/resume":
                return self._send_json(app.executor.resume_task(int(body["task_id"])))
            if path == "/api/reports":
                r = app.archive.seal_report(int(body["task_id"]), body["title"], body["mode"],
                                            [int(x) for x in body["partition_ids"]],
                                            int(body["hold_days"]))
                return self._send_json(r)
            if path == "/api/reports/renew":
                return self._send_json(app.archive.renew(int(body["report_id"]),
                                                          int(body["extra_days"])))
            if path == "/api/reap":
                return self._send_json(app.reaper.run(dry_run=bool(body.get("dry_run", False))))
            return self._send_json({"error": "NotFound", "message": path}, 404)
        except (ValueError, KeyError, RuntimeError, FileNotFoundError, PermissionError) as e:
            return self._error(e, 400)
        except Exception as e:
            return self._error(e, 500)

    def _static(self, path: str) -> None:
        if path in ("/", "/index.html"):
            f = WEB_DIR / "index.html"
            ctype = "text/html; charset=utf-8"
        elif path == "/app.js":
            f = WEB_DIR / "app.js"
            ctype = "application/javascript; charset=utf-8"
        elif path == "/styles.css":
            f = WEB_DIR / "styles.css"
            ctype = "text/css; charset=utf-8"
        else:
            self.send_error(404)
            return
        if not f.exists():
            self.send_error(404)
            return
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(app: App, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    Handler.app = app
    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd
