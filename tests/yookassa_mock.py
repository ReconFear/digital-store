"""Локальный HTTP-имитатор API ЮKassa v3 — ТОЛЬКО для тестов.

Настоящий адаптер payments/yookassa.py ходит сюда по HTTP (127.0.0.1) так же, как в
api.yookassa.ru: Basic-авторизация, Idempotence-Key, POST /v3/payments,
GET /v3/payments/{id}. Имитатор повторяет документированное поведение ЮKassa:
* тот же ключ идемпотентности + те же данные -> тот же платёж (новый не создаётся);
* тот же ключ + другие данные -> 400;
* статусы pending / waiting_for_capture / succeeded / canceled; конечные не меняются;
* GET /v3/payments?created_at.gte=&created_at.lt=&limit=&cursor= — список, новые первыми,
  страницами с next_cursor (как в документации и SDK ЮKassa).

Управление сценариями сбоев — из теста: fail_next(...), set_status(...), tamper(...).
Это имитация протокола, а НЕ проверка настоящей ЮKassa (см. PR3_NOTES.md).
"""

import base64
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


def _now_iso():
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


class YooKassaMock:
    def __init__(self, shop_id="123456", secret_key="test_mock_secret_key_0001"):
        self.shop_id = shop_id
        self.secret_key = secret_key
        self.payments = {}          # id -> объект платежа (dict)
        self.idempotence = {}       # key -> (тело запроса, id платежа)
        self.requests = []          # [(method, path, headers, body)]
        self.failures = []          # очередь сбоев для следующих запросов
        self.create_overrides = {}  # поля, подменяемые в ОТВЕТЕ на создание (для проверки ответа)
        self.redirect_location = "http://127.0.0.1:9/steal"   # куда «перенаправляет» сбой redirect_*
        self.lock = threading.Lock()
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                mock._handle(self, "GET")

            def do_POST(self):
                mock._handle(self, "POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/v3"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    # ---------- управление из тестов ----------

    def fail_next(self, mode, count=1, method=None):
        """mode: 'timeout' | '500' | '500_after_processing' | '202' | '429' | '401' | 'drop'
        | 'garbage' | '400' | 'redirect_<3xx>' | 'redirect_after_processing_<3xx>'.
        method — ограничить сбой методом (GET/POST)."""
        with self.lock:
            self.failures.extend([(mode, method)] * count)

    def set_status(self, payment_id, status, reason=None):
        with self.lock:
            payment = self.payments[payment_id]
            if payment["status"] in ("succeeded", "canceled"):
                raise AssertionError("у ЮKassa конечный статус не меняется")
            payment["status"] = status
            payment["paid"] = status in ("succeeded", "waiting_for_capture")
            if status == "succeeded":
                payment["captured_at"] = "2026-10-13T00:00:00.000Z"
                payment.pop("confirmation", None)
            if status == "canceled":
                payment["cancellation_details"] = {"party": "yoo_money", "reason": reason or "expired_on_confirmation"}
                payment.pop("confirmation", None)

    def tamper(self, payment_id, **changes):
        """Изменить объект платежа у «провайдера» (amount/currency/account_id/test/metadata)."""
        with self.lock:
            payment = self.payments[payment_id]
            if "amount" in changes:
                payment["amount"]["value"] = changes["amount"]
            if "currency" in changes:
                payment["amount"]["currency"] = changes["currency"]
            if "account_id" in changes:
                payment["recipient"]["account_id"] = changes["account_id"]
            if "test" in changes:
                payment["test"] = changes["test"]
            if "metadata" in changes:
                payment["metadata"] = changes["metadata"]
            for key in ("created_at", "status", "paid"):
                if key in changes:
                    payment[key] = changes[key]

    def add_foreign_payment(self, status="succeeded", amount="1.00", metadata=None):
        """Платёж, созданный не нашим магазином/кодом (для поддельных уведомлений)."""
        pid = self._new_id()
        with self.lock:
            self.payments[pid] = self._payment_object(pid, {"value": amount, "currency": "RUB"},
                                                      metadata or {}, "Чужой платёж")
        self.set_status(pid, status)
        return pid

    def created_payments(self):
        return [r for r in self.requests if r[0] == "POST"]

    # ---------- протокол ----------

    @staticmethod
    def _new_id():
        return f"{uuid.uuid4().hex[:8]}-000f-5000-a000-{uuid.uuid4().hex[:12]}"

    def _payment_object(self, pid, amount, metadata, description):
        return {
            "id": pid,
            "status": "pending",
            "paid": False,
            "amount": dict(amount),
            "confirmation": {"type": "redirect", "confirmation_url": f"https://yoomoney.mock/checkout?orderId={pid}"},
            "created_at": _now_iso(),
            "description": description,
            "metadata": dict(metadata),
            "recipient": {"account_id": self.shop_id, "gateway_id": "100500"},
            "refundable": False,
            "test": True,
        }

    def _reply(self, handler, status, body, headers=()):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        handler.send_response(status)
        for name, value in headers:
            handler.send_header(name, value)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    def _take_failure(self, method):
        with self.lock:
            for i, (_mode, only) in enumerate(self.failures):
                if only is None or only == method:
                    return self.failures.pop(i)[0]
        return None

    def _handle(self, handler, method):
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length else b""
        headers = {k.lower(): v for k, v in handler.headers.items()}
        with self.lock:
            self.requests.append((method, handler.path, headers, raw))

        expected = "Basic " + base64.b64encode(f"{self.shop_id}:{self.secret_key}".encode()).decode()
        failure = self._take_failure(method)
        if failure == "401" or headers.get("authorization") != expected:
            return self._reply(handler, 401, {"type": "error", "code": "invalid_credentials"})
        if failure == "timeout":
            time.sleep(1.5)  # дольше таймаута клиента в тестах; запрос НЕ обрабатывается
            return self._reply(handler, 500, {"type": "error", "code": "internal_server_error"})
        if failure == "drop":
            handler.close_connection = True
            handler.connection.shutdown(2)
            return None
        if failure in ("500", "429", "400"):
            return self._reply(handler, int(failure), {"type": "error", "code": "test_failure"})
        if failure == "202":
            return self._reply(handler, 202, {"type": "processing", "retry_after": 10})
        if failure and failure.startswith("redirect_"):
            # redirect_<код> — ответ 3xx без обработки; redirect_after_processing_<код> — платёж
            # создан, но вместо ответа пришло перенаправление (результат для клиента неизвестен).
            code = int(failure.rsplit("_", 1)[1])
            if failure.startswith("redirect_after_processing_") and method == "POST":
                self._create(headers, raw)
            return self._reply(handler, code, {"type": "redirect"}, [("Location", self.redirect_location)])
        if failure == "garbage":
            return self._reply(handler, 200, b"<html>not json</html>")

        if method == "POST" and handler.path == "/v3/payments":
            status, body = self._create(headers, raw)
            if failure == "500_after_processing":
                # Платёж создан, но ответ потерян — результат для клиента неизвестен.
                return self._reply(handler, 500, {"type": "error", "code": "internal_server_error"})
            return self._reply(handler, status, body)
        if method == "GET" and urlsplit(handler.path).path == "/v3/payments":
            return self._reply(handler, 200, self._list(handler.path))
        if method == "GET" and handler.path.startswith("/v3/payments/"):
            pid = handler.path.rsplit("/", 1)[1]
            with self.lock:
                payment = self.payments.get(pid)
                body = json.loads(json.dumps(payment)) if payment else None
            if body is None:
                return self._reply(handler, 404, {"type": "error", "code": "not_found"})
            return self._reply(handler, 200, body)
        return self._reply(handler, 404, {"type": "error", "code": "not_found"})

    def _create(self, headers, raw):
        key = headers.get("idempotence-key")
        if not key or len(key) > 64:
            return 400, {"type": "error", "code": "invalid_request", "parameter": "Idempotence-Key"}
        try:
            data = json.loads(raw)
        except ValueError:
            return 400, {"type": "error", "code": "invalid_request"}
        with self.lock:
            if key in self.idempotence:
                previous, pid = self.idempotence[key]
                if previous != data:
                    return 400, {"type": "error", "code": "invalid_request", "description": "idempotence key reused"}
                body = json.loads(json.dumps(self.payments[pid]))
                body.update(json.loads(json.dumps(self.create_overrides)))
                return 200, body
            amount = data.get("amount") or {}
            if not isinstance(amount.get("value"), str):
                return 400, {"type": "error", "code": "invalid_request", "parameter": "amount.value"}
            pid = self._new_id()
            self.payments[pid] = self._payment_object(pid, amount, data.get("metadata") or {},
                                                      data.get("description", ""))
            self.idempotence[key] = (data, pid)
            body = json.loads(json.dumps(self.payments[pid]))
            body.update(json.loads(json.dumps(self.create_overrides)))
            return 200, body

    def _list(self, path):
        query = {k: v[0] for k, v in parse_qs(urlsplit(path).query).items()}
        gte, lt = query.get("created_at.gte", ""), query.get("created_at.lt", "~")
        limit = int(query.get("limit", "10"))
        start = int(query.get("cursor", "0"))
        with self.lock:
            matching = sorted((p for p in self.payments.values() if gte <= p["created_at"] < lt),
                              key=lambda p: p["created_at"], reverse=True)
            page = json.loads(json.dumps(matching[start:start + limit]))
        body = {"type": "list", "items": page}
        if start + limit < len(matching):
            body["next_cursor"] = str(start + limit)
        return body
