"""Ставка векселей FRED DTB3 для книги базиса (optbot/rates.py) и задача бота."""
import asyncio
import dataclasses
import json
from unittest.mock import AsyncMock

import pytest

from optbot import db, rates
from optbot.fmt import DAY_MS

CSV = "observation_date,DTB3\n2026-11-17,4.15\n2026-11-18,.\n2026-11-19,4.16\n2026-11-20,\n"
D19 = 1795046400000


def test_parse_csv_skips_missing():
    assert rates.parse_csv(CSV) == [(D19 - 2 * DAY_MS, pytest.approx(0.0415)), (D19, pytest.approx(0.0416))]


def test_update_saves_and_falls_back(cfg):
    c = dataclasses.replace(cfg, rf_url="https://x/csv?id=DTB3")
    conn = db.account(c)
    got = rates.update(conn, c, D19 + DAY_MS, fetcher=lambda url, now: (D19, 0.0420))
    assert got.rate.rate == 0.042 and got.rate.source == "fred" and not got.stale and got.error is None
    saved = json.loads(conn.execute("SELECT value FROM market_state WHERE key = ?", (rates.STATE_KEY,)).fetchone()[0])
    assert saved["date_ms"] == D19

    def boom(url, now):
        raise OSError("network down")
    got = rates.update(conn, c, D19 + 3 * DAY_MS, fetcher=boom)                # ошибка — последнее значение
    assert got.rate.rate == 0.042 and "network down" in got.error and not got.stale
    got = rates.update(conn, c, D19 + 8 * DAY_MS, fetcher=boom)                # старше 7 дней — устарела
    assert got.stale and "устарела" in str(got)
    conn.close()


def test_config_fallback_when_never_fetched(cfg):
    conn = db.account(cfg)
    r = rates.current(conn)
    assert r.source == "config" and r.rate == pytest.approx(0.0416) and r.date_ms == D19
    assert rates.update(conn, cfg, D19 + 10 * DAY_MS).stale                     # RF_URL пуст — не берём, но видно
    conn.close()


def test_fetch_requests_last_month(monkeypatch):
    seen = []

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return CSV.encode()
    monkeypatch.setattr(rates.urllib.request, "urlopen", lambda req, timeout: seen.append(req.full_url) or R())
    assert rates.fetch("https://f/csv?id=DTB3", D19 + DAY_MS) == (D19, pytest.approx(0.0416))
    assert seen == ["https://f/csv?id=DTB3&cosd=2026-10-21"]
    monkeypatch.setattr(rates, "parse_csv", lambda t: [(D19, 0.9)])
    with pytest.raises(ValueError):
        rates.fetch("https://f/csv", D19)


def test_bot_rf_job_alerts_when_stale(cfg, make_client, monkeypatch):
    from optbot.bot import OptionsBot
    bot = OptionsBot(make_client(cfg), cfg)
    alerts = []
    monkeypatch.setattr(bot, "admin_alert", AsyncMock(side_effect=lambda key, text, **k: alerts.append(key)))
    monkeypatch.setattr(rates, "update", lambda conn, c: rates.UpdateResult(rates.Rate(0.04, 0, "fred"), "x", True))
    asyncio.run(bot._rf_job(None))
    assert alerts == ["rf_stale"]
    monkeypatch.setattr(rates, "update", lambda conn, c: rates.UpdateResult(rates.Rate(0.04, 0, "fred")))
    asyncio.run(bot._rf_job(None))
    assert alerts == ["rf_stale"]


def test_rf_url_must_be_https(cfg):
    from optbot.bot import check_config
    assert any("RF_URL" in e for e in check_config(dataclasses.replace(cfg, rf_url="http://x")))


def test_newer_config_rate_wins_over_old_fred(cfg, monkeypatch):
    conn = db.account(cfg)
    with conn:
        conn.execute("INSERT INTO market_state (key, value) VALUES (?, ?)",
                     (rates.STATE_KEY, json.dumps({"rate": 0.05, "date_ms": D19 - 30 * DAY_MS})))
    assert rates.current(conn).source == "config"                                # config 2026-11-19 свежее
    with conn:
        conn.execute("UPDATE market_state SET value = ? WHERE key = ?",
                     (json.dumps({"rate": 0.039, "date_ms": D19 + DAY_MS}), rates.STATE_KEY))
    assert rates.current(conn).rate == 0.039
    conn.close()


def test_fred_used_when_config_broken(cfg, monkeypatch):
    conn = db.account(cfg)
    with conn:
        conn.execute("INSERT INTO market_state (key, value) VALUES (?, ?)",
                     (rates.STATE_KEY, json.dumps({"rate": 0.04, "date_ms": D19})))

    def broken():
        raise ValueError("bad json")
    monkeypatch.setattr(rates, "_config_rate", broken)
    assert rates.current(conn).rate == 0.04
    with conn:
        conn.execute("DELETE FROM market_state")
    with pytest.raises(ValueError):
        rates.current(conn)
    conn.close()
