"""Общие фикстуры. Сеть не используется: Bybit и Telegram подменены.

Переменные окружения задаются ДО импорта optbot: Settings читает их при импорте,
а load_dotenv не перезаписывает уже заданные — так тесты не зависят от локального .env.
"""
import os
import tempfile

os.environ.update(
    # своя временная БД: тест не может записать подставные данные в настоящую data/market-<env>.db
    DB_PATH=os.path.join(tempfile.mkdtemp(prefix="optbot-test-"), "market.db"),
    COLLECTOR_ENABLED="true", COLLECTOR_INTERVAL_MIN="15",
    BYBIT_ENV="demo", BYBIT_API_KEY="test-key", BYBIT_API_SECRET="test-secret",
    SETTLE_COIN="USDT", BASE_COINS="BTC,ETH,SOL",
    MAX_QTY_BTC="1", MAX_QTY_ETH="10", MAX_QTY_SOL="100",
    MAX_ORDER_PREMIUM_USDT="500", MAX_SLIPPAGE_PCT="15", MAX_SPREAD_PCT="20",
    MAX_POSITION_BTC="0", MAX_POSITION_ETH="0", MAX_POSITION_SOL="0",
    MAX_NET_DELTA_BTC="0", MAX_NET_DELTA_ETH="0", MAX_NET_DELTA_SOL="0",
    MAX_DAILY_PREMIUM_USDT="0",
    TELEGRAM_BOT_TOKEN="123:test", TELEGRAM_ALLOWED_CHAT_IDS="-100111,-100222",
    TELEGRAM_TRADER_IDS="10", TELEGRAM_VIEWER_IDS="20",
    ALLOW_ALL_GROUP_MEMBERS_VIEW="false", SENSITIVE_COMMANDS_DM_ONLY="true",
    FNG_URL="",  # без сети: индекс страха и жадности в тестах подменяется
)

import dataclasses  # noqa: E402
import time  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

from optbot import bybit_client  # noqa: E402
from optbot.bybit_client import OptionsClient  # noqa: E402
from optbot.config import Settings  # noqa: E402
from tests.fakes import FakeHTTP  # noqa: E402


@pytest.fixture
def cfg(tmp_path) -> Settings:
    # своя БД на тест: журнал и сборщик не делят данные между тестами
    return dataclasses.replace(Settings(), db_path=str(tmp_path / "market.db"))


@pytest.fixture
def fake() -> FakeHTTP:
    return FakeHTTP()


@pytest.fixture
def make_client(fake):
    def make(cfg: Settings) -> OptionsClient:
        c = OptionsClient(cfg)
        c.http = fake
        return c
    return make


@pytest.fixture
def client(make_client, cfg) -> OptionsClient:
    return make_client(cfg)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Опрос статуса после сбоя связи спит между попытками — в тестах мгновенно.
    Подменяется только ссылка на модуль time внутри клиента, глобальный time.sleep не трогаем."""
    monkeypatch.setattr(bybit_client, "time", SimpleNamespace(time=time.time, sleep=lambda s: None))


@pytest.fixture(autouse=True)
def fixed_day_start(monkeypatch):
    """Начало суток UTC = час назад: тесты суточной премии не зависят от запуска около полуночи."""
    start = int(time.time() * 1000) - 3600_000
    monkeypatch.setattr(bybit_client.OptionsClient, "_day_start_ms", staticmethod(lambda: start))
