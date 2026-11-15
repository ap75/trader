"""Приватный WebSocket Bybit: исполнения (execution) и состояние ордеров (order) по опционам.

pybit держит соединение в своём потоке, сам переподключается и шлёт ping.
Колбэк вызывается из потока pybit — в asyncio его нужно передавать через call_soon_threadsafe.
"""
from __future__ import annotations

import logging
from typing import Callable

from pybit.unified_trading import WebSocket

from .config import Settings

log = logging.getLogger("optbot.ws")


class ExecutionWatcher:
    def __init__(self, cfg: Settings, on_executions: Callable[[list[dict]], None],
                 on_orders: Callable[[list[dict]], None] | None = None):
        self.cfg = cfg
        self.on_executions = on_executions
        self.on_orders = on_orders
        self.ws: WebSocket | None = None

    def start(self) -> None:
        """Подключение и подписка (блокирует до установления соединения — вызывать из потока)."""
        self.ws = WebSocket(
            channel_type="private",
            testnet=self.cfg.env == "testnet",
            demo=self.cfg.env == "demo",
            api_key=self.cfg.api_key,
            api_secret=self.cfg.api_secret,
            restart_on_error=True,
            trace_logging=False,
        )
        self.ws.execution_stream(callback=self._on_message)
        log.info("WebSocket исполнений подключён (%s)", self.cfg.env)
        if self.on_orders:
            try:  # без топика order журнал всё равно догрузит ордера сверкой — уведомления не терять
                self.ws.order_stream(callback=self._on_order_message)
            except Exception:
                log.warning("WebSocket: подписка на ордера не удалась — состояние ордеров только сверкой",
                            exc_info=True)

    @staticmethod
    def _options(msg: dict) -> list[dict]:
        return [r for r in msg.get("data", []) if r.get("category") == "option"]

    def _on_message(self, msg: dict) -> None:
        try:
            if rows := self._options(msg):
                self.on_executions(rows)
        except Exception:
            log.exception("Ошибка обработки сообщения WebSocket")

    def _on_order_message(self, msg: dict) -> None:
        try:
            if (rows := self._options(msg)) and self.on_orders:
                self.on_orders(rows)
        except Exception:
            log.exception("Ошибка обработки сообщения WebSocket (order)")

    def stop(self) -> None:
        if self.ws:
            try:
                self.ws.exit()
            except Exception:
                log.debug("ws.exit()", exc_info=True)
