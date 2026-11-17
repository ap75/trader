"""Этап 5: многоногая сделка (сначала покупки, IOC от mid, частичное, закрытие лишнего) и кнопка «Выставить»."""
import asyncio
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest
import requests

from optbot import db, multileg
from optbot.bot import OptionsBot
from optbot.journal import Journal
from optbot.linkid import parse_link_id
from optbot.multileg import MultiLegExecutor, fmt_result, safe_order
from optbot.bybit_client import PortfolioRiskError
from tests.fakes import EXP, NOW, set_chain, set_klines
from tests.helpers import with_cfg
from tests.test_bot import TG, TRADER, VIEWER

LONG_C = f"BTC-{EXP}-67500-C-USDT"
SHORT_C = f"BTC-{EXP}-67000-C-USDT"
Q = Decimal("0.01")


def run(coro):
    return asyncio.run(coro)


def q(cfg, sql, *a):
    conn = db.connect(cfg.db_file)
    try:
        return [dict(r) for r in conn.execute(sql, a)]
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(multileg, "time", NS(sleep=lambda s: None))


@pytest.fixture
def market(fake):
    set_chain(fake, iv=0.6)            # спред bid/ask 4%: IOC по mid не исполняется, по mid+5% — да
    set_klines(fake, rv=0.4)
    return fake


def ex(client, cfg, sid=7):
    j = Journal(cfg)
    return MultiLegExecutor(client, cfg, link=lambda leg: j.register_signal_order(sid, leg, TRADER, "user10", 1))


def bear_call(qty=Q):
    return [(SHORT_C, "Sell", qty, 300.0), (LONG_C, "Buy", qty, 200.0)]   # в порядке сигнала: короткая первой


def sent(fake):
    return [(kw["symbol"], kw["side"], kw["reduceOnly"]) for kw in fake.called("place_order")]


# ---------- порядок и полное исполнение ----------
def test_safe_order_buys_first():
    legs = [("a", "Sell"), ("b", "Buy"), ("c", "Sell"), ("d", "Buy")]
    assert safe_order(legs) == [1, 3, 0, 2]


def test_full_execution_buys_first_from_mid(client, cfg, market):
    res = ex(client, cfg).execute(bear_call())
    assert res.status == "complete" and res.qty == Q and not res.alert
    calls = market.called("place_order")
    assert [c["symbol"] for c in calls][0] == LONG_C                 # сначала покупка
    assert all(c["timeInForce"] == "IOC" for c in calls)
    first, second = calls[0], calls[1]
    assert Decimal(first["price"]) < Decimal(second["price"])        # по mid не вышло — шаг к рынку
    assert [f.filled for f in res.legs] == [Q, Q]
    assert res.actual_net != 0 and "разница" in fmt_result(res, 7)


def test_links_are_signal_links_unique_across_runs(client, cfg, market):
    ex(client, cfg).execute(bear_call())
    ex(client, cfg).execute(bear_call())                             # второе нажатие — новые orderLinkId
    links = [c["orderLinkId"] for c in market.called("place_order")]
    assert len(links) == len(set(links))
    p = parse_link_id(links[0])
    assert p["source"] == "signal" and p["signal_id"] == 7 and p["leg"] == 1   # нога 1 — покупка
    rows = q(cfg, "SELECT signal_id, leg, user_id FROM order_links WHERE source = 'signal'")
    assert len(rows) == len(links) and all(r["signal_id"] == 7 and r["user_id"] == TRADER for r in rows)


# ---------- частичное и закрытие ----------
def test_partial_next_legs_on_filled_qty(client, cfg, market):
    market.ioc_max[LONG_C] = Q                                       # за раз — только 0.01
    res = ex(client, cfg).execute(bear_call(Q * 4))
    # три попытки (от ask к mid+10%) по 0.01 → 0.03 из 0.04; короткая — ровно 0.03
    assert res.status == "partial" and res.qty == Q * 3 and res.alert
    short = res.legs[0]
    assert short.qty == Q * 3 and short.filled == Q * 3
    assert not res.unwind


def test_short_never_fills_longs_closed_reduce_only(client, cfg, market):
    market.ioc_max[SHORT_C] = Decimal(0)
    res = ex(client, cfg).execute(bear_call())
    assert res.status == "unwound" and res.qty == 0 and not res.left
    s = sent(market)
    assert s[0] == (LONG_C, "Buy", False) and (LONG_C, "Sell", True) in s   # купленное — продано reduce-only
    assert all(sym != SHORT_C or side == "Sell" for sym, side, _ in s)       # непокрытой продажи не было


def test_unwind_fails_left_and_alert(client, cfg, market, monkeypatch):
    market.ioc_max[SHORT_C] = Decimal(0)
    real = market._ioc

    def no_sell_long(o):
        if o["symbol"] == LONG_C and o["side"] == "Sell":
            market.ioc_max[LONG_C] = Decimal(0)
        real(o)
    monkeypatch.setattr(market, "_ioc", no_sell_long)
    res = ex(client, cfg).execute(bear_call())
    assert res.status == "stuck" and res.alert and res.left == [f"Buy 0.01 {LONG_C}"]
    assert "проверьте /pos" in fmt_result(res, 7)


def test_condor_short_partial_unwinds_excess(client, cfg, market):
    """Кондор: крылья 0.02, первая короткая 0.02, вторая — 0.01: лишнее закрывается (откуп, потом продажа)."""
    p_long, p_short = f"BTC-{EXP}-60000-P-USDT", f"BTC-{EXP}-62000-P-USDT"
    legs = [(p_short, "Sell", Q * 2, 1.0), (p_long, "Buy", Q * 2, 1.0), (SHORT_C, "Sell", Q * 2, 1.0),
            (LONG_C, "Buy", Q * 2, 1.0)]
    real = market._ioc

    def ioc(o):  # вторая короткая (колл) исполняется только на 0.01 за всё время
        if o["symbol"] == SHORT_C and not o["reduceOnly"]:
            market.ioc_max[SHORT_C] = Q if not any(h["symbol"] == SHORT_C and h["cumExecQty"] != "0"
                                                   for h in market.history) else Decimal(0)
        real(o)
    market._ioc = ioc
    res = ex(client, cfg).execute(legs)
    assert res.status == "partial" and res.qty == Q
    s = sent(market)
    opening = [(sym, side) for sym, side, ro in s if not ro]
    assert [side for _, side in opening[:2]] == ["Buy", "Buy"]       # сначала оба крыла
    unwind = list(dict.fromkeys((sym, side) for sym, side, ro in s if ro))   # попыток по ноге — несколько
    # лишнее 0.01: сначала откуп короткого пута, потом продажа длинных крыльев
    assert unwind[0] == (p_short, "Buy") and {x for x in unwind[1:]} == {(p_long, "Sell"), (LONG_C, "Sell")}
    assert not res.left


# ---------- ошибки ----------
def test_risk_error_on_first_leg_nothing_sent(make_client, cfg, market):
    c = with_cfg(cfg, max_order_qty={"BTC": "0.001"})
    res = ex(make_client(c), c).execute(bear_call())
    assert res.status == "none" and "MAX_QTY_BTC" in res.note and market.called("place_order") == []


def test_network_error_after_send_found_by_link(client, cfg, market):
    market.after["place_order"] = requests.exceptions.ReadTimeout("timeout")
    res = ex(client, cfg).execute(bear_call())
    assert res.status == "complete"                                  # исход нашли по orderLinkId


def test_network_error_unknown_stops(client, cfg, market):
    market.fail["place_order"] = requests.exceptions.ReadTimeout("timeout")   # не дошло и не найдено
    res = ex(client, cfg).execute(bear_call())
    assert res.status == "unknown" and res.alert and "исход неизвестен" in res.note
    assert market.called("place_order")[1:] == []                    # дальше ноги не ставим


# ---------- бот: кнопка «Выставить» ----------
@pytest.fixture
def b(make_client, cfg, market, monkeypatch):
    monkeypatch.setattr("optbot.signals.time", NS(time=lambda: NOW / 1000, monotonic=__import__("time").monotonic))
    monkeypatch.setattr("optbot.bot.time.time", lambda: NOW / 1000)
    c = with_cfg(cfg, tg_admin_ids={50})
    bot = OptionsBot(make_client(c), c)
    return bot


def _card(b, tg):
    run(b.cmd_signals(tg.upd(TRADER), tg.ctx("BTC")))
    return next(x for x in tg.buttons() if x.startswith("ex:"))


def test_execute_button_viewer_denied(b):
    tg = TG()
    btn = _card(b, tg)
    u = tg.upd(VIEWER, data=btn)
    run(b.on_callback(u, tg.ctx()))
    u.callback_query.answer.assert_awaited_with("Нет прав")


def test_execute_button_preview_confirm_and_journal(b, market):
    tg = TG()
    btn = _card(b, tg)
    run(b.on_callback(tg.upd(TRADER, data=btn), tg.ctx()))
    preview = tg.last("reply")
    assert "Выставить сигнал #1" in preview and "сначала покупки" in preview
    legs = [ln.split()[0] for ln in preview.split("\n") if ln.startswith(" BUY") or ln.startswith(" SELL")]
    assert legs == ["BUY", "BUY", "SELL", "SELL"]                    # кондор: крылья первыми
    pid = tg.buttons()[0].split(":")[1]
    sends = []
    b._app = NS(bot=NS(send_message=lambda *a, **k: sends.append(a) or asyncio.sleep(0)))
    confirm(b, tg, f"ok:{pid}")
    assert "Исполнено полностью · сигнал #1" in tg.last("edit")
    r = q(b.cfg, "SELECT signal_id, status, user_id, planned_qty, qty FROM signal_executions")
    assert r == [{"signal_id": 1, "status": "complete", "user_id": TRADER, "planned_qty": 0.01, "qty": 0.01}]
    assert sends == []                                               # полностью — без срочного уведомления


def confirm(b, tg, data):
    """Нажать «Подтвердить» и дождаться фоновой задачи (многоногая сделка исполняется задачей приложения)."""
    async def go():
        await b.on_callback(tg.upd(TRADER, data=data), _app_ctx(tg))
        me = asyncio.current_task()
        while pending := [t for t in asyncio.all_tasks() if t is not me and not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)
    run(go())


def _app_ctx(tg):
    """Контекст с фоновыми задачами приложения, как в PTB (таймер подтверждения — не ждём)."""
    def create_task(c):
        if c.__name__ == "_expire":
            c.close()
            return None
        return asyncio.get_running_loop().create_task(c)
    return NS(args=[], bot=tg.bot, application=NS(create_task=create_task))


def _confirm(b, tg, market, monkeypatch, block_calls=False, block_unwind=False):
    btn = _card(b, tg)
    run(b.on_callback(tg.upd(TRADER, data=btn), _app_ctx(tg)))
    pid = tg.buttons()[0].split(":")[1]
    real = market._ioc

    def ioc(o):
        if (block_calls and "-C-" in o["symbol"] and not o["reduceOnly"]) or (block_unwind and o["reduceOnly"]):
            market.ioc_max[o["symbol"]] = Decimal(0)
        else:
            market.ioc_max.pop(o["symbol"], None)
        real(o)
    monkeypatch.setattr(market, "_ioc", ioc)

    sends = []

    async def send(chat_id, text, **kw):
        sends.append((chat_id, text))
    b._app = NS(bot=NS(send_message=send))

    confirm(b, tg, f"ok:{pid}")                                      # исполнение и срочное — задачами
    return sends


def test_execute_unwound_alerts_admin(b, market, monkeypatch):
    """Колловая сторона не исполнилась — путы закрыты reduce-only с потерей: «не собралось», срочно админу."""
    tg = TG()
    sends = _confirm(b, tg, market, monkeypatch, block_calls=True)
    assert "Не собралось — исполненное закрыто" in tg.last("edit")
    assert [c for c, _ in sends] == [50] and "Закрытие лишнего: -" in sends[0][1]
    r = q(b.cfg, "SELECT status, qty FROM signal_executions")
    assert r == [{"status": "unwound", "qty": 0.0}]


def test_execute_stuck_alerts_admin(b, market, monkeypatch):
    tg = TG()
    sends = _confirm(b, tg, market, monkeypatch, block_calls=True, block_unwind=True)
    assert "часть позиции осталась" in tg.last("edit")
    assert [c for c, t in sends] == [50] and "исполнен не полностью" in sends[0][1]


def test_execute_expired_signal_refused(b, market):
    tg = TG()
    btn = _card(b, tg)
    conn = db.connect(b.cfg.db_file)
    with conn:
        conn.execute("UPDATE signals SET expiry_ms = ?", (NOW + 60_000,))
    conn.close()
    run(b.on_callback(tg.upd(TRADER, data=btn), tg.ctx()))
    assert "устарел" in tg.last()


def test_network_error_on_quote_is_leg_error_not_unknown(client, cfg, market):
    """Сбой связи до отправки (котировка) — ордера нет на бирже: ошибка ноги, без «исход неизвестен»."""
    def down(sym):
        raise multileg.BybitNetworkError("timeout")
    client.ticker = down                                             # связи нет на всех попытках
    res = ex(client, cfg).execute(bear_call())
    assert res.status == "none" and "сбой связи" in res.note and not res.alert
    assert market.called("place_order") == []


def test_single_quote_blip_retried_not_unwound(client, cfg, market):
    """Мгновенный сбой связи на котировке короткой ноги (после исполненной покупки) — следующая попытка,
    а не откат всей сделки."""
    real, n = client.ticker, []

    def blip(sym):
        if sym == SHORT_C and not n:
            n.append(1)
            raise multileg.BybitNetworkError("timeout")
        return real(sym)
    client.ticker = blip
    res = ex(client, cfg).execute(bear_call())
    assert res.status == "complete" and not res.unwind and res.legs[0].error is None


def test_fmt_result_partial_lists_legs_and_diff(client, cfg, market):
    market.ioc_max[LONG_C] = Q
    res = ex(client, cfg).execute(bear_call(Q * 4))
    text = fmt_result(res, 7)
    assert text.startswith("⚠️ Исполнено частично · сигнал #7") and "Кол-во стратегии: 0.03 из 0.04" in text
    assert "Кредит" in text or "Дебет" in text


# ---------- по ревью ----------
def test_hung_ioc_is_unknown_not_refilled(client, cfg, market, monkeypatch):
    """IOC так и не завершился (New) — исход неизвестен: следующую попытку не шлём (иначе перебор)."""
    real = market._ioc

    def hang(o):
        real(o)
        o["orderStatus"] = "New"
    monkeypatch.setattr(market, "_ioc", hang)
    res = ex(client, cfg).execute(bear_call())
    assert res.status == "unknown" and "не завершён" in res.note and len(market.called("place_order")) == 1


def test_unexpected_error_after_buy_reports_left(client, cfg, market, monkeypatch):
    real = client.ticker

    def bad(sym):
        if sym == SHORT_C:
            raise KeyError("bid1Price")
        return real(sym)
    monkeypatch.setattr(client, "ticker", bad)
    res = ex(client, cfg).execute(bear_call())
    assert res.status == "unknown" and res.alert and "внутренняя ошибка KeyError" in res.note
    assert res.left == [f"Buy 0.01 {LONG_C}"]


def test_single_step_is_touch(make_client, cfg, market):
    """Одна попытка — по лучшей встречной цене (ask), не по mid."""
    c = with_cfg(cfg, multileg_steps=1)
    res = ex(make_client(c), c).execute(bear_call())
    t = market.tickers[LONG_C]
    price, ask = Decimal(market.called("place_order")[0]["price"]), Decimal(t["ask1Price"])
    assert ask <= price < ask + 5                                    # ask вне сетки — вверх к шагу 5
    assert res.status == "complete"


def test_untried_legs_marked(client, cfg, market):
    market.ioc_max[LONG_C] = Decimal(0)
    res = ex(client, cfg).execute(bear_call())
    short = res.legs[0]
    assert res.status == "none" and short.qty == 0 and short.error == "не выставлялась"
    assert "не исполнилось" in res.note


def test_fmt_avg_no_exponent(client, cfg, market):
    res = ex(client, cfg).execute(bear_call())
    assert "e+" not in fmt_result(res, 7).lower()


def test_multileg_slippage_above_max_is_clamped_not_fatal(cfg):
    """MULTILEG_SLIPPAGE_PCT ≥ MAX_SLIPPAGE_PCT — не ошибка запуска: попытки и так ограничены пределом от mark."""
    from optbot.bot import check_config
    c = with_cfg(cfg, tg_token="x", tg_trader_ids={10}, multileg_slippage_pct=20.0)
    assert not any("MULTILEG" in e for e in check_config(c))
    t = {"bid1Price": "95", "ask1Price": "105", "markPrice": "100"}
    assert multileg.step_price(t, "Buy", Decimal(1), c) <= Decimal(100) * (1 + c.max_slippage_pct / 100)


def test_execute_stale_card_refused(b, market):
    from datetime import datetime, timezone
    tg = TG()
    btn = _card(b, tg)
    u = tg.upd(TRADER, data=btn)
    u.callback_query.message.date = datetime.fromtimestamp(NOW / 1000 - 3600, timezone.utc)
    run(b.on_callback(u, tg.ctx()))                                  # «Обновить» был только что — свежая
    assert "Выставить сигнал #1" in tg.last("reply")
    b._card_shown[(TRADER, 1)] = (NOW / 1000 - 3600, {}, None)
    run(b.on_callback(u, tg.ctx()))
    assert "Карточке больше 30 мин" in tg.last()


def test_execute_card_refresh_unchanged_counts(b, market):
    """«Обновить» без изменений (Telegram не правит сообщение) — карточка всё равно свежая."""
    from datetime import datetime, timezone
    tg = TG()
    btn = _card(b, tg)
    b._card_shown[(TRADER, 1)] = (NOW / 1000 - 3600, {}, None)
    run(b.on_callback(tg.upd(TRADER, data="sig:BTC"), tg.ctx()))      # «Обновить»
    u = tg.upd(TRADER, data=btn)
    u.callback_query.message.date = datetime.fromtimestamp(NOW / 1000 - 3600, timezone.utc)
    run(b.on_callback(u, tg.ctx()))
    assert "Выставить сигнал #1" in tg.last("reply")


def test_execute_twice_refused_while_running_and_warned(b, market):
    tg = TG()
    btn = _card(b, tg)
    run(b.on_callback(tg.upd(TRADER, data=btn), tg.ctx()))
    pid1 = tg.buttons()[0].split(":")[1]
    run(b.on_callback(tg.upd(TRADER, data=btn), tg.ctx()))
    pid2 = tg.buttons()[0].split(":")[1]
    b._guard.claim_exec({"OTHER"})                                   # первое (или другой сигнал) исполняется
    confirm(b, tg, f"ok:{pid2}")
    assert "другая сделка по сигналу" in tg.last("edit") and market.called("place_order") == []
    b._guard.release_exec()
    confirm(b, tg, f"ok:{pid1}")
    assert "Исполнено полностью" in tg.last("edit") and not b._guard.busy
    run(b.on_callback(tg.upd(TRADER, data=btn), tg.ctx()))           # новое превью — предупреждение о повторе
    assert "Уже выставлялся 1 раз (исполнено 0.01)" in tg.last("reply")


# ---------- лимиты портфеля — по сделке целиком; занятые символы ----------
def _pre(client, cfg, legs=None):
    return multileg.precheck(client, cfg, legs or bear_call())


def test_daily_premium_whole_strategy(make_client, cfg, market):
    """Каждая нога в лимите премии за сутки, сумма — нет: отказ до отправки (раньше — продажа после покупки)."""
    quotes, _ = _pre(make_client(cfg), cfg)
    prem = sorted(q.req.premium for q in quotes)
    lim = prem[-1] + prem[0] / 2
    c = with_cfg(cfg, max_daily_premium_usdt=str(lim))
    client = make_client(c)
    for q in quotes:  # по отдельности — прошло бы
        client.prepare_order(multileg.OrderRequest(symbol=q.req.symbol, side=q.req.side, qty=q.req.qty,
                                                   price=q.req.price, time_in_force="IOC"))
    with pytest.raises(multileg.RiskError, match="все ноги"):
        _pre(client, c)
    assert market.called("place_order") == []


def test_delta_whole_strategy_allows_long_leg(make_client, cfg, market):
    """Покупка сама по себе вывела бы дельту за лимит, спред целиком — нет: превью и исполнение проходят."""
    market.greeks.append({"baseCoin": "BTC", "totalDelta": "0.0095"})
    c = with_cfg(cfg, max_net_delta={"BTC": "0.01"})
    client = make_client(c)
    with pytest.raises(multileg.RiskError, match="MAX_NET_DELTA"):   # старое поведение: нога отдельно
        client.prepare_order(multileg.OrderRequest(symbol=LONG_C, side="Buy", qty=Q, price=Decimal(800)))
    _, info = _pre(client, c)
    assert any(x.startswith("Δ BTC после") for x in info)
    res = ex(client, c).execute(bear_call())
    assert res.status == "complete"
    assert all(kw["reduceOnly"] is False for kw in market.called("place_order"))


def test_delta_whole_strategy_refused(make_client, cfg, market):
    market.greeks.append({"baseCoin": "BTC", "totalDelta": "0.0095"})
    c = with_cfg(cfg, max_net_delta={"BTC": "0.01"})
    with pytest.raises(multileg.RiskError, match="сделка целиком"):
        _pre(make_client(c), c, [(LONG_C, "Buy", Q, 1.0)])


def test_position_in_leg_symbol_refused(client, cfg, market):
    market.positions.append({"symbol": SHORT_C, "side": "Buy", "size": "0.01"})
    with pytest.raises(multileg.RiskError, match="уже есть позиция или ордер"):
        _pre(client, cfg)


def test_open_order_in_leg_symbol_refused_other_symbol_ok(client, cfg, market):
    other = f"BTC-{EXP}-60000-P-USDT"
    market.orders.append({"orderId": "x1", "orderLinkId": "man-x", "symbol": other, "side": "Buy", "qty": "0.01",
                          "leavesQty": "0.01", "price": "5", "reduceOnly": False, "cumExecQty": "0",
                          "createdTime": "0", "orderStatus": "New"})
    _pre(client, cfg)                                                # чужой ордер в другом символе — не мешает
    market.orders[0]["symbol"] = LONG_C
    with pytest.raises(multileg.RiskError, match=LONG_C):
        _pre(client, cfg)


def test_execute_rechecks_after_confirm(b, market):
    """Портфель изменился между превью и «Подтвердить» — повторная проверка, ничего не отправлено."""
    tg = TG()
    btn = _card(b, tg)
    run(b.on_callback(tg.upd(TRADER, data=btn), tg.ctx()))
    pid = tg.buttons()[0].split(":")[1]
    sym = next(ln.split()[2] for ln in tg.last("reply").split("\n") if ln.startswith(" BUY"))
    market.positions.append({"symbol": f"BTC-{sym}-USDT" if not sym.startswith("BTC") else sym + "-USDT",
                             "side": "Buy", "size": "0.01"})
    confirm(b, tg, f"ok:{pid}")
    assert "уже есть позиция или ордер" in tg.last("edit") and market.called("place_order") == []


# ---------- скорость попытки ----------
def test_attempt_one_ticker_and_immediate_poll(client, cfg, market):
    """Попытка: один запрос тикера (prepare_order берёт тот же), итог — сразу после отправки, без паузы."""
    sleeps = []
    j = Journal(cfg)
    e = MultiLegExecutor(client, cfg, link=lambda leg: j.register_signal_order(7, leg, TRADER, "user10", 1),
                         sleep=sleeps.append)
    market.calls.clear()
    market.ioc_max[LONG_C] = Decimal(0)                              # первая попытка (по ask) — без исполнения
    real = market._ioc

    def ioc(o):
        real(o)
        market.ioc_max.pop(LONG_C, None)
    market._ioc = ioc
    res = e.execute([(LONG_C, "Buy", Q, 1.0)])
    assert res.status == "complete" and res.legs[0].attempts == 2
    tickers = [kw for n, kw in market.calls if n == "get_tickers" and kw.get("symbol") == LONG_C]
    assert len(tickers) == 2                                         # по одному на попытку
    assert sleeps == [cfg.multileg_step_delay_s]                     # только пауза между попытками


def test_prepare_order_quote_reused_and_default_fresh(client, market):
    t = dict(market.tickers[LONG_C])
    market.calls.clear()
    client.prepare_order(multileg.OrderRequest(symbol=LONG_C, side="Buy", qty=Q, price=Decimal(700), quote=t))
    assert [n for n, kw in market.calls if n == "get_tickers" and kw.get("symbol") == LONG_C] == []
    client.prepare_order(multileg.OrderRequest(symbol=LONG_C, side="Buy", qty=Q, price=Decimal(700)))
    assert [n for n, kw in market.calls if n == "get_tickers" and kw.get("symbol") == LONG_C] == ["get_tickers"]


# ---------- ревью 2 ----------
def test_rejected_attempt_forgotten_in_journal(make_client, cfg, market):
    """Отказ проверки до биржи — запись order_links удаляется (не «призрак» по сигналу)."""
    c = with_cfg(cfg, max_order_qty={"BTC": "0.001"})
    j = Journal(c)
    e = MultiLegExecutor(make_client(c), c, link=lambda leg: j.register_signal_order(7, leg, TRADER, "u", 1),
                         forget=j.forget_order)
    res = e.execute(bear_call())
    assert res.status == "none" and q(c, "SELECT * FROM order_links") == []


def test_unwind_unexpected_error_reported(client, cfg, market, monkeypatch):
    market.ioc_max[SHORT_C] = Decimal(0)
    e = ex(client, cfg)
    real = e._fill

    def fill(f, reduce_only=False):
        if reduce_only:
            raise TypeError("bad response")
        return real(f, reduce_only)
    monkeypatch.setattr(e, "_fill", fill)
    res = e.execute(bear_call())
    # исход ушедшей попытки закрытия неизвестен — как в основной сделке: стоп, всё исполненное — админу
    assert res.status == "unknown" and res.alert and res.left == [f"Buy 0.01 {LONG_C}"]
    assert "внутренняя ошибка TypeError" in res.unwind[0].error and "закрытие лишнего" in res.note


def test_ratio_legs_refused(client, cfg, market):
    with pytest.raises(multileg.RiskError, match="разным количеством"):
        multileg.precheck(client, cfg, [(SHORT_C, "Sell", Q, 1.0), (LONG_C, "Buy", Q * 2, 1.0)])


def test_precheck_one_ticker_per_leg(client, cfg, market):
    market.calls.clear()
    multileg.precheck(client, cfg, bear_call())
    assert len([1 for n, kw in market.calls if n == "get_tickers"]) == 2


def test_execute_recheck_network_error_is_plain_error(b, market):
    tg = TG()
    btn = _card(b, tg)
    run(b.on_callback(tg.upd(TRADER, data=btn), tg.ctx()))
    pid = tg.buttons()[0].split(":")[1]
    market.fail["get_tickers"] = requests.exceptions.ReadTimeout("timeout")
    confirm(b, tg, f"ok:{pid}")
    out = tg.last("edit")
    assert "⛔ Ошибка" in out and "ордера не отправлялись" in out and "Не повторяйте" not in out
    assert market.called("place_order") == []


# ---------- ревью 3 ----------
def test_unwind_keeps_long_covering_unclosed_short(client, cfg, market, monkeypatch):
    """Кондор: колловая пара исполнилась, короткий пут — нет. Откуп короткого колла не прошёл — длинный колл
    не продаём (иначе непокрытая короткая), длинный пут — продаём."""
    p_long, p_short = f"BTC-{EXP}-60000-P-USDT", f"BTC-{EXP}-62000-P-USDT"
    legs = [(SHORT_C, "Sell", Q, 1.0), (LONG_C, "Buy", Q, 1.0), (p_short, "Sell", Q, 1.0), (p_long, "Buy", Q, 1.0)]
    real = market._ioc

    def ioc(o):
        block = (o["symbol"] == p_short and not o["reduceOnly"]) or (o["symbol"] == SHORT_C and o["reduceOnly"])
        if block:
            market.ioc_max[o["symbol"]] = Decimal(0)
        else:
            market.ioc_max.pop(o["symbol"], None)
        real(o)
    monkeypatch.setattr(market, "_ioc", ioc)
    res = ex(client, cfg).execute(legs)
    sold = {kw["symbol"] for kw in market.called("place_order") if kw["reduceOnly"] and kw["side"] == "Sell"}
    assert sold == {p_long}                                          # длинный колл не продавали
    assert res.status == "stuck" and res.alert
    assert f"Sell 0.01 {SHORT_C}" in res.left
    assert f"Buy 0.01 {LONG_C} (покрывает неоткупленную короткую)" in res.left


def test_step_price_clamped_to_max_slippage_from_mark(cfg):
    t = {"bid1Price": "110", "ask1Price": "130", "markPrice": "100"}   # mid 120 — на 20% выше mark
    p = multileg.step_price(t, "Buy", Decimal(1), cfg)
    assert p <= Decimal(100) * (1 + cfg.max_slippage_pct / 100)
    assert multileg.step_price(t, "Buy", Decimal(0), cfg) == p          # даже mid упирается в предел
    t2 = {"bid1Price": "95", "ask1Price": "105", "markPrice": "100"}
    assert multileg.step_price(t2, "Buy", Decimal(1), cfg) == Decimal("110.0")   # обычно — mid + 10%
    assert multileg.step_price(t2, "Sell", Decimal(1), cfg) == Decimal("90.0")


def test_attempt_numbers_skip_gaps(cfg):
    j = Journal(cfg)
    a, b2 = j.register_signal_order(7, 0, TRADER), j.register_signal_order(7, 0, TRADER)
    j.forget_order(a)
    c3 = j.register_signal_order(7, 0, TRADER)
    assert a.endswith("-1") and b2.endswith("-2") and c3.endswith("-3")
    assert len(q(cfg, "SELECT * FROM order_links")) == 2


def test_exchange_reject_keeps_link_row(client, cfg, market):
    from pybit.exceptions import InvalidRequestError
    market.fail["place_order"] = InvalidRequestError("req", "Insufficient balance", 110007, "0", {})
    j = Journal(cfg)
    e = MultiLegExecutor(client, cfg, link=lambda leg: j.register_signal_order(7, leg, TRADER), forget=j.forget_order)
    res = e.execute(bear_call())
    assert res.status == "none" and "110007" in res.note
    assert len(q(cfg, "SELECT * FROM order_links")) == 1             # биржа видела orderLinkId — номер не повторим


def test_manual_order_refused_on_executing_symbol(b, market):
    tg = TG()
    run(b.cmd_buy(tg.upd(TRADER), tg.ctx(LONG_C, "0.01", "100")))
    pid = tg.buttons()[0].split(":")[1]
    b._guard.claim_exec({LONG_C})
    confirm(b, tg, f"ok:{pid}")
    assert "сейчас исполняется сделка по сигналу" in tg.last("edit") and market.called("place_order") == []


def test_card_shown_pruned(b, market):
    b._card_shown = {(TRADER, 99): (NOW / 1000 - 7200, {}, None)}
    _card(b, TG())
    assert (TRADER, 99) not in b._card_shown and (TRADER, 1) in b._card_shown


# ---------- ревью 4 ----------
def test_unwind_cost_in_cash_and_text(client, cfg, market):
    market.ioc_max[SHORT_C] = Decimal(0)
    res = ex(client, cfg).execute(bear_call())
    assert res.status == "unwound" and res.actual_net == 0 and res.cash < 0      # купили дороже, чем продали
    assert "Закрытие лишнего: -" in fmt_result(res, 7)


def test_unknown_reports_cash_of_filled(client, cfg, market):
    real = market._ioc

    def ioc(o):
        real(o)
        if o["symbol"] == SHORT_C:
            o["orderStatus"] = "New"                                 # продажа «зависла»
    market._ioc = ioc
    res = ex(client, cfg).execute(bear_call())
    # продажа не завершена, но уже исполнилась — учтена: в итоге обе ноги и кредит (продажа дороже покупки)
    assert res.status == "unknown" and res.legs[0].filled == Q and res.qty == Q and res.cash > 0
    assert len(res.left) == 2 and "Премия по исполненному: +" in fmt_result(res, 7)


def test_clamped_price_stops_repeating(client, cfg, market):
    t = market.tickers[LONG_C]
    t.update(markPrice="100", bid1Price="130", ask1Price="150")      # mid 140 — далеко выше mark
    res = ex(client, cfg).execute([(LONG_C, "Buy", Q, 1.0)])
    f = res.legs[0]
    assert f.attempts == 1 and "цена не сдвигается к рынку" in f.error and res.status == "none"


def test_link_failure_is_leg_error_not_unknown(client, cfg, market):
    def bad(leg):
        raise ValueError("orderLinkId длиннее 36")
    res = MultiLegExecutor(client, cfg, link=bad).execute(bear_call())
    assert res.status == "none" and "orderLinkId" in res.note and market.called("place_order") == []


def test_preview_plan_prices_from_card(b, market):
    tg = TG()
    btn = _card(b, tg)
    ts, px, _ = b._card_shown[(TRADER, 1)]
    b._card_shown[(TRADER, 1)] = (ts, {k: 123.0 for k in px}, 77.0)                  # цены, которые трейдер видел на карточке
    run(b.on_callback(tg.upd(TRADER, data=btn), tg.ctx()))
    out = tg.last("reply")
    assert out.count("в сигнале 123") == 4 and "Обычно до ~" in out
    assert "макс. риск по сигналу 77.00" in out                      # риск — тоже с карточки


def test_execution_cash_recorded(b, market, monkeypatch):
    tg = TG()
    _confirm(b, tg, market, monkeypatch, block_calls=True)
    r = q(b.cfg, "SELECT status, actual_net, cash FROM signal_executions")
    assert r[0]["status"] == "unwound" and r[0]["actual_net"] == 0 and r[0]["cash"] < 0


# ---------- ревью 5 ----------
def test_margin_regular_refused_before_buys(client, cfg, market, monkeypatch):
    real = market.get_wallet_balance
    monkeypatch.setattr(market, "get_wallet_balance", lambda accountType: {
        "retCode": 0, "result": {"list": [{"totalEquity": "5", "totalAvailableBalance": "5", "coin": []}]}})
    with pytest.raises(multileg.RiskError, match="Маржи не хватит"):
        _pre(client, cfg)
    assert market.called("place_order") == [] and real


def test_margin_regular_estimate_formula(client, market):
    t = {"markPrice": "100", "indexPrice": "65000"}
    req = multileg.OrderRequest(symbol=SHORT_C, side="Sell", qty=Q, ticker=t)   # колл 67000: OTM 2000
    # 10%·65000 − 2000 = 4500 > 5%·65000 = 3250 → (100 + 4500)·0.01 = 46
    assert client.short_im_regular(req) == Decimal("46.00")


def test_margin_portfolio_not_estimated(client, cfg, market):
    market.margin_mode = "PORTFOLIO_MARGIN"
    _, info = _pre(client, cfg)
    assert any("PORTFOLIO_MARGIN" in x and "не проверена" in x for x in info)
    assert market.called("get_wallet_balance") == []


def test_unwind_unknown_stops_further_unwinds(client, cfg, market, monkeypatch):
    """Кондор не собрался; исход откупа короткой неизвестен — длинные не продаём, статус «неизвестно»."""
    p_long, p_short = f"BTC-{EXP}-60000-P-USDT", f"BTC-{EXP}-62000-P-USDT"
    legs = [(SHORT_C, "Sell", Q, 1.0), (LONG_C, "Buy", Q, 1.0), (p_short, "Sell", Q, 1.0), (p_long, "Buy", Q, 1.0)]
    real = market._ioc

    def ioc(o):
        if o["symbol"] == p_short and not o["reduceOnly"]:
            market.ioc_max[p_short] = Decimal(0)
        real(o)
        if o["symbol"] == SHORT_C and o["reduceOnly"]:
            o["orderStatus"] = "New"                                 # откуп «завис»
    monkeypatch.setattr(market, "_ioc", ioc)
    res = ex(client, cfg).execute(legs)
    assert res.status == "unknown" and "закрытие лишнего" in res.note
    assert not [kw for kw in market.called("place_order") if kw["reduceOnly"] and kw["side"] == "Sell"]
    assert res.legs[2].error == "не выставлялась" or res.legs[2].attempts > 0


def test_unknown_marks_untried_legs(client, cfg, market):
    market.fail["place_order"] = requests.exceptions.ReadTimeout("timeout")
    res = ex(client, cfg).execute(bear_call())
    assert res.status == "unknown" and res.legs[0].error == "не выставлялась" and res.legs[0].qty == 0


def test_tick_rounded_same_price_stops(client, cfg, market):
    t = market.tickers[LONG_C]
    t.update(markPrice="22", bid1Price="20", ask1Price="25")         # шаг 5: 22.5, 23.6, 24.7 → все 20
    res = ex(client, cfg).execute([(LONG_C, "Buy", Q, 1.0)])
    assert res.legs[0].attempts == 1 and "не сдвигается" in res.legs[0].error


def test_exec_refused_while_manual_inflight(b, market):
    tg = TG()
    btn = _card(b, tg)
    run(b.on_callback(tg.upd(TRADER, data=btn), tg.ctx()))
    pid = tg.buttons()[0].split(":")[1]
    sym = next(l["symbol"] for l in b.sigjournal.get(1)["legs"])
    b._guard.claim_manual(sym)
    confirm(b, tg, f"ok:{pid}")
    assert "отправляется ручной ордер" in tg.last("edit") and market.called("place_order") == []
    b._guard.release_manual(sym)
    assert not b._guard.busy


def test_manual_inflight_released(b, market):
    tg = TG()
    run(b.cmd_buy(tg.upd(TRADER), tg.ctx(LONG_C, "0.01", "100")))
    pid = tg.buttons()[0].split(":")[1]
    confirm(b, tg, f"ok:{pid}")
    assert "Ордер принят" in tg.last("edit") and not b._guard.busy



# ---------- ревью 6 ----------
def test_guard_exec_and_manual_exclusive():
    g = multileg.SymbolGuard()
    g.claim_manual("A")
    with pytest.raises(ValueError, match="ручной ордер"):
        g.claim_exec({"A", "B"})
    g.claim_exec({"B"})                                               # другой символ — можно
    with pytest.raises(ValueError, match="другая сделка"):
        g.claim_exec({"C"})
    with pytest.raises(ValueError, match="исполняется сделка"):
        g.claim_manual("B")
    g.claim_manual("C")
    g.release_exec()
    g.release_manual("A")
    g.release_manual("C")
    assert not g.busy


def test_card_freshness_per_chat(b, market):
    """Карточку того же сигнала показали в другом чате — старой карточке это свежести не даёт."""
    from datetime import datetime, timezone
    from tests.test_bot import GROUP
    tg = TG()
    btn = _card(b, tg)                                               # личка трейдера
    b._card_shown[(TRADER, 1)] = (NOW / 1000 - 3600, {}, None)
    b._card_shown[(GROUP, 1)] = (NOW / 1000, {}, None)                     # свежий показ — в группе
    u = tg.upd(TRADER, data=btn)
    u.callback_query.message.date = datetime.fromtimestamp(NOW / 1000 - 3600, timezone.utc)
    run(b.on_callback(u, tg.ctx()))
    assert "Карточке больше 30 мин" in tg.last()


def test_group_preview_hides_account_limits(make_client, cfg, market, monkeypatch):
    from tests.test_bot import GROUP
    monkeypatch.setattr("optbot.signals.time", NS(time=lambda: NOW / 1000, monotonic=__import__("time").monotonic))
    monkeypatch.setattr("optbot.bot.time.time", lambda: NOW / 1000)
    c = with_cfg(cfg, tg_allowed_chat_ids={GROUP}, sensitive_commands_dm_only=True)
    bot = OptionsBot(make_client(c), c)
    tg = TG()
    run(bot.cmd_signals(tg.upd(TRADER, chat="g"), tg.ctx("BTC")))
    btn = next(x for x in tg.buttons() if x.startswith("ex:"))
    run(bot.on_callback(tg.upd(TRADER, chat="g", data=btn), tg.ctx()))
    out = tg.last("reply")
    assert "подробности — в личке" in out and "доступных" not in out and "премия за сутки" not in out



# ---------- ревью 7 ----------
def test_same_rounded_price_skipped_not_stopped(client, cfg, market, monkeypatch):
    """ask за пределом (mid 60, предел 66, шаг 5): попытки 65, 65 — повтор пропускается, не останавливая ногу;
    к третьей mid вырос — 70 уходит и исполняется."""
    t = market.tickers[LONG_C]
    t.update(markPrice="60", bid1Price="50", ask1Price="70")
    real, n = market.get_tickers, []

    def tickers(**kw):
        if kw.get("symbol") == LONG_C:
            n.append(1)
            if len(n) == 3:
                t.update(markPrice="65", bid1Price="60", ask1Price="70")   # mid 65, предел 71.5
        return real(**kw)
    monkeypatch.setattr(market, "get_tickers", tickers)
    res = ex(client, cfg).execute([(LONG_C, "Buy", Q, 1.0)])
    prices = [kw["price"] for kw in market.called("place_order")]
    assert prices == ["65", "70"] and res.status == "complete" and res.legs[0].error is None


def test_unknown_instrument_is_leg_error(client, cfg, market, monkeypatch):
    real = client.resolve_symbol

    def gone(sym):
        if sym == SHORT_C:
            raise ValueError(f"Неизвестный инструмент: {sym}")
        return real(sym)
    monkeypatch.setattr(client, "resolve_symbol", gone)
    res = ex(client, cfg).execute(bear_call())
    assert res.status == "unwound" and "Неизвестный инструмент" in res.legs[0].error


def test_group_risk_refusal_hides_numbers(make_client, cfg, market, monkeypatch):
    from tests.test_bot import GROUP
    monkeypatch.setattr("optbot.signals.time", NS(time=lambda: NOW / 1000, monotonic=__import__("time").monotonic))
    monkeypatch.setattr("optbot.bot.time.time", lambda: NOW / 1000)
    monkeypatch.setattr(market, "get_wallet_balance", lambda accountType: {
        "retCode": 0, "result": {"list": [{"totalEquity": "5", "totalAvailableBalance": "5", "coin": []}]}})
    c = with_cfg(cfg, tg_allowed_chat_ids={GROUP}, sensitive_commands_dm_only=True)
    bot = OptionsBot(make_client(c), c)
    tg = TG()
    run(bot.cmd_signals(tg.upd(TRADER, chat="g"), tg.ctx("BTC")))
    btn = next(x for x in tg.buttons() if x.startswith("ex:"))
    run(bot.on_callback(tg.upd(TRADER, chat="g", data=btn), tg.ctx()))
    out = tg.last()
    assert "Отказ по состоянию счёта" in out and "доступно" not in out and "5.00" not in out
    run(bot.cmd_signals(tg.upd(TRADER), tg.ctx("BTC")))              # в личке — своя карточка, с цифрами
    run(bot.on_callback(tg.upd(TRADER, data=btn), tg.ctx()))
    assert "Маржи не хватит" in tg.last() and "доступно 5.00" in tg.last()


def test_expiry_rechecked_at_confirm(b, market, monkeypatch):
    tg = TG()
    btn = _card(b, tg)
    run(b.on_callback(tg.upd(TRADER, data=btn), tg.ctx()))
    pid = tg.buttons()[0].split(":")[1]
    row = b.sigjournal.get(1)
    monkeypatch.setattr("optbot.bot.time.time", lambda: (row["expiry_ms"] - 1800_000) / 1000)   # за 30 мин
    confirm(b, tg, f"ok:{pid}")
    assert "меньше часа" in tg.last("edit") and market.called("place_order") == []


def test_amend_refused_on_executing_symbol(b, market):
    tg = TG()
    run(b.cmd_buy(tg.upd(TRADER), tg.ctx(LONG_C, "0.01", "100")))
    confirm(b, tg, "ok:" + tg.buttons()[0].split(":")[1])
    oid = market.orders[-1]["orderId"]
    run(b.cmd_amend(tg.upd(TRADER), tg.ctx(LONG_C, oid, "105")))
    pid = tg.buttons()[0].split(":")[1]
    b._guard.claim_exec({LONG_C})
    confirm(b, tg, f"ok:{pid}")
    assert "исполняется сделка по сигналу" in tg.last("edit") and market.called("amend_order") == []


# ---------- ревью 8 ----------
def test_group_busy_symbol_refusal_hides_symbols(make_client, cfg, market, monkeypatch):
    from tests.test_bot import GROUP
    monkeypatch.setattr("optbot.signals.time", NS(time=lambda: NOW / 1000, monotonic=__import__("time").monotonic))
    monkeypatch.setattr("optbot.bot.time.time", lambda: NOW / 1000)
    c = with_cfg(cfg, tg_allowed_chat_ids={GROUP}, sensitive_commands_dm_only=True)
    bot = OptionsBot(make_client(c), c)
    tg = TG()
    run(bot.cmd_signals(tg.upd(TRADER, chat="g"), tg.ctx("BTC")))
    btn = next(x for x in tg.buttons() if x.startswith("ex:"))
    sym = bot.sigjournal.get(1)["legs"][0]["symbol"]
    market.positions.append({"symbol": sym, "side": "Buy", "size": "0.01"})
    run(bot.on_callback(tg.upd(TRADER, chat="g", data=btn), tg.ctx()))
    out = tg.last()
    assert "Отказ по состоянию счёта" in out and sym not in out


def test_unknown_qty_counts_untried_legs(client, cfg, market, monkeypatch):
    """Длинная исполнилась, затем исход неизвестен до короткой: стратегия — 0, а не «1 из 1»."""
    real = market._ioc

    def ioc(o):
        real(o)
        if o["symbol"] == LONG_C:
            o["orderStatus"] = "New"
    monkeypatch.setattr(market, "_ioc", ioc)
    res = ex(client, cfg).execute(bear_call())
    assert res.status == "unknown" and res.qty == 0 and res.planned_net == 0 and res.actual_net == 0


def test_leg_side():
    assert multileg.leg_side(1) == "Buy" and multileg.leg_side(-1) == "Sell"


# ---------- ревью 9 ----------
def test_daily_premium_counts_sells_at_bid(make_client, cfg, market):
    """Продажа идёт от bid: в лимит премии — по bid, а не по худшей (самой низкой) цене попыток."""
    quotes, _ = _pre(make_client(cfg), cfg, [(SHORT_C, "Sell", Q, 1.0)])
    bid = Decimal(market.tickers[SHORT_C]["bid1Price"])
    assert quotes[0].req.premium >= bid * Q * Decimal("0.999") > quotes[0].req.price * Q
    lim = (quotes[0].req.price * Q + bid * Q) / 2                    # между худшей и bid
    c = with_cfg(cfg, max_daily_premium_usdt=str(lim))
    with pytest.raises(PortfolioRiskError, match="Премия за сутки"):
        _pre(make_client(c), c, [(SHORT_C, "Sell", Q, 1.0)])


def test_margin_without_index_refused(client, market):
    req = multileg.OrderRequest(symbol=SHORT_C, side="Sell", qty=Q, ticker={"markPrice": "100", "indexPrice": ""})
    with pytest.raises(PortfolioRiskError, match="нет индекса"):
        client.short_im_regular(req)


def test_journal_written_after_guard_released(b, market, monkeypatch):
    seen = []
    real = b.sigjournal.record_execution

    def rec(*a, **k):
        seen.append(b._guard.busy)
        return real(*a, **k)
    monkeypatch.setattr(b.sigjournal, "record_execution", rec)
    tg = TG()
    btn = _card(b, tg)
    run(b.on_callback(tg.upd(TRADER, data=btn), tg.ctx()))
    confirm(b, tg, "ok:" + tg.buttons()[0].split(":")[1])
    assert seen == [False] and "Исполнено полностью" in tg.last("edit")


# ---------- ревью 10 ----------
def test_unwind_crosses_wide_spread(client, cfg, market, monkeypatch):
    """Крыло купили, короткая не исполнилась, а к откату спред крыла стал широким (bid 500 / ask 1000):
    продажа при откате доходит до bid, а не застревает у mid − 10% (675)."""
    market.ioc_max[SHORT_C] = Decimal(0)
    real = market._ioc

    def ioc(o):
        real(o)
        if o["symbol"] == LONG_C and o["side"] == "Buy" and o["cumExecQty"] != "0":
            market.tickers[LONG_C].update(bid1Price="500", ask1Price="1000", markPrice="550")
    monkeypatch.setattr(market, "_ioc", ioc)
    res = ex(client, cfg).execute(bear_call())
    assert res.status == "unwound" and not res.left
    sells = [Decimal(kw["price"]) for kw in market.called("place_order") if kw["reduceOnly"]]
    assert sells and sells[0] <= 500                                 # первая же — по bid, не mid − 10%


def test_step_price_reduce_only_targets_touch(cfg):
    t = {"bid1Price": "5", "ask1Price": "10", "markPrice": "7"}
    assert multileg.step_price(t, "Sell", Decimal(1), cfg, reduce_only=True) <= Decimal(5)
    assert multileg.step_price(t, "Sell", Decimal(1), cfg) > Decimal(5)   # открывающая — по-прежнему mid − 10%


def test_partially_filled_leg_not_marked_error(client, cfg, market, monkeypatch):
    """Первая попытка исполнила часть, дальше ask ушёл за предел — повтор той же цены пропущен, но нога
    с исполненным — не «ошибка»."""
    market.ioc_max[LONG_C] = Q
    t = market.tickers[LONG_C]
    t.update(markPrice="60", bid1Price="55", ask1Price="65")        # mid 60, предел 66: первая — 65
    real = market._ioc

    def ioc(o):
        real(o)
        t.update(bid1Price="52.5", ask1Price="67.5")                # ask за пределом: дальше 65, 65 — повторы
    monkeypatch.setattr(market, "_ioc", ioc)
    res = ex(client, cfg).execute([(LONG_C, "Buy", Q * 2, 1.0)])
    assert res.legs[0].filled == Q and res.legs[0].error is None and res.legs[0].attempts == 1

# ---------- ревью 11 ----------
def test_order_premium_limit_checks_sell_at_mid(make_client, cfg, market):
    """Продажа идёт от mid: лимит премии на ордер — по mid, иначе отказ случился бы уже после покупок."""
    quotes, _ = _pre(make_client(cfg), cfg, [(SHORT_C, "Sell", Q, 1.0)])
    lim = (quotes[0].req.price * Q + quotes[0].req.premium) / 2      # между худшей и mid
    c = with_cfg(cfg, max_order_premium_usdt=str(lim))
    with pytest.raises(multileg.RiskError, match="MAX_ORDER_PREMIUM_USDT"):
        _pre(make_client(c), c, [(SHORT_C, "Sell", Q, 1.0)])


def test_duplicate_symbol_legs_refused(client, cfg, market):
    with pytest.raises(multileg.RiskError, match="одном символе"):
        _pre(client, cfg, [(LONG_C, "Buy", Q, 1.0), (LONG_C, "Sell", Q, 1.0)])


# ---------- ревью 12 ----------
def test_same_price_retried_when_market_reaches_it(client, cfg, market, monkeypatch):
    """Цена попытки та же (шаг), но рынок до неё дошёл — повторить, а не пропустить."""
    t = market.tickers[LONG_C]
    t.update(markPrice="61", bid1Price="50", ask1Price="70")         # mid 60, предел 66 < ask: все шаги — 65 (шаг 5)
    real = market._ioc

    def ioc(o):
        real(o)
        t.update(bid1Price="55", ask1Price="65")                     # после первой попытки ask опустился до 65
    monkeypatch.setattr(market, "_ioc", ioc)
    res = ex(client, cfg).execute([(LONG_C, "Buy", Q, 1.0)])
    prices = [kw["price"] for kw in market.called("place_order")]
    assert prices[:2] == ["65", "65"] and res.status == "complete"


# ---------- ревью 13 ----------
def test_confirm_returns_before_execution(b, market):
    """Обработчик «Подтвердить» не ждёт исполнения — бот отвечает другим, пока идёт сделка."""
    tg = TG()
    btn = _card(b, tg)
    run(b.on_callback(tg.upd(TRADER, data=btn), tg.ctx()))
    pid = tg.buttons()[0].split(":")[1]
    tasks = []
    ctx = NS(args=[], bot=tg.bot, application=NS(create_task=tasks.append))
    run(b.on_callback(tg.upd(TRADER, data=f"ok:{pid}"), ctx))
    assert "⏳ Отправка…" in tg.last("edit") and market.called("place_order") == []
    assert len(tasks) == 1
    run(tasks[0])                                                    # сама сделка — фоновой задачей
    assert "Исполнено полностью" in tg.last("edit")
    for t in tasks[1:]:                                              # уведомления, поставленные сделкой
        run(t)


def test_unparsed_response_after_send_looked_up(client, cfg, market):
    """Ответ не разобран (ордер ушёл) — не «точно не на бирже»: исход по orderLinkId, исполнение учтено."""
    market.after["place_order"] = KeyError("result")
    res = ex(client, cfg).execute(bear_call())
    assert res.status == "complete" and res.legs[1].filled == Q


# ---------- цены: от ask/bid в пределе mid ± проскальзывание; журнал попыток; пассивная первая нога ----------
def test_step_price_starts_at_touch_within_cap(cfg):
    t = {"bid1Price": "57.5", "ask1Price": "62.5", "markPrice": "60"}  # mid 60, предел покупки 66, продажи 54
    assert multileg.step_price(t, "Buy", Decimal(0), cfg) == Decimal("62.5")
    assert multileg.step_price(t, "Buy", Decimal(1), cfg) == Decimal("66.0")
    assert multileg.step_price(t, "Sell", Decimal(0), cfg) == Decimal("57.5")
    assert multileg.step_price(t, "Sell", Decimal(1), cfg) == Decimal("54.0")
    wide = {"bid1Price": "50", "ask1Price": "70", "markPrice": "60"}   # ask за пределом — сам предел
    assert multileg.step_price(wide, "Buy", Decimal(0), cfg) == Decimal("66.0")


def test_precheck_refuses_touch_beyond_limit(client, cfg, market):
    """Лучшая цена хоть одной ноги за пределом mid ± проскальзывание — стратегию не открывать, ордеров нет."""
    t = market.tickers[SHORT_C]
    mid = (Decimal(t["bid1Price"]) + Decimal(t["ask1Price"])) / 2
    t.update(bid1Price=str(mid * Decimal("0.8")), ask1Price=str(mid * Decimal("1.2")))  # bid на 20% ниже mid
    with pytest.raises(multileg.RiskError, match="bid .* за пределом .* стратегия не открывается"):
        _pre(client, cfg)
    t.update(bid1Price="0")
    with pytest.raises(multileg.RiskError, match="нет bid"):
        _pre(client, cfg)
    assert market.called("place_order") == []


def test_precheck_refuses_ask_beyond_limit_on_buy(client, cfg, market):
    t = market.tickers[LONG_C]
    t.update(ask1Price=str(Decimal(t["ask1Price"]) * Decimal("1.5")))
    with pytest.raises(multileg.RiskError, match="ask .* за пределом"):
        _pre(client, cfg)


def test_attempts_logged_with_mid_price_and_gap(client, cfg, market):
    res = ex(client, cfg).execute(bear_call())
    short, long_ = res.legs
    assert [x["kind"] for x in long_.tries] == ["ioc"] and long_.tries[0]["n"] == 1
    tr = long_.tries[0]
    t = market.tickers[LONG_C]
    assert tr["mid"] == pytest.approx(float((Decimal(t["bid1Price"]) + Decimal(t["ask1Price"])) / 2))
    assert tr["filled"] == float(Q) and tr["avg"] == pytest.approx(float(t["ask1Price"]))
    assert tr["price"] >= float(t["ask1Price"])
    assert long_.gap_ms is None and short.gap_ms is not None and short.gap_ms >= 0  # пауза — у второй ноги
    text = fmt_result(res, 7)
    assert "Пауза между ногами, с:" in text and " mid " in text


def test_attempt_log_saved_in_journal(b, market):
    tg = TG()
    btn = _card(b, tg)
    run(b.on_callback(tg.upd(TRADER, data=btn), tg.ctx()))
    confirm(b, tg, tg.buttons()[0])
    import json
    row = q(b.cfg, "SELECT legs FROM signal_executions")[0]
    legs = json.loads(row["legs"])
    assert all(x["tries"] and {"n", "mid", "price", "filled", "avg", "ts"} <= set(x["tries"][0]) for x in legs)
    assert sum(x["gap_ms"] is not None for x in legs) == len(legs) - 1


def _passive_ex(client, cfg, on_sleep, secs=3):
    c = with_cfg(cfg, passive_first_leg_seconds=secs)
    j = Journal(c)
    return MultiLegExecutor(client, c, link=lambda leg: j.register_signal_order(7, leg, TRADER, "user10", 1),
                            sleep=on_sleep)


def test_passive_first_leg_filled_while_resting(client, cfg, market):
    """PostOnly по mid исполнился за время ожидания — покупка без IOC, дальше короткая нога."""
    def sleep(d):
        if market.orders:
            market.fill_resting(market.orders[0]["orderLinkId"], Q)
    res = _passive_ex(client, cfg, sleep).execute(bear_call())
    kw = market.called("place_order")
    t = market.tickers[LONG_C]
    mid = (Decimal(t["bid1Price"]) + Decimal(t["ask1Price"])) / 2
    assert kw[0]["timeInForce"] == "PostOnly" and Decimal(kw[0]["price"]) <= mid
    assert [k["timeInForce"] for k in kw[1:]] == ["IOC"] and kw[1]["side"] == "Sell"
    assert res.status == "complete" and res.legs[1].tries[0]["kind"] == "passive"
    assert market.called("cancel_order") == []


def test_passive_not_filled_cancelled_then_ioc(client, cfg, market):
    res = _passive_ex(client, cfg, lambda d: None).execute(bear_call())
    kw = market.called("place_order")
    assert [k["timeInForce"] for k in kw] == ["PostOnly", "IOC", "IOC"]
    assert len(market.called("cancel_order")) == 1 and market.orders == []
    assert res.status == "complete" and res.legs[1].attempts == 2
    assert [x["kind"] for x in res.legs[1].tries] == ["passive", "ioc"]


def test_passive_partial_then_ioc_for_rest(client, cfg, market):
    def sleep(d):
        o = market.orders[0] if market.orders else None
        if o and o["cumExecQty"] == "0":
            market.fill_resting(o["orderLinkId"], Q)
    res = _passive_ex(client, cfg, sleep).execute(bear_call(Q * 3))
    kw = market.called("place_order")
    assert kw[0]["timeInForce"] == "PostOnly" and kw[1]["timeInForce"] == "IOC"
    assert Decimal(kw[1]["qty"]) == Q * 2                           # IOC — только на неисполненное
    assert res.status == "complete" and res.legs[1].filled == Q * 3


def test_passive_cancel_unknown_stops(client, cfg, market):
    """Снять не удалось и ордер ещё стоит — исход неизвестен, IOC поверх не ставим."""
    market.cancel_order = lambda **kw: (_ for _ in ()).throw(requests.exceptions.ReadTimeout("t"))
    res = _passive_ex(client, cfg, lambda d: None).execute(bear_call())
    assert res.status == "unknown" and res.alert
    assert [k["timeInForce"] for k in market.called("place_order")] == ["PostOnly"]


def test_passive_skipped_for_sell_first_and_when_off(client, cfg, market):
    _passive_ex(client, cfg, lambda d: None).execute([(SHORT_C, "Sell", Q, 1.0)])
    assert [k["timeInForce"] for k in market.called("place_order")] == ["IOC"]
    market.calls.clear()
    _passive_ex(client, cfg, lambda d: None, secs=0).execute(bear_call())
    assert "PostOnly" not in [k["timeInForce"] for k in market.called("place_order")]


def test_passive_price_below_ask_on_tick(cfg):
    t = {"bid1Price": "60", "ask1Price": "65", "markPrice": "62"}      # mid 62.5, шаг 5 → 60
    assert multileg.passive_price(t, cfg, Decimal(5)) == Decimal(60)
    t = {"bid1Price": "60", "ask1Price": "61", "markPrice": "60"}      # mid 60.5 → 60 < ask
    assert multileg.passive_price(t, cfg, Decimal("0.5")) == Decimal("60.5")
    t = {"bid1Price": "0", "ask1Price": "5", "markPrice": "5"}         # mid=mark=ask → ниже ask
    assert multileg.passive_price(t, cfg, Decimal(5)) == Decimal(0)


def test_preview_mentions_passive(b, market):
    b.cfg = with_cfg(b.cfg, passive_first_leg_seconds=5)
    tg = TG()
    btn = _card(b, tg)
    run(b.on_callback(tg.upd(TRADER, data=btn), tg.ctx()))
    out = tg.last()
    assert "PostOnly по mid на 5 с" in out and "IOC от ask/bid" in out


# ---------- ревью 14 ----------
def test_passive_cancelled_by_link_even_if_lookups_fail(client, cfg, market, monkeypatch):
    """Опросы во время ожидания сбоят (ордер не найден) — снять всё равно по orderLinkId: в стакане не остаётся."""
    real, n = client.find_order_by_link, []

    def flaky(sym, link):
        n.append(1)
        if len(n) <= 3:                                              # все опросы ожидания (3 с)
            raise multileg.BybitNetworkError("timeout")
        return real(sym, link)
    monkeypatch.setattr(client, "find_order_by_link", flaky)
    res = _passive_ex(client, cfg, lambda d: None).execute(bear_call())
    assert market.called("cancel_order")[0]["orderLinkId"] and market.orders == []
    assert res.status == "complete"


def test_passive_cancel_retried_after_network_error(client, cfg, market):
    market.fail["cancel_order"] = requests.exceptions.ReadTimeout("t")   # первое снятие — сбой
    res = _passive_ex(client, cfg, lambda d: None).execute(bear_call())
    assert len(market.called("cancel_order")) == 2 and market.orders == [] and res.status == "complete"


def test_passive_unknown_warns_order_may_rest(client, cfg, market):
    market.cancel_order = lambda **kw: (_ for _ in ()).throw(requests.exceptions.ReadTimeout("t"))
    res = _passive_ex(client, cfg, lambda d: None).execute(bear_call())
    assert res.status == "unknown" and "может стоять в стакане" in res.note
    assert res.legs[1].tries and res.legs[1].tries[-1]["kind"] == "passive" and "error" in res.legs[1].tries[-1]


def test_rejected_attempt_after_send_logged(client, cfg, market):
    """Биржа отклонила ушедшую попытку — она в журнале попыток (с ошибкой); начало ноги — с неё."""
    from pybit.exceptions import InvalidRequestError
    market.after["place_order"] = InvalidRequestError("req", "bad", 10001, "0", {})
    res = ex(client, cfg).execute([(LONG_C, "Buy", Q, 1.0)])
    f = res.legs[0]
    assert f.tries and f.tries[0].get("error") and f.started_ms is not None


def test_refused_before_send_not_leg_start(client, cfg, market, monkeypatch):
    """Отказ проверки до отправки — не начало ноги: время старта — от реальной отправки, в журнале её нет."""
    real, n = client.prepare_order, []

    def prep(req):
        n.append(1)
        if len(n) == 1:
            raise multileg.RiskError("отказ")
        return real(req)
    monkeypatch.setattr(client, "prepare_order", prep)
    res = ex(client, cfg).execute([(LONG_C, "Buy", Q, 1.0)])
    f = res.legs[0]
    assert market.called("place_order") == [] and f.started_ms is None and f.tries == []
    assert f.error == "отказ"


def test_nan_in_tries_does_not_lose_record(cfg):
    from optbot.sigjournal import SignalJournal
    f = multileg.LegFill(0, LONG_C, "Buy", Q, 1.0, filled=Q, value=Q * 100, attempts=1,
                         tries=[dict(n=1, kind="ioc", ts=1, mid=float("nan"), bid=0.0, ask=float("inf"),
                                     price=100.0, filled=0.01, avg=100.0)])
    res = multileg.ExecResult("complete", Q, Q, [f])
    SignalJournal(cfg).record_execution(1, res, TRADER, "u", 1)
    import json
    row = q(cfg, "SELECT legs FROM signal_executions")[0]
    assert json.loads(row["legs"])[0]["tries"][0]["mid"] is None


# ---------- ревью 15 ----------
def test_unknown_partial_fill_counted_in_left(client, cfg, market):
    """IOC первой ноги не завершён, но уже исполнил часть — она в «осталось» и в итоге, а не только в тексте."""
    real = market._ioc

    def ioc(o):
        real(o)
        o.update(orderStatus="PartiallyFilled", cumExecQty="0.01", cumExecValue="7")
    market._ioc = ioc
    res = ex(client, cfg).execute(bear_call(Q * 2))
    f = res.legs[1]
    assert res.status == "unknown" and f.filled == Q and any(LONG_C in x and "0.01" in x for x in res.left)
    assert f.tries[-1]["filled"] == 0.01 and "error" in f.tries[-1]


def test_skipped_repeat_waits_step_delay(client, cfg, market):
    """Повтор той же цены пропускается, но с паузой — у рынка есть время дойти до неё."""
    sleeps = []
    t = market.tickers[LONG_C]
    t.update(markPrice="60", bid1Price="50", ask1Price="70")         # все шаги — 65, ask 70 не достаётся
    j = Journal(cfg)
    e = MultiLegExecutor(client, cfg, link=lambda leg: j.register_signal_order(7, leg, TRADER, "user10", 1),
                         sleep=sleeps.append)
    e.execute([(LONG_C, "Buy", Q, 1.0)])
    assert sleeps.count(cfg.multileg_step_delay_s) == cfg.multileg_steps - 1


def test_passive_rejected_after_send_logged(client, cfg, market):
    from pybit.exceptions import InvalidRequestError
    market.after["place_order"] = InvalidRequestError("req", "bad", 10001, "0", {})
    res = _passive_ex(client, cfg, lambda d: None).execute(bear_call())
    tr = res.legs[1].tries
    assert tr[0]["kind"] == "passive" and tr[0].get("error") and [x["n"] for x in tr] == list(range(1, len(tr) + 1))


# ---------- ревью 16 ----------
def test_passive_partial_seen_counted_when_link_lost(client, cfg, market, monkeypatch):
    """Во время ожидания PostOnly частично исполнился, затем связь пропала (снять и опросить нельзя) —
    исполненное из последнего увиденного состояния учтено в итоге и «осталось»."""
    state = {"down": False}
    real = client.find_order_by_link

    def find(sym, link):
        if state["down"]:
            raise multileg.BybitNetworkError("down")
        return real(sym, link)

    def sleep(d):
        o = market.orders[0] if market.orders else None
        if o and o["cumExecQty"] == "0":
            market.fill_resting(o["orderLinkId"], Q)
        elif o:
            state["down"] = True
    monkeypatch.setattr(client, "find_order_by_link", find)
    market.cancel_order = lambda **kw: (_ for _ in ()).throw(requests.exceptions.ReadTimeout("t"))
    res = _passive_ex(client, cfg, sleep).execute(bear_call(Q * 3))
    assert res.status == "unknown" and res.legs[1].filled == Q
    assert any(LONG_C in x and "0.01" in x for x in res.left)


def test_result_edit_failure_still_alerts_and_sends(b, market, monkeypatch):
    """Итог не удалось показать правкой сообщения — срочное админу и итог отдельным сообщением всё равно уходят."""
    from telegram.error import TelegramError
    real = b.edit

    async def edit(q, text, markup=None):
        if "Не собралось" in text:
            raise TelegramError("message to edit not found")
        return await real(q, text, markup)
    monkeypatch.setattr(b, "edit", edit)
    tg = TG()
    sends = _confirm(b, tg, market, monkeypatch, block_calls=True)
    assert [c for c, _ in sends] == [50] and "Не собралось" in sends[0][1]      # срочное админу
    assert any(k == "send" and "Не собралось" in t for k, t, _ in tg.out)    # итог — отдельным сообщением


# ---------- ревью 17 ----------
def test_tail_error_becomes_unknown_with_left(client, cfg, market, monkeypatch):
    """Непредвиденная ошибка после исполненных ног (подсчёт итога) — «исход неизвестен» с исполненным,
    а не исключение мимо журнала и срочного."""
    market.ioc_max[SHORT_C] = Decimal(0)                             # будет откат — в нём _kind
    monkeypatch.setattr(multileg, "_kind", lambda sym: 1 / 0)
    res = ex(client, cfg).execute(bear_call())
    assert res.status == "unknown" and res.alert and "внутренняя ошибка итога ZeroDivisionError" in res.note
    assert res.left == [f"Buy 0.01 {LONG_C}"]


def test_tail_error_fallback_when_unknown_also_fails(client, cfg, market, monkeypatch):
    e = ex(client, cfg)
    monkeypatch.setattr(e, "_nets", lambda res: 1 / 0)
    res = e.execute(bear_call())
    assert res.status == "unknown" and res.alert and len(res.left) == 2


def test_tail_fallback_left_net_of_unwind(client, cfg, market, monkeypatch):
    """Двойной сбой итога после отката — закрытое reduce-only не попадает в «осталось»."""
    market.ioc_max[SHORT_C] = Decimal(0)
    e = ex(client, cfg)
    monkeypatch.setattr(e, "_nets", lambda res: 1 / 0)
    res = e.execute(bear_call())
    assert res.status == "unknown" and res.unwind and res.left == []
