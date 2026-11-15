"""orderLinkId: по нему журнал узнаёт источник ордера (Bybit: ≤ 36 символов, уникален на каждый ордер).

    man-u<хеш6>-<nonce20>         ручной ордер (бот или CLI)
    sig<ID>-l<N>-u<хеш6>-<попытка>  ордер по сигналу (этап 3)
    ob-<hex24>                    ордера бота до журнала (legacy)
    всё остальное                 external — сайт, приложение, другие программы

хеш6 = HMAC-SHA256(секрет, user_id)[:6]: id пользователя Telegram на биржу не уходит.
Полное соответствие (user_id, имя, чат, сигнал) — в таблице order_links.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import uuid

MAX_LEN = 36
NO_USER = "000000"  # CLI и скрипты без пользователя Telegram

_MAN = re.compile(r"man-u([0-9a-f]{6})-[0-9a-f]+")
_SIG = re.compile(r"sig(\d+)-l(\d+)-u([0-9a-f]{6})-(\d+)")


def user_hash(secret: bytes, user_id: int | None) -> str:
    if user_id is None:
        return NO_USER
    return hmac.new(secret, str(user_id).encode(), hashlib.sha256).hexdigest()[:6]


def new_link_id(user_tag: str = NO_USER) -> str:
    return f"man-u{user_tag}-{uuid.uuid4().hex[:20]}"


def signal_link_id(signal_id: int, leg: int, user_tag: str, attempt: int) -> str:
    link = f"sig{signal_id}-l{leg}-u{user_tag}-{attempt}"
    if len(link) > MAX_LEN:
        raise ValueError(f"orderLinkId длиннее {MAX_LEN}: {link}")
    return link


def parse_link_id(link: str | None) -> dict:
    """{'source': manual|signal|legacy|external, 'user_hash', 'signal_id', 'leg'}."""
    link = link or ""
    if m := _MAN.fullmatch(link):
        return {"source": "manual", "user_hash": m[1]}
    if m := _SIG.fullmatch(link):
        return {"source": "signal", "signal_id": int(m[1]), "leg": int(m[2]), "user_hash": m[3]}
    if link.startswith("ob-"):
        return {"source": "legacy"}
    return {"source": "external"}
