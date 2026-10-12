"""Локальный HTTP-сервер для frontend Digital Store (только для разработки).

    .\\.venv\\Scripts\\python.exe frontend\\serve.py            # http://127.0.0.1:5500
    .\\.venv\\Scripts\\python.exe frontend\\serve.py --port 5173

Чем лучше простого `python -m http.server`:
  * раздаёт ТОЛЬКО папку frontend/ — файлы проекта (.env с паролями, код backend)
    недоступны;
  * слушает только 127.0.0.1 — страницы не видны другим компьютерам в сети;
  * всегда отдаёт .js/.mjs как JavaScript — на некоторых Windows реестр
    выдаёт text/plain, и браузер отказывается загружать ES-модули;
  * отключает кеш, чтобы правки были видны сразу.
"""

import argparse
import functools
import http.server
from pathlib import Path

FRONTEND_DIR = Path(__file__).resolve().parent

TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".md": "text/plain; charset=utf-8",
}


class FrontendHandler(http.server.SimpleHTTPRequestHandler):
    def guess_type(self, path):
        return TYPES.get(Path(str(path)).suffix.lower()) or super().guess_type(path)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()


def main():
    parser = argparse.ArgumentParser(description="Локальный сервер frontend Digital Store")
    parser.add_argument("--port", type=int, default=5500)
    args = parser.parse_args()

    handler = functools.partial(FrontendHandler, directory=str(FRONTEND_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    base = f"http://127.0.0.1:{args.port}"
    print(f"Раздаю {FRONTEND_DIR}")
    print(f"  Демо-витрина:     {base}/examples/vanilla-storefront/")
    print(f"  Тесты JS-клиента: {base}/tests/")
    print("Остановить: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
