"""Email покупателя на HTTP-входе (PR6 этап 2d, решение владельцев): в PR6 — только ASCII.

  1. строка не длиннее MAX_RAW_EMAIL_CHARS (до обрезки — дешёвая граница);
  2. пробельные символы ASCII по краям обрезаются (копирование адреса); внутри адреса пробелов нет;
  3. остаток — только печатные символы ASCII (U+0021…U+007E). Проверка ДО перевода в нижний регистр:
     иначе символы вроде KELVIN SIGN (U+212A) стали бы ASCII-буквой «k» и создали бы двойника адреса;
  4. перевод в нижний регистр (для ASCII — только A–Z);
  5. SQL-совместимый контракт accounts.service.check_email_norm (≤ 254, шаблон базы целиком, .invalid
     зарезервирован).

Unicode-адреса, NFKC и IDNA в PR6 не поддерживаются. Правил провайдеров нет: точки и «+tag» не меняются.
Регистрация и вход нормализуют одной функцией: уникальность email_norm и ключ счётчика попыток в базе
(sha256('email:' || email_norm)) общие для всех написаний одного адреса.

Ошибка — AccountInputError с кодом email_invalid или email_reserved (HTTP 422); введённое значение
в сообщение и repr не попадает.
"""

import re

from accounts.service import AccountInputError, check_email_norm

MAX_RAW_EMAIL_CHARS = 320
ASCII_WHITESPACE = " \t\r\n\f\v"
_PRINTABLE_ASCII_RE = re.compile(r"[\x21-\x7e]+")


def normalize_email(raw):
    """Введённый адрес -> email_norm для функций базы."""
    if not isinstance(raw, str) or len(raw) > MAX_RAW_EMAIL_CHARS:
        raise AccountInputError("email_invalid")
    trimmed = raw.strip(ASCII_WHITESPACE)
    if not _PRINTABLE_ASCII_RE.fullmatch(trimmed):
        raise AccountInputError("email_invalid")
    return check_email_norm(trimmed.lower())
