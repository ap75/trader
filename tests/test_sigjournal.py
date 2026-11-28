"""Журнал сигналов (этап 4): запись выдачи, итог после экспирации, путь по leg_marks, статистика, бот и CLI."""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest
import requests

from optbot import cli, db, sigjournal
from optbot.bot import OptionsBot
from optbot.fmt import DAY_MS
from optbot.journal import Journal
from optbot.signals import Leg, SignalEngine, payoff
from optbot.sigjournal import SignalJournal, compute_stats, fmt_settled, fmt_stats, legs_key, path_stats
from tests.fakes import EXP, EXP_MS, NOW, set_chain, set_klines
from tests.test_bot import TG, TRADER, VIEWER, STRANGER


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def market(fake):
    set_chain(fake, iv=0.6)
    set_klines(fake, rv=0.4)
    return fake


@pytest.fixture
def sig(make_client, cfg, market):
    s = SignalEngine(make_client(cfg), cfg).compute("BTC", NOW)
    assert s.strategy == "iron_condor" and s.legs and s.expiry_ms == EXP_MS and s.regime == "high"
    return s


def q(cfg, sql, *a):
    conn = db.account(cfg)
    try:
        return [dict(r) for r in conn.execute(sql, a)]
    finally:
        conn.close()


# ---------- запись ----------
def test_record_and_track_legs(cfg, sig):
    sj = SignalJournal(cfg)
    sid = sj.record(sig, "bot", TRADER, "user10", -100111, now_ms=NOW)
    r = q(cfg, "SELECT * FROM signals WHERE id = ?", sid)[0]
    assert (r["base"], r["strategy"], r["regime"], r["trend"], r["expiry"]) == ("BTC", "iron_condor", "high", "flat", EXP)
    assert r["net"] == pytest.approx(sig.net) and r["max_loss"] == pytest.approx(sig.profile.max_loss)
    assert r["user_id"] == TRADER and r["source"] == "bot" and r["shown"] == 1 and r["cfg_hash"]
    assert json.loads(r["metrics"])["iv7"] == pytest.approx(0.6)
    assert [d["symbol"] for d in json.loads(r["legs"])] == [l.symbol for l in sig.legs]
    tracked = q(cfg, "SELECT symbol, until_ms, reason FROM tracked_symbols")
    assert {t["symbol"] for t in tracked} == {l.symbol for l in sig.legs}
    assert all(t["until_ms"] == EXP_MS and t["reason"] == f"demo:sig:{sid}" for t in tracked)


def test_evaluate_restores_lost_tracking(cfg, sig):
    sj = SignalJournal(cfg)
    sid = sj.record(sig, now_ms=NOW)
    conn = db.account(cfg)
    with conn:                                             # сбой между записью сигнала и слежения
        conn.execute("DELETE FROM tracked_symbols")
    conn.close()
    sj.evaluate(now_ms=NOW + 1000)
    tracked = q(cfg, "SELECT symbol, until_ms, reason FROM tracked_symbols")
    assert {t["symbol"] for t in tracked} == {l.symbol for l in sig.legs}
    assert all(t["until_ms"] == EXP_MS and t["reason"] == f"demo:sig:{sid}" for t in tracked)


def test_record_same_trade_once(cfg, sig, make_client, market):
    sj = SignalJournal(cfg)
    a = sj.record(sig, user_id=1, now_ms=NOW)
    b = sj.record(sig, user_id=2, now_ms=NOW + 3600_000)          # другой человек — новый показ
    assert a == b and sj.record(sig, user_id=2, now_ms=NOW + 7200_000) == a   # тот же снова — не показ
    r = q(cfg, "SELECT shown, ts, last_shown_ms FROM signals")
    assert len(r) == 1 and r[0]["shown"] == 2 and r[0]["ts"] == NOW and r[0]["last_shown_ms"] == NOW + 3600_000
    assert sj.record(sig, user_id=2, now_ms=NOW + DAY_MS + 3600_000) == a   # через сутки — снова показ
    assert q(cfg, "SELECT shown FROM signals")[0]["shown"] == 3
    set_chain(market, iv=0.6, F=67000)   # рынок ушёл — другие страйки, другая сделка
    other = SignalEngine(make_client(cfg), cfg).compute("BTC", NOW)
    assert legs_key(other.legs) != legs_key(sig.legs)
    assert sj.record(other, now_ms=NOW) != a


def test_record_after_expiry_is_new_row(cfg, sig):
    sj = SignalJournal(cfg)
    a = sj.record(sig, now_ms=NOW)
    assert sj.record(sig, now_ms=EXP_MS + 1) != a  # та же «сделка» после экспирации — уже не открыта


def test_record_without_legs(cfg, sig):
    sig.legs = []
    assert SignalJournal(cfg).record(sig) is None
    assert q(cfg, "SELECT COUNT(*) n FROM signals")[0]["n"] == 0


# ---------- путь ----------
def L(sym, kind, k, side, price, qty=1.0):
    return Leg(symbol=sym, kind=kind, strike=k, side=side, qty=qty, bid=price, ask=price)


def test_path_stats_drawdown_and_half():
    legs = [L("S", "P", 60000, -1, 300), L("B", "P", 58000, +1, 100)]  # кредит 200
    m = lambda ts, s, mark, bid, ask: dict(ts=ts, symbol=s, mark=mark, bid=bid, ask=ask)  # noqa: E731
    marks = [
        m(1, "S", 350, 340, 360), m(1, "B", 110, 105, 115),   # закрыть: −(360−300) + (105−100) = −55
        m(2, "S", 500, 490, 510),                              # нет второй ноги — срез пропускается
        m(3, "S", 200, 190, 0), m(3, "B", 80, 75, 85),         # нет ask у короткой — исполнимо нельзя, mark можно
        m(4, "S", 150, 140, 160), m(4, "B", 60, 55, 65),       # −(160−300) + (55−100) = +95
    ]
    ps = path_stats(legs, marks, target=100)
    assert ps.n == 3
    assert ps.max_dd == pytest.approx(-(360 - 300) + (105 - 100))         # −55
    assert ps.max_dd_mark == pytest.approx(-(350 - 300) + (110 - 100))    # −40
    assert ps.half_ms is None   # лучший исполнимый итог +95 < 100


def test_path_stats_half_exact():
    legs = [L("S", "P", 60000, -1, 300), L("B", "P", 58000, +1, 100)]
    marks = [dict(ts=1, symbol="S", mark=200, bid=190, ask=200), dict(ts=1, symbol="B", mark=100, bid=100, ask=110),
             dict(ts=2, symbol="S", mark=150, bid=140, ask=150), dict(ts=2, symbol="B", mark=50, bid=50, ask=60)]
    # ts1: −(200−300) + (100−100) = +100 ≥ 100 → половина достигнута на ts1
    assert path_stats(legs, marks, target=100).half_ms == 1
    assert path_stats(legs, marks, target=150).half_ms is None  # ts2: 150 − 50 = 100
    assert path_stats(legs, [], target=1).n == 0


# ---------- итог ----------
def _set_price(cfg, price, base="BTC", t=EXP_MS):
    Journal(cfg).record_delivery_prices(base, [{"deliveryPrice": str(price), "deliveryTime": str(t)}])


def test_evaluate_settles_after_expiry(cfg, sig):
    sj = SignalJournal(cfg)
    sid = sj.record(sig, now_ms=NOW)
    _set_price(cfg, 65000)
    assert sj.evaluate(now_ms=EXP_MS - 1).settled == 0              # до экспирации — нет
    assert sj.evaluate(now_ms=EXP_MS + 10 * 60_000).settled == 0     # цена ещё может публиковаться
    res = sj.evaluate(now_ms=EXP_MS + 3600_000)
    assert res.settled == 1
    r = q(cfg, "SELECT * FROM signals WHERE id = ?", sid)[0]
    assert r["delivery_price"] == 65000 and r["pnl"] == pytest.approx(sig.net)   # внутри кондора — весь кредит
    assert r["r_mult"] == pytest.approx(sig.net / sig.profile.max_loss)
    assert r["pnl_mid"] == pytest.approx(sig.net_mid)
    assert sj.evaluate(now_ms=EXP_MS + 7200_000).settled == 0        # повторно не считается


def test_evaluate_loss_and_path(cfg, sig):
    sj = SignalJournal(cfg)
    sid = sj.record(sig, now_ms=NOW)
    conn = db.account(cfg)
    with conn:
        for l in sig.legs:  # один срез котировок: цены как при входе — PnL на закрытие = −спред
            conn.execute("INSERT INTO leg_marks (ts, symbol, mark, bid, ask) VALUES (?,?,?,?,?)",
                         (NOW + 900_000, l.symbol, l.mark, l.bid, l.ask))
    conn.close()
    _set_price(cfg, 50000)  # глубоко ниже пут-крыла — макс. убыток
    sj.evaluate(now_ms=EXP_MS + 3600_000)
    r = q(cfg, "SELECT * FROM signals WHERE id = ?", sid)[0]
    assert r["pnl"] == pytest.approx(payoff(sig.legs, 50000)) and r["pnl"] < 0
    assert r["r_mult"] == pytest.approx(-1, abs=0.35)  # кондор: худшая сторона уже — R ≥ −1
    assert r["marks_n"] == 1 and r["max_dd"] < 0 and r["half_ms"] is None


def test_evaluate_fetches_missing_price(cfg, sig, make_client, fake):
    sj = SignalJournal(cfg)
    sj.record(sig, now_ms=NOW)
    client = make_client(cfg)
    fake.fail["get_new_delivery_price"] = requests.exceptions.ConnectionError("down")
    res = sj.evaluate(client, now_ms=EXP_MS + 3600_000)
    assert res.settled == 0 and res.waiting == 1 and res.errors
    fake.delivery_px["BTC"] = [{"deliveryPrice": "66000", "deliveryTime": str(EXP_MS)}]
    res = sj.evaluate(client, now_ms=EXP_MS + 3600_000)
    assert res.settled == 1 and "итогов 1" in str(res)
    assert q(cfg, "SELECT price FROM delivery_prices")[0]["price"] == 66000  # заодно — журналу сделок


def test_evaluate_gives_up_without_price(cfg, sig, make_client, fake, caplog):
    sj = SignalJournal(cfg)
    sid = sj.record(sig, now_ms=NOW)
    assert sj.evaluate(make_client(cfg), now_ms=EXP_MS + DAY_MS).waiting == 1        # ещё ждём
    assert sj.evaluate(make_client(cfg), now_ms=EXP_MS + 4 * DAY_MS).void == 0       # пустой ответ биржи — ждём
    fake.delivery_px["BTC"] = [{"deliveryPrice": "70000", "deliveryTime": str(EXP_MS + 2 * DAY_MS)}]  # других дат
    res = sj.evaluate(make_client(cfg), now_ms=EXP_MS + 4 * DAY_MS)
    assert (res.void, res.waiting, res.settled) == (1, 0, 0) and "без итога" in str(res)
    assert "закрыт без итога" in caplog.text
    n = len(fake.called("get_new_delivery_price"))
    assert sj.evaluate(make_client(cfg), now_ms=EXP_MS + 5 * DAY_MS).void == 0          # больше не трогается
    assert len(fake.called("get_new_delivery_price")) == n
    st = compute_stats(sj.rows(0, 10 ** 15), "30 дн.")
    assert (st.total.n, st.total.settled, st.total.void) == (1, 0, 1)
    assert "без цены поставки 1" in fmt_stats(st) and "ждут экспирации 0" in fmt_stats(st)
    assert q(cfg, "SELECT pnl FROM signals WHERE id = ?", sid)[0]["pnl"] is None


def test_settle_only_once_across_runs(cfg, sig):
    """Параллельный --evaluate уже посчитал строку — второй проход её не объявляет."""
    sj = SignalJournal(cfg)
    sj.record(sig, now_ms=NOW)
    _set_price(cfg, 65000)
    row = q(cfg, "SELECT * FROM signals")[0]
    conn = db.account(cfg)
    with conn:
        assert SignalJournal._settle(conn, row, 65000, EXP_MS + 1) is True
        assert SignalJournal._settle(conn, row, 65000, EXP_MS + 2) is False
    conn.close()


def test_record_new_row_for_other_qty_or_thresholds(cfg, sig):
    import dataclasses
    sj = SignalJournal(cfg)
    a = sj.record(sig, now_ms=NOW)
    for l in sig.legs:
        l.qty = 0.02
    sig.qty = 0.02
    assert sj.record(sig, now_ms=NOW) != a                       # другой размер — другая запись
    cfg2 = dataclasses.replace(cfg, sig=dataclasses.replace(cfg.sig, wing_em=0.7))
    assert SignalJournal(cfg2).record(sig, now_ms=NOW) not in (a, None)  # другие пороги — тоже


def test_path_stats_null_mark_is_zero():
    """Пустой mark ноги в срезе — ноль (сборщик пишет 0 как NULL), а не пропуск среза."""
    legs = [L("S", "P", 60000, -1, 300), L("B", "P", 58000, +1, 100)]
    marks = [dict(ts=1, symbol="S", mark=600, bid=590, ask=610), dict(ts=1, symbol="B", mark=None, bid=None, ask=5)]
    ps = path_stats(legs, marks, target=None)
    assert ps.max_dd_mark == pytest.approx(-(600 - 300) + (0 - 100))   # −400: худший момент не потерян
    assert ps.max_dd == pytest.approx(-(610 - 300) + (0 - 100))


# ---------- статистика ----------
def _settled_rows(cfg, sig, prices):
    """Несколько сигналов с итогами: один и тот же набор ног, разные экспирации (разные строки)."""
    sj = SignalJournal(cfg)
    for i, p in enumerate(prices):
        sig.expiry_ms = EXP_MS + i * DAY_MS
        sj.record(sig, now_ms=NOW + i)
        conn = db.account(cfg)
        with conn:  # ключ ног тот же — открытая строка была бы переиспользована; закрываем её сразу
            conn.execute("UPDATE signals SET legs_key = legs_key || ? WHERE legs_key NOT LIKE '%#%'", (f"#{i}",))
        conn.close()
        _set_price(cfg, p, t=sig.expiry_ms)
    sj.evaluate(now_ms=EXP_MS + 10 * DAY_MS)
    return sj.rows(0, 10 ** 15)


def test_stats_and_format(cfg, sig):
    rows = _settled_rows(cfg, sig, [65000, 65000, 50000])
    st = compute_stats(rows, "30 дн.", "btc")
    assert (st.total.n, st.total.settled, st.total.wins) == (3, 3, 2)
    assert st.by_strategy["iron_condor"].n == 3 and st.by_regime["high"].n == 3
    text = fmt_stats(st)
    for s in ("Сигналы BTC за 30 дн.: 3", "Прибыльных: 2 из 3 (67%)", "Железный кондор", "IV высокая", "ΣR"):
        assert s in text
    assert "Сигналов с ногами за период нет" in fmt_stats(compute_stats([], "7 дн."))


def test_stats_counts_open(cfg, sig, clock_before_expiry):
    SignalJournal(cfg).record(sig, now_ms=NOW)
    st = compute_stats(SignalJournal(cfg).rows(0, 10 ** 15), "7 дн.")
    assert (st.total.n, st.total.settled) == (1, 0)
    assert "ждут экспирации 1" in fmt_stats(st) and "Прибыльных" not in fmt_stats(st)


def test_rows_filter_and_csv(cfg, sig):
    sj = SignalJournal(cfg)
    sj.record(sig, user_name="=cmd()", now_ms=NOW)
    assert sj.rows(0, 10 ** 15, "eth") == [] and len(sj.rows(0, 10 ** 15, "btc")) == 1
    assert sj.rows(NOW + 1, 10 ** 15) == []
    data = sj.csv_bytes(0, 10 ** 15).decode()
    assert data.startswith("﻿id,ts,base") and "Железный кондор" in data and "'=cmd()" in data


def test_fmt_settled(cfg, sig):
    rows = _settled_rows(cfg, sig, [65000])
    text = fmt_settled(rows)
    assert f"#{rows[0]['id']} BTC Железный кондор" in text and "R)" in text and "/signals_stats" in text
    assert len(fmt_settled(rows * sigjournal.SETTLED_PER_MESSAGE)) < 4096   # полное сообщение — в лимите Telegram


def test_stats_average_ignores_rows_without_r(cfg, sig):
    rows = _settled_rows(cfg, sig, [65000, 65000])
    rows[1] = dict(rows[1], r_mult=None)
    st = compute_stats(rows, "7 дн.")
    assert st.total.n_r == 1 and f"средний {rows[0]['r_mult']:+.2f}R" in fmt_stats(st)


# ---------- бот ----------
@pytest.fixture
def b(make_client, cfg, market, monkeypatch):
    monkeypatch.setattr("optbot.signals.time", NS(time=lambda: NOW / 1000, monotonic=__import__("time").monotonic))
    return OptionsBot(make_client(cfg), cfg)


def test_bot_signals_recorded_with_number(b, cfg):
    tg = TG()
    run(b.cmd_signals(tg.upd(VIEWER, "g"), tg.ctx("BTC")))
    r = q(cfg, "SELECT id, user_id, chat_id, source FROM signals")
    assert len(r) == 1 and (r[0]["user_id"], r[0]["chat_id"], r[0]["source"]) == (VIEWER, -100111, "bot")
    assert f"#{r[0]['id']} Железный кондор" in tg.last()


def test_bot_record_failure_does_not_block_card(b, monkeypatch, caplog):
    tg = TG()
    monkeypatch.setattr(b.sigjournal, "record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    run(b.cmd_signals(tg.upd(VIEWER), tg.ctx("BTC")))
    assert "Железный кондор" in tg.last() and "не записан" in caplog.text


def test_bot_signals_stats_and_csv(b, cfg):
    tg = TG()
    run(b.cmd_signals(tg.upd(VIEWER), tg.ctx("BTC")))
    conn = db.account(cfg)
    with conn:  # запись — по реальным часам бота, а время расчёта подменено; для отбора по периоду — «сейчас»
        conn.execute("UPDATE signals SET ts = ?", (int(__import__("time").time() * 1000) - 1000,))
    conn.close()
    docs = []

    async def send_document(document, filename):
        docs.append((filename, document))
    u = tg.upd(VIEWER, "g")
    u.effective_chat.send_document = send_document
    run(b.cmd_signals_stats(u, tg.ctx("btc", "7d", "csv")))
    assert "Сигналы BTC за 7 дн.: 1" in tg.last()
    assert docs and docs[0][0] == "signals-demo-btc-7дн.csv" and b"BTC" in docs[0][1]
    run(b.cmd_signals_stats(tg.upd(VIEWER), tg.ctx()))
    assert "за 30 дн." in tg.last()
    run(b.cmd_signals_stats(tg.upd(VIEWER), tg.ctx("7d", "30d")))
    assert "Формат" in tg.last()
    n = len(tg.out)
    run(b.cmd_signals_stats(tg.upd(STRANGER), tg.ctx()))
    assert len(tg.out) == n


def test_bot_eval_job_broadcasts(b, cfg, sig):
    tg = TG()
    b._app = NS(bot=tg.bot)
    sid = SignalJournal(cfg).record(sig, now_ms=NOW)
    _set_price(cfg, 65000)
    b.sigjournal.evaluate = lambda client: SignalJournal(cfg).evaluate(client, now_ms=EXP_MS + 3600_000)
    run(b._sigeval_job(NS()))
    assert f"#{sid} BTC Железный кондор" in tg.last("send")
    sent = len(tg.out)
    run(b._sigeval_job(NS()))  # больше нечего
    assert len(tg.out) == sent


def test_bot_eval_job_survives_errors(b, caplog):
    b.sigjournal.evaluate = lambda client: (_ for _ in ()).throw(RuntimeError("boom"))
    run(b._sigeval_job(NS()))
    assert "итоги не посчитаны" in caplog.text


def test_bot_menu_and_help():
    from optbot.bot import HELP
    assert "signals_stats" in [n for n, _, _ in OptionsBot.COMMANDS] and "/signals_stats" in HELP


# ---------- CLI ----------
def test_cli_signals_records(client, cfg, market, monkeypatch, capsys):
    monkeypatch.setattr("optbot.signals.time", NS(time=lambda: NOW / 1000, monotonic=__import__("time").monotonic))
    cli.run(cli.build_parser().parse_args(["signals", "BTC"]), client)
    out = capsys.readouterr().out
    r = q(cfg, "SELECT id, source, user_name FROM signals")
    assert r and r[0]["source"] == "cli" and f"#{r[0]['id']}" in out


def test_sigjournal_main(cfg, sig, monkeypatch, capsys, tmp_path, make_client):
    monkeypatch.setattr(sigjournal, "settings", cfg)
    SignalJournal(cfg).record(sig)
    assert sigjournal.main(["--stats", "7d", "--base", "BTC", "--csv", str(tmp_path / "s.csv")]) == 0
    assert "Сигналы BTC" in capsys.readouterr().out and (tmp_path / "s.csv").read_bytes().startswith(b"\xef\xbb\xbf")
    monkeypatch.setattr("optbot.bybit_client.OptionsClient", lambda cfg: make_client(cfg))
    assert sigjournal.main(["--evaluate"]) == 0
    assert "итогов 0" in capsys.readouterr().out


def test_cli_signals_record_failure_still_prints(client, cfg, market, monkeypatch, capsys):
    monkeypatch.setattr("optbot.signals.time", NS(time=lambda: NOW / 1000, monotonic=__import__("time").monotonic))
    monkeypatch.setattr(SignalJournal, "record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("locked")))
    cli.run(cli.build_parser().parse_args(["signals", "BTC"]), client)
    out = capsys.readouterr()
    assert "Железный кондор" in out.out and "не записан (locked)" in out.err


# ---------- ревью: цена поставки после простоя, цель при нулевой прибыли, имена, выключенный журнал ----------
def test_evaluate_after_long_downtime_fetches_before_giving_up(cfg, sig, make_client, fake):
    sj = SignalJournal(cfg)
    sid = sj.record(sig, now_ms=NOW)
    fake.delivery_px["BTC"] = [{"deliveryPrice": "65000", "deliveryTime": str(EXP_MS)}]
    res = sj.evaluate(make_client(cfg), now_ms=EXP_MS + 10 * DAY_MS)   # бот лежал 10 дней — у биржи цена есть
    assert res.settled == 1 and res.void == 0
    assert q(cfg, "SELECT pnl FROM signals WHERE id = ?", sid)[0]["pnl"] is not None


def test_evaluate_no_give_up_when_fetch_failed_or_no_client(cfg, sig, make_client, fake):
    sj = SignalJournal(cfg)
    sj.record(sig, now_ms=NOW)
    assert sj.evaluate(now_ms=EXP_MS + 10 * DAY_MS).void == 0                # биржу не спрашивали
    fake.fail["get_new_delivery_price"] = requests.exceptions.ConnectionError("down")
    res = sj.evaluate(make_client(cfg), now_ms=EXP_MS + 10 * DAY_MS)
    assert res.void == 0 and res.waiting == 1 and res.errors                # сбой запроса — не повод закрывать


def test_half_target_with_zero_max_profit(cfg, sig, monkeypatch):
    sj = SignalJournal(cfg)
    sid = sj.record(sig, now_ms=NOW)
    _set_price(cfg, 65000)
    conn = db.account(cfg)
    with conn:
        conn.execute("UPDATE signals SET max_profit = 0 WHERE id = ?", (sid,))
    conn.close()
    seen = []
    monkeypatch.setattr(sigjournal, "path_stats", lambda legs, marks, target: seen.append(target) or
                        sigjournal.PathStats())
    sj.evaluate(now_ms=EXP_MS + 3600_000)
    assert seen == [None]                       # прибыли нет — «50% прибыли» не считается


def test_csv_without_names(cfg, sig):
    sj = SignalJournal(cfg)
    sj.record(sig, user_name="Trader Joe", now_ms=NOW)
    assert "Trader Joe" in sj.csv_bytes(0, 10 ** 15).decode()
    data = sj.csv_bytes(0, 10 ** 15, names=False).decode()
    assert "Trader Joe" not in data and "user_name" not in data


def test_journal_disabled_no_signal_writes(cfg, sig):
    import dataclasses
    off = dataclasses.replace(cfg, journal_enabled=False)
    assert SignalJournal(off).record(sig) is None
    assert q(cfg, "SELECT COUNT(*) n FROM signals")[0]["n"] == 0
    assert SignalJournal(off).evaluate(now_ms=EXP_MS + DAY_MS).settled == 0


def test_stats_expired_waiting_for_price_shown_separately(cfg, sig):
    sj = SignalJournal(cfg)
    sj.record(sig, now_ms=NOW)
    before = fmt_stats(compute_stats(sj.rows(0, 10 ** 15), "7 дн.", now_ms=EXP_MS - 1))
    after = fmt_stats(compute_stats(sj.rows(0, 10 ** 15), "7 дн.", now_ms=EXP_MS + 1))
    assert "ждут экспирации 1" in before and "итог ещё не посчитан" not in before
    assert "ждут экспирации 0" in after and "итог ещё не посчитан 1" in after


def test_bot_viewer_csv_has_no_names(b, cfg):
    tg = TG()
    run(b.cmd_signals(tg.upd(TRADER), tg.ctx("BTC")))
    docs = []

    async def send_document(document, filename):
        docs.append(document)
    for who in (VIEWER, TRADER):
        u = tg.upd(who)
        u.effective_chat.send_document = send_document
        run(b.cmd_signals_stats(u, tg.ctx("30d", "csv")))
    assert b"user_name" not in docs[0] and b"user_name" in docs[1]


# ---------- ревью 3: объявление ручных итогов, имена в группе, номер в копии, повтор, shown из кэша ----------
def test_eval_job_announces_manual_evaluate(b, cfg, sig):
    tg = TG()
    b._app = NS(bot=tg.bot)
    sid = SignalJournal(cfg).record(sig, now_ms=NOW)
    _set_price(cfg, 65000)
    SignalJournal(cfg).evaluate(now_ms=EXP_MS + 3600_000)          # вручную: `--evaluate` на сервере
    b.sigjournal.evaluate = lambda client: sigjournal.EvalResult()  # боту считать нечего
    run(b._sigeval_job(NS()))
    assert f"#{sid} BTC" in tg.last("send")
    assert q(cfg, "SELECT announced_ms FROM signals")[0]["announced_ms"]
    n = len(tg.out)
    run(b._sigeval_job(NS()))
    assert len(tg.out) == n                                         # второй раз не объявляется


def test_trader_csv_in_group_has_no_names(b, cfg):
    tg = TG()
    run(b.cmd_signals(tg.upd(TRADER), tg.ctx("BTC")))
    docs = []

    async def send_document(document, filename):
        docs.append(document)
    u = tg.upd(TRADER, "g")
    u.effective_chat.send_document = send_document
    run(b.cmd_signals_stats(u, tg.ctx("30d", "csv")))
    assert docs and b"user_name" not in docs[0]


def test_cached_showing_counted_and_card_has_number(b, cfg, monkeypatch):
    monkeypatch.setattr(sigjournal, "time", NS(time=lambda: NOW / 1000 + 1))  # запись — после расчёта, как в жизни
    tg = TG()
    run(b.cmd_signals(tg.upd(VIEWER), tg.ctx("BTC")))
    run(b.cmd_signals(tg.upd(TRADER), tg.ctx("BTC")))               # из кэша, другой человек — показ
    run(b.cmd_signals(tg.upd(TRADER), tg.ctx("BTC")))               # тот же человек снова — не показ
    r = q(cfg, "SELECT id, shown FROM signals")
    assert len(r) == 1 and r[0]["shown"] == 2
    assert f"#{r[0]['id']} Железный кондор" in tg.last() and "Повтор" not in tg.last()  # тот же расчёт
    assert b._sig_cache["BTC"][1].id is None                        # номер — в копии, не в общем кэше


def test_eval_job_keeps_unannounced_when_undelivered(b, cfg, sig, caplog):
    b._app = None                                                   # Telegram недоступен — доставлено 0
    SignalJournal(cfg).record(sig, now_ms=NOW)
    _set_price(cfg, 65000)
    SignalJournal(cfg).evaluate(now_ms=EXP_MS + 3600_000)
    b.sigjournal.evaluate = lambda client: sigjournal.EvalResult()
    run(b._sigeval_job(NS()))
    assert "никуда не доставлена" in caplog.text and len(SignalJournal(cfg).unannounced()) == 1


def test_signals_stats_journal_disabled(make_client, cfg, market):
    import dataclasses
    bb = OptionsBot(make_client(cfg), dataclasses.replace(cfg, journal_enabled=False))
    tg = TG()
    run(bb.cmd_signals_stats(tg.upd(VIEWER), tg.ctx()))
    assert "Журнал выключен" in tg.last()


def test_path_mark_from_entry_mid():
    legs = [Leg(symbol="S", kind="P", strike=60000, side=-1, qty=1.0, bid=280, ask=320)]  # продажа по 280, mid 300
    marks = [dict(ts=1, symbol="S", mark=300, bid=280, ask=320)]                          # рынок не двигался
    ps = path_stats(legs, marks, target=None)
    assert ps.max_dd_mark == 0 and ps.max_dd == pytest.approx(-40)  # спред — только в исполнимом пути


def test_fmt_signal_repeat_line(sig):
    from optbot.signals import fmt_signal
    sig.id = 7
    assert "Повтор" not in fmt_signal(sig, "demo")
    sig.journal_first_ms = NOW + 1          # запись после расчёта — свой показ из кэша, не повтор
    assert "Повтор" not in fmt_signal(sig, "demo")
    sig.journal_first_ms = NOW - 3600_000
    assert "Повтор #7: в журнале — цены первого показа" in fmt_signal(sig, "demo")


# ---------- ревью 5 ----------
def test_eval_job_marks_each_delivered_chunk(b, cfg, sig, monkeypatch):
    """Вторая часть сводки не ушла — первая отмечена (не повторится), вторая — ждёт следующего прохода."""
    monkeypatch.setattr(sigjournal, "SETTLED_PER_MESSAGE", 1)
    monkeypatch.setattr("optbot.bot.SETTLED_PER_MESSAGE", 1)
    rows = _settled_rows(cfg, sig, [65000, 65000])
    calls = []

    async def bc(msg, what):
        calls.append(msg)
        return 1 if len(calls) == 1 else 0
    b._broadcast = bc
    b.sigjournal.evaluate = lambda client: sigjournal.EvalResult()
    run(b._sigeval_job(NS()))
    left = SignalJournal(cfg).unannounced()
    assert len(calls) == 2 and [r["id"] for r in left] == [rows[1]["id"]]


def test_record_failure_retried_on_next_view(b, cfg, monkeypatch):
    tg = TG()
    real = b.sigjournal.record
    monkeypatch.setattr(b.sigjournal, "record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("locked")))
    run(b.cmd_signals(tg.upd(VIEWER), tg.ctx("BTC")))
    assert q(cfg, "SELECT COUNT(*) n FROM signals")[0]["n"] == 0
    monkeypatch.setattr(b.sigjournal, "record", real)
    run(b.cmd_signals(tg.upd(VIEWER), tg.ctx("BTC")))       # тот же человек, из кэша — записать снова
    assert q(cfg, "SELECT COUNT(*) n FROM signals")[0]["n"] == 1


# ---------- ревью 6 ----------
def test_double_tap_counts_once(b, cfg, clock_before_expiry):
    tg = TG()
    run(b.cmd_signals(tg.upd(VIEWER), tg.ctx("BTC")))

    async def two():
        await asyncio.gather(b.cmd_signals(tg.upd(TRADER), tg.ctx("BTC")),
                             b.cmd_signals(tg.upd(TRADER), tg.ctx("BTC")))
    run(two())
    assert q(cfg, "SELECT shown FROM signals")[0]["shown"] == 2


def test_repeat_show_sets_first_time(cfg, sig):
    import dataclasses
    sj = SignalJournal(cfg)
    sid = sj.record(sig, now_ms=NOW)
    other = dataclasses.replace(sig, strategy=None, id=None)
    assert sj.record(other, now_ms=NOW) is None and other.id is None
    again = dataclasses.replace(sig, id=None)
    assert sj.record(again, user_id=5, now_ms=NOW + 1) == sid and again.journal_first_ms == NOW


def test_track_uses_record_time(cfg, sig):
    SignalJournal(cfg).record(sig, now_ms=NOW)
    assert {r["added_ms"] for r in q(cfg, "SELECT added_ms FROM tracked_symbols")} == {NOW}


# ---------- ревью 7 ----------
def test_same_user_after_cache_ttl_not_counted_again(b, cfg, clock_before_expiry):
    tg = TG()
    run(b.cmd_signals(tg.upd(VIEWER), tg.ctx("BTC")))
    b._sig_cache.clear()                                   # прошло больше SIGNAL_CACHE_TTL — пересчёт
    run(b.cmd_signals(tg.upd(VIEWER), tg.ctx("BTC")))
    assert q(cfg, "SELECT shown FROM signals")[0]["shown"] == 1
    conn = db.account(cfg)
    with conn:                                             # прошли сутки — снова показ
        conn.execute("UPDATE signal_views SET ts = ts - ?", (2 * DAY_MS,))
    conn.close()
    run(b.cmd_signals(tg.upd(VIEWER), tg.ctx("BTC")))
    assert q(cfg, "SELECT shown FROM signals")[0]["shown"] == 2


def test_evaluate_skips_malformed_fetched_rows(cfg, sig, make_client, fake):
    sj = SignalJournal(cfg)
    sid = sj.record(sig, now_ms=NOW)
    fake.delivery_px["BTC"] = [{"deliveryPrice": "", "deliveryTime": str(EXP_MS - DAY_MS)},
                               {"deliveryPrice": "65000", "deliveryTime": str(EXP_MS)}]
    res = sj.evaluate(make_client(cfg), now_ms=EXP_MS + 3600_000)
    assert res.settled == 1 and not res.errors
    assert q(cfg, "SELECT delivery_price FROM signals WHERE id = ?", sid)[0]["delivery_price"] == 65000


def test_void_not_double_counted(cfg, sig, make_client, monkeypatch):
    """Параллельный --evaluate закрыл раньше — второй проход не считает и не логирует."""
    sj = SignalJournal(cfg)
    sid = sj.record(sig, now_ms=NOW)
    client = make_client(cfg)
    orig = client.delivery_prices

    def closing(base):  # пока этот проход спрашивал биржу, другой закрыл запись
        conn = db.account(cfg)
        with conn:
            conn.execute("UPDATE signals SET settled_ms = 1 WHERE id = ?", (sid,))
        conn.close()
        return orig(base)
    client.delivery_prices = closing
    assert sj.evaluate(client, now_ms=EXP_MS + 4 * DAY_MS).void == 0


# ---------- ревью 8 ----------
def test_eval_job_no_targets_marks_announced(b, cfg, sig):
    b._app = NS(bot=TG().bot)
    b.cfg = __import__("dataclasses").replace(b.cfg, tg_allowed_chat_ids=frozenset(), tg_trader_ids=frozenset())
    SignalJournal(cfg).record(sig, now_ms=NOW)
    _set_price(cfg, 65000)
    SignalJournal(cfg).evaluate(now_ms=EXP_MS + 3600_000)
    b.sigjournal.evaluate = lambda client: sigjournal.EvalResult()
    run(b._sigeval_job(NS()))
    assert SignalJournal(cfg).unannounced() == []              # не копится до появления групп


def test_evaluate_broken_row_does_not_stop_others(cfg, sig):
    sj = SignalJournal(cfg)
    a = sj.record(sig, now_ms=NOW)
    conn = db.account(cfg)
    with conn:
        conn.execute("UPDATE signals SET legs = '[{}]', legs_key = 'broken' WHERE id = ?", (a,))
    conn.close()
    b_ = sj.record(sig, now_ms=NOW)
    _set_price(cfg, 65000)
    res = sj.evaluate(now_ms=EXP_MS + 3600_000)
    assert res.settled == 1 and any(e.startswith(f"#{a}:") for e in res.errors)
    assert q(cfg, "SELECT pnl FROM signals WHERE id = ?", b_)[0]["pnl"] is not None


def test_repeat_line_when_journal_prices_differ(sig):
    from optbot.signals import fmt_signal
    sig.id, sig.journal_first_ms = 7, sig.metrics.ts_ms + 1     # запись позже начала расчёта (другой процесс)
    sig.journal_net = sig.net
    assert "Повтор" not in fmt_signal(sig, "demo")
    sig.journal_net = sig.net + 1                                # но цены в журнале другие — предупредить
    assert "Повтор #7" in fmt_signal(sig, "demo")


def test_show_uses_short_db_timeout(cfg, sig, monkeypatch):
    seen = []
    real = db.connect
    monkeypatch.setattr(sigjournal.db, "connect",
                        lambda path, timeout=30, **kw: seen.append(timeout) or real(path, timeout, **kw))
    sj = SignalJournal(cfg)
    sj.record(sig, now_ms=NOW)
    assert seen and set(seen) == {sigjournal.SHOW_DB_TIMEOUT_S}   # БД счёта и подключаемая рыночная


# ---------- ревью 9 ----------
def test_path_long_leg_zero_bid_is_exit_value():
    legs = [L("S", "P", 60000, -1, 300), L("B", "P", 58000, +1, 100)]
    marks = [dict(ts=1, symbol="S", mark=40, bid=30, ask=50), dict(ts=1, symbol="B", mark=1, bid=0, ask=5)]
    ps = path_stats(legs, marks, target=100)
    assert ps.n == 1 and ps.half_ms == 1 and ps.max_dd == 0   # −(50−300) + (0−100) = +150 ≥ 100
    marks[0]["ask"] = 0                                          # короткую выкупить нельзя — срез пропущен
    assert path_stats(legs, marks, target=100).half_ms is None


def test_path_includes_slot_captured_after_entry(cfg, sig):
    sj = SignalJournal(cfg)
    sid = sj.record(sig, now_ms=NOW + 2000)                     # показ в 08:00:02
    conn = db.account(cfg)
    with conn:
        for l in sig.legs:  # срез 08:00 снят в 08:00:05 — после входа, должен войти в путь
            conn.execute("INSERT INTO leg_marks (ts, symbol, mark, bid, ask, captured_ms) VALUES (?,?,?,?,?,?)",
                         (NOW, l.symbol, l.mark, l.bid, l.ask, NOW + 5000))
    conn.close()
    _set_price(cfg, 65000)
    sj.evaluate(now_ms=EXP_MS + 3600_000)
    assert q(cfg, "SELECT marks_n FROM signals WHERE id = ?", sid)[0]["marks_n"] == 1


def test_broken_row_closed_after_timeout(cfg, sig, caplog):
    sj = SignalJournal(cfg)
    a = sj.record(sig, now_ms=NOW)
    conn = db.account(cfg)
    with conn:
        conn.execute("UPDATE signals SET legs = '[{}]' WHERE id = ?", (a,))
    conn.close()
    _set_price(cfg, 65000)
    assert sj.evaluate(now_ms=EXP_MS + 3600_000).errors
    res = sj.evaluate(now_ms=EXP_MS + 4 * DAY_MS)
    assert res.void == 1 and not res.errors and "закрыт без итога" in caplog.text
    assert not sj.evaluate(now_ms=EXP_MS + 5 * DAY_MS).errors


def test_signals_stats_unknown_asset(b):
    tg = TG()
    run(b.cmd_signals_stats(tg.upd(VIEWER), tg.ctx("XRP")))
    assert "Актив 'XRP'" in tg.last()
    run(b.cmd_signals_stats(tg.upd(VIEWER), tg.ctx("BTC", "ETH")))
    assert "Один актив" in tg.last()
    run(b.cmd_signals_stats(tg.upd(VIEWER), tg.ctx("вчера")))
    assert "за вчера" in tg.last()



# ---------- ревью 10 ----------
def test_path_long_leg_null_bid_is_zero():
    """Сборщик пишет нулевой bid как NULL — для длинной ноги это продажа по 0, срез не теряется."""
    legs = [L("S", "P", 60000, -1, 300), L("B", "P", 58000, +1, 100)]
    marks = [dict(ts=1, symbol="S", mark=40, bid=30, ask=50), dict(ts=1, symbol="B", mark=None, bid=None, ask=5)]
    assert path_stats(legs, marks, target=100).half_ms == 1


def test_transient_db_error_not_voided(cfg, sig, monkeypatch):
    import sqlite3
    sj = SignalJournal(cfg)
    sid = sj.record(sig, now_ms=NOW)
    _set_price(cfg, 65000)

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(SignalJournal, "_settle", staticmethod(locked))
    res = sj.evaluate(now_ms=EXP_MS + 4 * DAY_MS)
    assert res.void == 0 and res.errors
    assert q(cfg, "SELECT settled_ms FROM signals WHERE id = ?", sid)[0]["settled_ms"] is None


def test_eval_job_gives_up_when_undeliverable(b, cfg, sig, caplog):
    rows = _settled_rows(cfg, sig, [65000])

    async def nobody(msg, what):
        return 0
    b._broadcast = nobody
    b.sigjournal.evaluate = lambda client: sigjournal.EvalResult()
    run(b._sigeval_job(NS()))                                  # свежий итог — повторим позже
    assert len(SignalJournal(cfg).unannounced()) == 1
    conn = db.account(cfg)
    with conn:
        conn.execute("UPDATE signals SET settled_ms = ?", (int(__import__("time").time() * 1000) - 2 * DAY_MS,))
    conn.close()
    run(b._sigeval_job(NS()))                                  # не доставлен сутки — больше не шлём
    assert SignalJournal(cfg).unannounced() == [] and f"[{rows[0]['id']}]" in caplog.text


def test_mark_announced_retried(b, monkeypatch):
    calls = []

    def flaky(ids):
        calls.append(ids)
        if len(calls) < 2:
            raise RuntimeError("locked")
    monkeypatch.setattr(b.sigjournal, "mark_announced", flaky)
    real_sleep = asyncio.sleep
    monkeypatch.setattr("optbot.bot.asyncio.sleep", lambda s: real_sleep(0))
    run(b._mark_announced([1]))
    assert calls == [[1], [1]]


def test_repeat_view_needs_no_write_lock(cfg, sig, monkeypatch):
    sj = SignalJournal(cfg)
    sj.record(sig, user_id=1, now_ms=NOW)
    real = db.connect
    stmts = []

    def spy(path, timeout=30, **kw):
        c = real(path, timeout, **kw)
        c.set_trace_callback(stmts.append)
        return c
    monkeypatch.setattr(sigjournal.db, "connect", spy)
    assert sj.record(sig, user_id=1, now_ms=NOW + 1000) is not None
    assert not any("BEGIN IMMEDIATE" in x for x in stmts)



# ---------- ревью 11 ----------
def test_giveup_marks_only_stale_rows(b, cfg, sig):
    rows = _settled_rows(cfg, sig, [65000, 65000])
    conn = db.account(cfg)
    with conn:  # первый посчитан двое суток назад, второй — только что
        conn.execute("UPDATE signals SET settled_ms = ? WHERE id = ?",
                     (int(__import__("time").time() * 1000) - 2 * DAY_MS, rows[0]["id"]))
    conn.close()

    async def nobody(msg, what):
        return 0
    b._broadcast = nobody
    b.sigjournal.evaluate = lambda client: sigjournal.EvalResult()
    run(b._sigeval_job(NS()))
    assert [r["id"] for r in SignalJournal(cfg).unannounced()] == [rows[1]["id"]]   # свежий ждёт повтора


def test_empty_price_list_closed_after_giveup(cfg, sig, make_client):
    sj = SignalJournal(cfg)
    sj.record(sig, now_ms=NOW)
    assert sj.evaluate(make_client(cfg), now_ms=EXP_MS + 10 * DAY_MS).waiting == 1   # биржа отдаёт пусто — ждём
    assert sj.evaluate(make_client(cfg), now_ms=EXP_MS + 15 * DAY_MS).void == 1      # но не вечно


def test_only_data_errors_close_row(cfg, sig, monkeypatch):
    sj = SignalJournal(cfg)
    sid = sj.record(sig, now_ms=NOW)
    _set_price(cfg, 65000)

    def oom(*a, **k):
        raise OSError("disk")
    monkeypatch.setattr(SignalJournal, "_settle", staticmethod(oom))
    assert sj.evaluate(now_ms=EXP_MS + 4 * DAY_MS).void == 0
    assert q(cfg, "SELECT settled_ms FROM signals WHERE id = ?", sid)[0]["settled_ms"] is None


def test_json_has_no_nan_or_infinity(cfg, sig):
    sig.profile.breakevens.append(float("inf"))
    sig.metrics.trend.gap_pct = float("nan")
    sid = SignalJournal(cfg).record(sig, now_ms=NOW)
    r = q(cfg, "SELECT breakevens, metrics FROM signals WHERE id = ?", sid)[0]
    assert "Infinity" not in r["breakevens"] and "NaN" not in r["metrics"]
    assert json.loads(r["breakevens"])[-1] is None and json.loads(r["metrics"])["ema_gap_pct"] is None


def test_signals_stats_period_words_from_parse_period(b):
    tg = TG()
    run(b.cmd_signals_stats(tg.upd(VIEWER), tg.ctx("today")))
    assert "за сегодня" in tg.last()



def test_evaluate_price_at_day_start(cfg, sig):
    """Bybit new-delivery-price: deliveryTime — 00:00 UTC дня поставки, а не 08:00 (сверено на demo)."""
    from optbot.fmt import DAY_MS
    sj = SignalJournal(cfg)
    sid = sj.record(sig, now_ms=NOW)
    _set_price(cfg, 65000, t=EXP_MS // DAY_MS * DAY_MS)
    _set_price(cfg, 1, t=EXP_MS // DAY_MS * DAY_MS - DAY_MS)           # прошлый день — не берётся
    assert sj.evaluate(now_ms=EXP_MS + 3600_000).settled == 1
    assert q(cfg, "SELECT delivery_price FROM signals WHERE id = ?", sid)[0]["delivery_price"] == 65000


def test_nearest_price_rules():
    from optbot.fmt import DAY_MS, HOUR_MS
    from optbot.journal import nearest_price
    t = 20 * DAY_MS + 8 * HOUR_MS
    assert nearest_price([(t + 30 * 60_000, 2.0), (20 * DAY_MS, 1.0)], t) == 2.0    # ±1 ч — приоритет
    assert nearest_price([(20 * DAY_MS, 1.0)], t) == 1.0                              # 00:00 того же дня
    assert nearest_price([(20 * DAY_MS + 3 * HOUR_MS, 1.0)], t) is None              # не начало дня и не ±1 ч
    assert nearest_price([(21 * DAY_MS, 1.0)], t) is None
