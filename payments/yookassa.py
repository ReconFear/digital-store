"""Адаптер ЮKassa (API v3) — настоящие HTTP-запросы, только стандартная библиотека.

Протокол (документация ЮKassa и официальный Python SDK, проверено 2026-10-13):
* https://api.yookassa.ru/v3/, HTTP Basic: shopId:секретный ключ.
* POST /payments с заголовком Idempotence-Key (до 64 символов, мы используем UUID v4).
  Повтор с тем же ключом и теми же данными в течение 24 часов возвращает ТОТ ЖЕ платёж.
* GET /payments/{id} — текущее состояние платежа.
* Ответ 202 — запрос ещё обрабатывается, повторить позже с тем же ключом.
* Ответ 500 — результат НЕИЗВЕСТЕН; повтор с тем же ключом безопасен.
* Статусы: pending, waiting_for_capture, succeeded (конечный), canceled (конечный).

Что здесь гарантируется:
* Сумма передаётся строкой, вычисленной из целых минимальных единиц без float.
* Ответ 200 на создание НЕ означает оплату: возвращается ProviderPayment со статусом
  провайдера (обычно pending); оплату учитывает только сервис по статусу succeeded.
* Повторы — только при неизвестном результате (сеть, таймаут, 202, 429, 5xx) и всегда
  с тем же ключом идемпотентности. Если повторы исчерпаны — ProviderUnavailable.
* HTTP-перенаправления (3xx) не выполняются никогда: иначе urllib повторил бы запрос по
  Location вместе с Authorization и Idempotence-Key. 3xx = «результат неизвестен» (rev4).
* TLS с проверкой сертификата (не ниже 1.2); http разрешён только для localhost
  (локальный имитатор в тестах).
* Секретный ключ не попадает в исключения, логи и repr.
"""

import base64
import json
import logging
import ssl
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode, urlsplit

from payments.config import is_loopback_host
from payments.provider import (
    PROVIDER_ID_RE,
    PROVIDER_STATUSES,
    ProviderAuthError,
    ProviderNotFound,
    ProviderPayment,
    ProviderProtocolError,
    ProviderRejected,
    ProviderUnavailable,
    decimal_string_to_minor,
    minor_to_decimal_string,
)

log = logging.getLogger("digital_store.payments.yookassa")

MAX_RESPONSE_BYTES = 1_000_000
MAX_DESCRIPTION = 128  # ограничение ЮKassa на description
RETRYABLE_STATUSES = frozenset({202, 429, 500, 502, 503, 504})
MAX_LIST_PAGES = 50  # до 5000 платежей в окне поиска одной попытки


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Запрет автоматических перенаправлений (rev4).

    Стандартный HTTPRedirectHandler повторяет запрос по адресу из Location и переносит туда
    заголовки, включая Authorization (shopId:секретный ключ) и Idempotence-Key — в том числе
    на другой домен. Здесь перенаправление никогда не выполняется: ответ 3xx возвращается
    вызывающему коду как HTTPError и обрабатывается как «результат неизвестен».
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _endpoint(path):
    """Имя ресурса для журнала: без ID и параметров запроса."""
    return path.split("/")[1].split("?")[0]


class YooKassaProvider:
    name = "yookassa"

    def __init__(self, *, shop_id, secret_key, api_url, timeout_seconds=10.0, max_attempts=3,
                 sleep=time.sleep):
        parts = urlsplit(api_url)
        if parts.scheme != "https" and not (parts.scheme == "http" and is_loopback_host(parts.hostname or "")):
            raise ValueError("адрес API ЮKassa должен быть https://")
        self._shop_id = shop_id
        self._auth = "Basic " + base64.b64encode(f"{shop_id}:{secret_key}".encode()).decode()
        self._api_url = api_url.rstrip("/")
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts
        self._sleep = sleep
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        # _NoRedirect заменяет стандартный обработчик перенаправлений (build_opener не добавляет
        # свой, если передан наследник HTTPRedirectHandler).
        handlers = [urllib.request.HTTPSHandler(context=context), _NoRedirect()]
        if parts.scheme == "http":
            handlers.append(urllib.request.ProxyHandler({}))  # локальный имитатор — без прокси
        self._opener = urllib.request.build_opener(*handlers)

    @classmethod
    def from_settings(cls, settings, **kwargs):
        return cls(shop_id=settings.shop_id, secret_key=settings.secret_key, api_url=settings.api_url,
                   timeout_seconds=settings.timeout_seconds, max_attempts=settings.max_attempts, **kwargs)

    def __repr__(self):
        return f"YooKassaProvider(shop_id={self._shop_id!r}, api_url={self._api_url!r})"

    # ---------- публичные операции ----------

    def create_payment(self, *, idempotence_key, amount_minor, currency, description, return_url, metadata):
        key = str(idempotence_key)
        if not 1 <= len(key) <= 64:
            raise ValueError("ключ идемпотентности: 1..64 символа")
        body = {
            "amount": {"value": minor_to_decimal_string(amount_minor, currency), "currency": currency},
            "capture": True,  # одностадийная оплата: succeeded без отдельного подтверждения
            "confirmation": {"type": "redirect", "return_url": return_url},
            "description": description[:MAX_DESCRIPTION],
            "metadata": {str(k): str(v) for k, v in metadata.items()},
        }
        data = self._request("POST", "/payments", body=body, idempotence_key=key)
        return self._parse_payment(data)

    def get_payment(self, provider_payment_id):
        if not isinstance(provider_payment_id, str) or not PROVIDER_ID_RE.match(provider_payment_id):
            raise ProviderNotFound("недопустимый ID платежа")
        data = self._request("GET", "/payments/" + quote(provider_payment_id, safe=""))
        return self._parse_payment(data)

    def find_payment_by_metadata(self, *, payment_attempt, order_id, created_gte, created_lt):
        """Найти платёж попытки в списке платежей магазина (GET /v3/payments, created_at.gte/lt).

        Нужен, когда ответ на создание платежа потерян, а повторять запрос с тем же ключом
        уже нельзя (ключ ЮKassa живёт 24 часа). Возвращает ProviderPayment или None, если в
        окне ни одного платежа с metadata.payment_attempt = payment_attempt и order_id = order_id.
        Если список не удалось просмотреть ЦЕЛИКОМ — ProviderUnavailable (вывод «платежа нет»
        по неполному списку делать нельзя). created_gte/created_lt — строки ISO 8601 UTC.
        """
        attempt, order = str(payment_attempt), str(order_id)
        params = {"created_at.gte": created_gte, "created_at.lt": created_lt, "limit": 100}
        for _ in range(MAX_LIST_PAGES):
            data = self._request("GET", "/payments?" + urlencode(params))
            items = data.get("items")
            if data.get("type") != "list" or not isinstance(items, list):
                raise ProviderProtocolError("ответ на список платежей — не список")
            for item in items:
                metadata = item.get("metadata") if isinstance(item, dict) else None
                if not isinstance(metadata, dict) or str(metadata.get("payment_attempt")) != attempt:
                    continue
                payment = self._parse_payment(item)   # наш платёж обязан быть корректным
                if payment.metadata.get("order_id") == order:
                    return payment
            cursor = data.get("next_cursor")
            if not cursor:
                return None
            if not isinstance(cursor, str) or len(cursor) > 512:
                raise ProviderProtocolError("неверный next_cursor")
            params["cursor"] = cursor
        raise ProviderUnavailable("список платежей слишком длинный: просмотрен не полностью")

    # ---------- HTTP ----------

    def _request(self, method, path, *, body=None, idempotence_key=None):
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Authorization": self._auth, "Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if idempotence_key is not None:
            headers["Idempotence-Key"] = idempotence_key

        last_problem = "нет попыток"
        for attempt in range(1, self._max_attempts + 1):
            request = urllib.request.Request(self._api_url + path, data=payload, headers=headers, method=method)
            delay = min(0.5 * 2 ** (attempt - 1), 4.0)
            try:
                with self._opener.open(request, timeout=self._timeout) as response:
                    status = response.status
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
            except urllib.error.HTTPError as error:
                status = error.code
                try:
                    raw = error.read(MAX_RESPONSE_BYTES + 1)
                except OSError:
                    raw = b""
                finally:
                    error.close()
            except (TimeoutError, OSError, urllib.error.URLError) as error:
                # Таймаут или обрыв: запрос мог дойти — результат неизвестен.
                last_problem = f"сеть: {type(error).__name__}"
                log.warning("ЮKassa %s %s: %s (попытка %d из %d)", method, _endpoint(path), last_problem,
                            attempt, self._max_attempts)
                if attempt < self._max_attempts:
                    self._sleep(delay)
                continue

            if len(raw) > MAX_RESPONSE_BYTES:
                raise ProviderProtocolError("слишком большой ответ провайдера")

            if status == 200:
                return self._decode(raw)
            if 300 <= status < 400:
                # Перенаправление не выполняется и не повторяется (секрет не уходит по Location).
                # Для POST это НЕ отказ: запрос мог быть обработан, поэтому результат неизвестен —
                # попытка останется unknown и повторится позже с тем же ключом идемпотентности.
                log.error("ЮKassa %s %s: неожиданное перенаправление HTTP %d — не выполняется", method,
                          _endpoint(path), status)
                raise ProviderUnavailable(f"результат запроса к ЮKassa неизвестен: перенаправление HTTP {status}")
            if status in RETRYABLE_STATUSES:
                last_problem = f"HTTP {status}"
                if status == 202:
                    delay = self._retry_after(raw, delay)
                log.warning("ЮKassa %s %s: HTTP %d (попытка %d из %d)", method, _endpoint(path), status,
                            attempt, self._max_attempts)
                if attempt < self._max_attempts:
                    self._sleep(delay)
                continue
            if status in (401, 403):
                raise ProviderAuthError(f"ЮKassa не приняла учётные данные магазина (HTTP {status})")
            if status == 404:
                raise ProviderNotFound("объект не найден у провайдера")
            raise ProviderRejected(self._error_code(raw), status)

        raise ProviderUnavailable(f"результат запроса к ЮKassa неизвестен: {last_problem}")

    @staticmethod
    def _decode(raw):
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ProviderProtocolError("ответ провайдера — не JSON") from None
        if not isinstance(data, dict):
            raise ProviderProtocolError("ответ провайдера — не объект")
        return data

    @staticmethod
    def _retry_after(raw, default):
        try:
            value = json.loads(raw.decode("utf-8")).get("retry_after")
        except (UnicodeDecodeError, ValueError, AttributeError):
            return default
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return min(value / 1000.0, 5.0)  # миллисекунды, не дольше 5 секунд
        return default

    @staticmethod
    def _error_code(raw):
        try:
            code = json.loads(raw.decode("utf-8")).get("code")
        except (UnicodeDecodeError, ValueError, AttributeError):
            return "unknown"
        return code if isinstance(code, str) and len(code) <= 64 and code.replace("_", "").isalnum() else "unknown"

    # ---------- разбор объекта платежа (ответу доверяем только после проверки формата) ----------

    @staticmethod
    def _parse_payment(data):
        payment_id = data.get("id")
        status = data.get("status")
        amount = data.get("amount")
        test = data.get("test")
        paid = data.get("paid")
        if not isinstance(payment_id, str) or not PROVIDER_ID_RE.match(payment_id):
            raise ProviderProtocolError("нет корректного id платежа")
        if status not in PROVIDER_STATUSES:
            raise ProviderProtocolError("неизвестный статус платежа")
        if not isinstance(amount, dict) or not isinstance(amount.get("currency"), str):
            raise ProviderProtocolError("нет суммы платежа")
        if not isinstance(test, bool) or not isinstance(paid, bool):
            raise ProviderProtocolError("нет признаков test/paid")
        currency = amount["currency"]
        amount_minor = decimal_string_to_minor(amount.get("value"), currency)

        recipient = data.get("recipient")
        account_id = recipient.get("account_id") if isinstance(recipient, dict) else None
        if account_id is not None and not isinstance(account_id, str):
            account_id = str(account_id)

        # Ссылка оплаты возвращается как есть (ограничена по длине); допустима ли она для
        # покупателя, решает сервис (https, разрешённый домен) — до выдачи покупателю.
        confirmation = data.get("confirmation")
        confirmation_url = None
        if isinstance(confirmation, dict):
            url = confirmation.get("confirmation_url")
            if isinstance(url, str) and len(url) <= 4096:
                confirmation_url = url

        cancellation = data.get("cancellation_details")
        reason = None
        if isinstance(cancellation, dict) and isinstance(cancellation.get("reason"), str):
            reason = cancellation["reason"][:64]

        metadata = data.get("metadata")
        metadata = {str(k): str(v) for k, v in metadata.items()} if isinstance(metadata, dict) else {}

        return ProviderPayment(
            id=payment_id, status=status, amount_minor=amount_minor, currency=currency, test=test,
            paid=paid, account_id=account_id, confirmation_url=confirmation_url,
            cancellation_reason=reason, metadata=metadata,
        )
