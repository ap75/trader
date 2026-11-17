"""Роль администратора и автосигналы ему в личку: сводка раз в сутки и сообщение о смене рекомендации."""
import asyncio
import dataclasses
import math
from types import SimpleNamespace as NS

import pytest
from telegram.error import Forbidden

from optbot import bot as botmod
from optbot import config, db
from optbot.bot import OptionsBot, check_config
from optbot.signals import SignalEngine, fmt_signal_brief, signal_key
from tests.fakes import NOW, set_chain, set_klines
from tests.helpers import with_cfg
from tests.test_bot import GROUP, GROUP2, TG, TRADER, VIEWER  # noqa: F401

ADMIN = 50


def run(coro):
    return asyncio.run(coro)


def q(cfg, sql, *a):
    conn = db.connect(cfg.db_file)
    try:
        return [dict(r) for r in conn.execute(sql, a)]
    finally:
        conn.close()


class Sent:
    """Подставной send_message: кому и что (с кнопками)."""

    def __init__(self, fail: set[int] = frozenset()):
        self.out: list[tuple[int, str, list]] = []
        self.fail = set(fail)

    async def __call__(self, chat_id, text, parse_mode=None, reply_markup=None):
        if chat_id in self.fail:
            raise Forbidden("bot was blocked by the user")
        btns = [b.callback_data for r in reply_markup.inline_keyboard for b in r] if reply_markup else []
        self.out.append((chat_id, text, btns))


@pytest.fixture
def market(fake):
    set_chain(fake, iv=0.6)
    set_klines(fake, rv=0.4)
    return fake


@pytest.fixture
def acfg(cfg):
    return with_cfg(cfg, tg_admin_ids={ADMIN})


@pytest.fixture
def sent():
    return Sent()


@pytest.fixture
def b(make_client, acfg, market, monkeypatch, sent):
    monkeypatch.setattr("optbot.signals.time", NS(time=lambda: NOW / 1000, monotonic=__import__("time").monotonic))
    bot = OptionsBot(make_client(acfg), acfg)
    bot._app = NS(bot=NS(send_message=sent))
    return bot


@pytest.fixture
def sig(make_client, cfg, market):
    s = SignalEngine(make_client(cfg), cfg).compute("BTC", NOW)
    assert s.strategy == "iron_condor" and s.legs
    return s


def none_of(sig, base="BTC"):
    return dataclasses.replace(sig, strategy=None, legs=[], profile=None, no_signal="IV в обычном диапазоне",
                               metrics=dataclasses.replace(sig.metrics, base=base))


def feed(b, monkeypatch, by_base: dict):
    """Подменить расчёт: актив -> сигнал (by_base меняют между проверками)."""
    async def fake_signal(base, fresh=False):
        assert fresh                                            # автосигналы — мимо кэша
        return dataclasses.replace(by_base[base]), False
    monkeypatch.setattr(b, "_signal", fake_signal)


def baseline(b):
    """Точка отсчёта — после AUTO_CHANGE_CONFIRM одинаковых проверок подряд."""
    for _ in range(botmod.AUTO_CHANGE_CONFIRM):
        run(b._auto_change_job(NS()))
    assert all(st.ready for st in b._auto.values())


def clock(monkeypatch, t: list[float]):
    monkeypatch.setattr(botmod.time, "time", lambda: t[0])


# ---------- настройки и роль ----------
@pytest.mark.parametrize("v,want", [("", (9, 0)), ("07:30", (7, 30)), ("off", None), ("0", None),
                                    ("25:00", (9, 0)), ("9", (9, 0)), ("09:5", (9, 0)),
                                    ("²:00", (9, 0))])
def test_daily_time_setting(monkeypatch, caplog, v, want):
    monkeypatch.setenv("AUTO_SIGNALS_DAILY_UTC", v)
    assert config._hhmm("AUTO_SIGNALS_DAILY_UTC", "09:00") == want
    assert ("ЧЧ:ММ" in caplog.text) == (v in ("25:00", "9", "09:5", "²:00"))  # не падение при импорте


def test_admin_trades_and_gets_notifications(make_client, acfg):
    bot = OptionsBot(make_client(acfg), acfg)
    tg = TG()
    w = bot.who(tg.upd(ADMIN))
    assert w.can_view and w.can_trade and w.is_admin
    assert not bot.who(tg.upd(TRADER)).is_admin
    run(bot.cmd_id(tg.upd(ADMIN), tg.ctx()))
    assert "права: администратор" in tg.last()
    assert not bot.who(tg.upd(VIEWER)).can_trade                 # остальным права не меняются
    assert bot._targets() == [GROUP2, GROUP, TRADER, ADMIN]      # исполнения — и администратору в личку


def test_check_config_admin(cfg):
    only_admin = with_cfg(cfg, tg_trader_ids=set(), tg_viewer_ids=set(), tg_admin_ids={ADMIN})
    assert check_config(only_admin) == []
    assert any("TELEGRAM_ADMIN_IDS" in e for e in check_config(with_cfg(cfg, tg_admin_ids={GROUP})))
    assert any("служебные" in e for e in check_config(with_cfg(cfg, tg_admin_ids={777000})))


# ---------- краткая строка ----------
def test_brief(sig):
    text = fmt_signal_brief(sig)
    head, *legs, money = text.split("\n")
    assert head.startswith("BTC: Железный кондор") and "кол-во 0.01" in head
    assert len(legs) == 4 and legs[0].split()[0] == "SELL" and "BTC-" not in legs[0]
    assert money.startswith("  Кредит") and money.endswith("USDT") and "риск" in money
    assert fmt_signal_brief(none_of(sig)) == "BTC: сигнала нет — IV в обычном диапазоне"
    nobuild = dataclasses.replace(sig, legs=[], no_signal="нет экспирации в окне")
    assert signal_key(nobuild) is None and "но не собрать — нет экспирации" in fmt_signal_brief(nobuild)
    unlimited = dataclasses.replace(sig, profile=dataclasses.replace(sig.profile, max_profit=math.inf,
                                                                     max_loss=math.inf))
    assert "прибыль без ограничения" in fmt_signal_brief(unlimited)
    assert "риск без ограничения" in fmt_signal_brief(unlimited)        # не 'inf' и не падение


# ---------- сводка ----------
def test_daily_summary_to_admin_only_and_journaled(b, acfg, sent):
    run(b._auto_daily_job(NS()))
    assert [c for c, _, _ in sent.out] == [ADMIN]                    # в группы и трейдерам — нет
    text = sent.out[0][1]
    assert "Сигналы на" in text and "BTC: Железный кондор" in text  # номер — после доставки, в сообщении его нет
    assert "ETH: сигнала нет" in text and "SOL: сигнала нет" in text
    r = q(acfg, "SELECT source, user_id, chat_id, shown FROM signals")
    assert r == [{"source": "auto", "user_id": ADMIN, "chat_id": ADMIN, "shown": 1}]
    assert b._auto == {}                            # сводка — снимок: точку отсчёта смены не задаёт


def test_daily_summary_survives_calc_error_and_undelivered(b, sent, monkeypatch, caplog):
    real = b._signal

    async def flaky(base, fresh=False):
        if base == "ETH":
            raise RuntimeError("boom")
        return await real(base, fresh)
    monkeypatch.setattr(b, "_signal", flaky)
    sent.fail = {ADMIN}
    run(b._auto_daily_job(NS()))
    assert "ETH не посчитан" in caplog.text and "никому не доставлена" in caplog.text
    assert b._auto == {}                            # не дошло — точка отсчёта смены не сдвигается
    assert q(b.cfg, "SELECT COUNT(*) n FROM signals")[0]["n"] == 0   # недоставленное — не показ
    sent.fail = set()
    run(b._auto_daily_job(NS()))
    assert "ETH: не посчитан — ошибка" in sent.out[-1][1]


def test_no_admins_no_messages(make_client, cfg, market, sent):
    bot = OptionsBot(make_client(cfg), cfg)
    bot._app = NS(bot=NS(send_message=sent))
    assert run(bot._send_admins("x")) == [] and sent.out == []


# ---------- смена рекомендации ----------
def test_change_alerts_with_hold(b, acfg, sig, sent, monkeypatch):
    t = [1_000_000.0]
    clock(monkeypatch, t)
    cur = {"BTC": sig, "ETH": none_of(sig, "ETH"), "SOL": none_of(sig, "SOL")}
    feed(b, monkeypatch, cur)
    baseline(b)
    assert sent.out == []                                       # первая проверка — точка отсчёта
    run(b._auto_change_job(NS()))
    assert sent.out == []                                       # без смены — тишина

    cur["BTC"] = none_of(sig)
    t[0] += 900
    run(b._auto_change_job(NS()))
    assert sent.out == []                                       # один срез — ещё не смена
    run(b._auto_change_job(NS()))
    assert len(sent.out) == 1                                   # продержалась вторую проверку — пишем
    chat, text, btns = sent.out[0]
    assert chat == ADMIN and "BTC: рекомендация сменилась — Железный кондор → сигнала нет" in text
    assert text.startswith("<pre>[demo]") and "Сигнала нет" in text and "/signals BTC" in text
    assert btns == []                                           # «Обновить» стёрло бы, что на что сменилось

    cur["BTC"] = sig                                            # вернулось внутри паузы — не пишем
    t[0] += 3600
    run(b._auto_change_job(NS()))
    run(b._auto_change_job(NS()))
    assert len(sent.out) == 1
    t[0] += acfg.auto_signals_change_hours * 3600                # пауза прошла, а отличие осталось — пишем
    run(b._auto_change_job(NS()))
    assert len(sent.out) == 2 and "сигнала нет → Железный кондор" in sent.out[1][1]
    assert q(acfg, "SELECT source, user_id FROM signals") == [{"source": "auto", "user_id": ADMIN}]  # после доставки


def test_change_flip_back_within_hold_is_silent(b, sig, sent, monkeypatch):
    t = [1_000_000.0]
    clock(monkeypatch, t)
    cur = {"BTC": sig, "ETH": none_of(sig, "ETH"), "SOL": none_of(sig, "SOL")}
    feed(b, monkeypatch, cur)
    baseline(b)
    cur["BTC"] = none_of(sig)
    run(b._auto_change_job(NS()))
    run(b._auto_change_job(NS()))
    assert len(sent.out) == 1
    cur["BTC"] = sig
    run(b._auto_change_job(NS()))
    run(b._auto_change_job(NS()))
    cur["BTC"] = none_of(sig)
    t[0] += 1000 * 3600
    run(b._auto_change_job(NS()))                               # то же, что уже сообщено, — не смена
    run(b._auto_change_job(NS()))
    assert len(sent.out) == 1


def test_change_single_glitch_is_silent(b, sig, sent, monkeypatch):
    """Один срез без сделки (широкий спред, нет котировки) между нормальными — не смена."""
    cur = {"BTC": sig, "ETH": none_of(sig, "ETH"), "SOL": none_of(sig, "SOL")}
    feed(b, monkeypatch, cur)
    baseline(b)
    for _ in range(3):
        cur["BTC"] = none_of(sig)
        run(b._auto_change_job(NS()))
        cur["BTC"] = sig
        run(b._auto_change_job(NS()))
    assert sent.out == []


def test_change_undelivered_retried(b, sig, sent, monkeypatch, caplog):
    t = [1_000_000.0]
    clock(monkeypatch, t)
    cur = {"BTC": sig, "ETH": none_of(sig, "ETH"), "SOL": none_of(sig, "SOL")}
    feed(b, monkeypatch, cur)
    baseline(b)
    cur["BTC"] = none_of(sig)
    sent.fail = {ADMIN}
    run(b._auto_change_job(NS()))
    run(b._auto_change_job(NS()))
    assert "повтор через 60 мин" in caplog.text and b._auto["BTC"].reported == "iron_condor"
    sent.fail = set()
    run(b._auto_change_job(NS()))
    assert sent.out == []                                       # до повтора — не долбим каждые 15 мин
    t[0] += botmod.AUTO_RETRY_S
    run(b._auto_change_job(NS()))
    assert len(sent.out) == 1 and b._auto["BTC"].reported is None


def test_daily_summary_flap_is_not_a_baseline(b, sig, sent, monkeypatch):
    """Сбойный замер в момент сводки (нет котировки) не становится точкой отсчёта: ложного «нет → есть» нет."""
    cur = {"BTC": sig, "ETH": none_of(sig, "ETH"), "SOL": none_of(sig, "SOL")}
    feed(b, monkeypatch, cur)
    baseline(b)                                                 # кондор подтверждён
    cur["BTC"] = none_of(sig)
    run(b._auto_daily_job(NS()))                                # в сводке — «сигнала нет»
    cur["BTC"] = sig
    for _ in range(3):
        run(b._auto_change_job(NS()))
    assert len(sent.out) == 1 and "Сигналы на" in sent.out[0][1]  # только сводка


def test_baseline_after_restart_needs_confirmation(b, sig, sent, monkeypatch):
    """Первый срез после старта сбойный («нет») — точкой отсчёта не становится, сообщения нет."""
    cur = {"BTC": none_of(sig), "ETH": none_of(sig, "ETH"), "SOL": none_of(sig, "SOL")}
    feed(b, monkeypatch, cur)
    run(b._auto_change_job(NS()))
    assert not b._auto["BTC"].ready
    cur["BTC"] = sig
    for _ in range(3):
        run(b._auto_change_job(NS()))
    assert sent.out == [] and b._auto["BTC"].ready and b._auto["BTC"].reported == "iron_condor"


def test_change_calc_error_skips_base(b, sig, sent, monkeypatch, caplog):
    async def boom(base, fresh=False):
        raise RuntimeError("bybit down")
    monkeypatch.setattr(b, "_signal", boom)
    run(b._auto_change_job(NS()))
    assert "смена не проверена" in caplog.text and b._auto == {} and sent.out == []


def test_change_streak_broken_by_failed_check(b, sig, sent, monkeypatch):
    """«Две проверки подряд» — без пропусков: сбой расчёта между ними обнуляет серию."""
    cur = {"BTC": sig, "ETH": none_of(sig, "ETH"), "SOL": none_of(sig, "SOL")}
    feed(b, monkeypatch, cur)
    baseline(b)
    cur["BTC"] = none_of(sig)
    run(b._auto_change_job(NS()))                               # серия 1
    cur["BTC"] = RuntimeError("bybit down")
    real = b._signal

    async def maybe_fail(base, fresh=False):
        if isinstance(cur[base], Exception):
            raise cur[base]
        return await real(base, fresh)
    monkeypatch.setattr(b, "_signal", maybe_fail)
    run(b._auto_change_job(NS()))                               # пропуск — серия сброшена
    cur["BTC"] = none_of(sig)
    run(b._auto_change_job(NS()))                               # снова серия 1 — ещё не смена
    assert sent.out == []
    run(b._auto_change_job(NS()))
    assert len(sent.out) == 1


def test_delivery_recorded_only_for_reached_admins(make_client, acfg, market, monkeypatch):
    c = with_cfg(acfg, tg_admin_ids={ADMIN, 51})
    monkeypatch.setattr("optbot.signals.time", NS(time=lambda: NOW / 1000, monotonic=__import__("time").monotonic))
    bot = OptionsBot(make_client(c), c)
    snd = Sent(fail={51})                                       # второй админ заблокировал бота
    bot._app = NS(bot=NS(send_message=snd))
    run(bot._auto_daily_job(NS()))
    assert q(c, "SELECT user_id FROM signal_views v JOIN signals s ON s.id = v.signal_id") == [{"user_id": ADMIN}]
    assert q(c, "SELECT shown FROM signals")[0]["shown"] == 1


def test_daily_and_change_do_not_race(b, sig, sent, monkeypatch):
    """Сводка и две проверки смены одновременно — по очереди: о смене одно сообщение, а не два."""
    cur = {"BTC": sig, "ETH": none_of(sig, "ETH"), "SOL": none_of(sig, "SOL")}
    feed(b, monkeypatch, cur)
    baseline(b)                                                 # точка отсчёта: кондор
    cur["BTC"] = none_of(sig)
    run(b._auto_change_job(NS()))                               # серия 1

    async def both():
        await asyncio.gather(b._auto_daily_job(NS()), b._auto_change_job(NS()), b._auto_change_job(NS()))
    run(both())
    texts = [t for _, t, _ in sent.out]
    assert sum("Сигналы на" in t for t in texts) == 1 and sum("сменилась" in t for t in texts) == 1


# ---------- расписание ----------
def _post_init(app, monkeypatch):
    monkeypatch.setattr(type(app.bot), "set_my_commands", lambda self, *a, **k: asyncio.sleep(0))
    monkeypatch.setattr("optbot.bot.ExecutionWatcher", lambda *a: NS(start=lambda: None, stop=lambda: None))
    asyncio.run(app.post_init(app))


def test_schedule_auto(make_client, acfg, monkeypatch):
    bot = OptionsBot(make_client(acfg), acfg)
    app = bot.build_app()
    daily, rep = [], []
    monkeypatch.setattr(type(app.job_queue), "run_daily", lambda self, cb, time, name: daily.append((cb, time, name)))
    monkeypatch.setattr(type(app.job_queue), "run_repeating",
                        lambda self, cb, interval, first, name: rep.append((cb, interval, first, name)))
    monkeypatch.setattr("optbot.bot.time.time", lambda: 899.5)
    _post_init(app, monkeypatch)
    (cb, t, name), = daily
    assert cb == bot._auto_daily_job and (t.hour, t.minute, t.utcoffset().total_seconds()) == (9, 5, 0)
    assert bot._auto_change_job not in [r[0] for r in rep]      # со сборщиком — вызов из его прохода


def test_schedule_change_without_collector(make_client, acfg, monkeypatch):
    c = with_cfg(acfg, collector_enabled=False)
    bot = OptionsBot(make_client(c), c)
    app = bot.build_app()
    rep = []
    monkeypatch.setattr(type(app.job_queue), "run_daily", lambda self, *a, **k: None)
    monkeypatch.setattr(type(app.job_queue), "run_repeating",
                        lambda self, cb, interval, first, name: rep.append((cb, interval, first, name)))
    monkeypatch.setattr("optbot.bot.time.time", lambda: 899.5)
    _post_init(app, monkeypatch)
    assert (bot._auto_change_job, 900, pytest.approx(5.5), "auto_signals_change") in rep


def test_collect_job_then_change_check(b, monkeypatch):
    calls = []

    async def change(context, bases=None):
        calls.append(bases)
    monkeypatch.setattr(b, "_auto_change_job", change)

    async def go():
        await b._collect_job(NS())
        if b._auto_task:
            await b._auto_task
    b.collector.collect = lambda: NS(errors=[], snapshots=3, skipped=0, failed_bases=set())
    run(go())
    assert calls == [["BTC", "ETH", "SOL"]]                     # после среза — отдельной задачей
    b._auto_task = None
    b.collector.collect = lambda: 1 / 0                         # срез не удался — история старая, не проверяем
    run(go())
    b.collector.collect = lambda: NS(errors=["BTC: timeout", "ETH: x", "SOL: y"], snapshots=0, skipped=0,
                                    failed_bases={"BTC", "ETH", "SOL"})
    run(go())                                                   # биржа лежит — ни одного среза
    assert len(calls) == 1
    b.collector.collect = lambda: NS(errors=["запись SOL: timeout"], snapshots=2, skipped=0,
                                    failed_bases={"SOL"})  # по множеству, а не по тексту ошибки
    run(go())
    assert calls[-1] == ["BTC", "ETH"]                          # по SOL среза нет — его смену не проверяем
    b.collector.collect = lambda: NS(errors=[], snapshots=0, skipped=3, failed_bases=set())  # слот уже записан (`--once`)
    run(go())
    assert calls[-1] == ["BTC", "ETH", "SOL"]
    n = len(calls)
    b.collector.collect = lambda: NS(errors=[], snapshots=3, skipped=0, failed_bases=set())
    b.cfg = with_cfg(b.cfg, auto_signals_change_hours=0)
    run(go())
    assert len(calls) == n


def test_change_check_does_not_block_collector(b, monkeypatch, caplog):
    """Медленная биржа: проверка смены идёт своей задачей; пока идёт — следующая не начинается."""
    gate = asyncio.Event()

    async def slow(context, bases=None):
        await gate.wait()
    monkeypatch.setattr(b, "_auto_change_job", slow)
    b.collector.collect = lambda: NS(errors=[], snapshots=3, skipped=0, failed_bases=set())

    async def go():
        await asyncio.wait_for(b._collect_job(NS()), 1)          # не ждёт проверку
        first = b._auto_task
        await b._collect_job(NS())
        assert b._auto_task is first and "ещё идёт" in caplog.text
        gate.set()
        await first
    run(go())


@pytest.mark.parametrize("kw", [{"tg_admin_ids": set()},
                                {"auto_signals_daily_utc": None, "auto_signals_change_hours": 0}])
def test_schedule_auto_off(make_client, acfg, monkeypatch, kw):
    c = with_cfg(acfg, **kw)
    bot = OptionsBot(make_client(c), c)
    app = bot.build_app()
    daily, rep = [], []
    monkeypatch.setattr(type(app.job_queue), "run_daily", lambda self, *a, **k: daily.append(a))
    monkeypatch.setattr(type(app.job_queue), "run_repeating", lambda self, cb, **k: rep.append(cb))
    _post_init(app, monkeypatch)
    assert daily == [] and bot._auto_change_job not in rep


def test_no_job_queue_warns_for_auto(make_client, acfg, monkeypatch, caplog):
    c = with_cfg(acfg, collector_enabled=False, journal_enabled=False)
    bot = OptionsBot(make_client(c), c)
    app = bot.build_app()
    monkeypatch.setattr(type(app), "job_queue", property(lambda self: None))
    _post_init(app, monkeypatch)
    assert "автосигналы выключены" in caplog.text


def test_change_message_banner_first(b, sig, sent, monkeypatch):
    cur = {"BTC": sig, "ETH": none_of(sig, "ETH"), "SOL": none_of(sig, "SOL")}
    feed(b, monkeypatch, cur)
    baseline(b)
    cur["BTC"] = none_of(sig)
    run(b._auto_change_job(NS()))
    run(b._auto_change_job(NS()))
    lines = sent.out[0][1].removeprefix("<pre>").split("\n")
    assert lines[0] == "[demo]" and lines[1].startswith("🔔 BTC") and lines[2] == "" and lines[3].startswith("📊 BTC")


def test_daily_summary_survives_format_error(b, sent, monkeypatch, caplog):
    real = botmod.fmt_signal_brief

    def brief(sig):
        if sig.metrics.base == "ETH":
            raise AttributeError("profile")
        return real(sig)
    monkeypatch.setattr(botmod, "fmt_signal_brief", brief)
    run(b._auto_daily_job(NS()))
    text = sent.out[0][1]
    assert "BTC: Железный кондор" in text and "ETH: не посчитан" in text and "SOL: сигнала нет" in text
    assert q(b.cfg, "SELECT base FROM signals") == [{"base": "BTC"}]   # записано посчитанное


def test_shutdown_cancels_change_task(make_client, acfg, monkeypatch):
    bot = OptionsBot(make_client(acfg), acfg)
    app = bot.build_app()

    async def go():
        bot._auto_task = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
        bot.watcher = None
        await app.post_shutdown(app)
        assert bot._auto_task.cancelled()
    run(go())


def _setup_change(b, sig, monkeypatch):
    cur = {"BTC": sig, "ETH": sig_for(sig, "ETH"), "SOL": none_of(sig, "SOL")}
    feed(b, monkeypatch, cur)
    baseline(b)
    return cur


def sig_for(sig, base):
    return dataclasses.replace(sig, metrics=dataclasses.replace(sig.metrics, base=base))


def test_change_error_on_one_base_does_not_stop_others(b, sig, sent, monkeypatch, caplog):
    cur = _setup_change(b, sig, monkeypatch)
    cur["BTC"], cur["ETH"] = none_of(sig), none_of(sig, "ETH")
    real = botmod.fmt_signal

    def bad(s, env, head=""):
        if s.metrics.base == "BTC":
            raise TypeError("None field")
        return real(s, env, head=head)
    monkeypatch.setattr(botmod, "fmt_signal", bad)
    run(b._auto_change_job(NS()))
    run(b._auto_change_job(NS()))
    assert [t.split("\n")[1] for _, t, _ in sent.out] == ["🔔 ETH: рекомендация сменилась — Железный кондор → сигнала нет"]
    assert "смена по BTC не проверена" in caplog.text


def test_retry_window_does_not_block_other_change(b, sig, sent, monkeypatch):
    t = [1_000_000.0]
    clock(monkeypatch, t)
    cur = _setup_change(b, sig, monkeypatch)
    cur["BTC"] = none_of(sig)
    sent.fail = {ADMIN}
    run(b._auto_change_job(NS()))
    run(b._auto_change_job(NS()))                               # не дошло — повтор через час
    assert b._auto["BTC"].retry_at > t[0]
    sent.fail = set()
    cur["BTC"] = sig                                            # вернулось к сообщённому — ожидание снято
    t[0] += 900
    run(b._auto_change_job(NS()))
    assert b._auto["BTC"].retry_at == 0
    cur["BTC"] = none_of(sig)
    t[0] += 900
    run(b._auto_change_job(NS()))
    run(b._auto_change_job(NS()))
    assert len(sent.out) == 1                                   # не ждёт час от прошлой неудачи


def test_daily_summary_leaves_change_state(b, sig, sent, monkeypatch):
    cur = _setup_change(b, sig, monkeypatch)
    cur["BTC"] = none_of(sig)
    sent.fail = {ADMIN}
    run(b._auto_change_job(NS()))
    run(b._auto_change_job(NS()))
    before = dataclasses.replace(b._auto["BTC"])
    sent.fail = set()
    run(b._auto_daily_job(NS()))
    assert b._auto["BTC"] == before                             # сводка — снимок, состояние смены не трогает


def test_late_check_does_not_continue_streak(b, sig, sent, monkeypatch):
    """Проверка опоздала на слот (следующий её пропустил) — её итоги серию не продолжают."""
    cur = _setup_change(b, sig, monkeypatch)
    cur["BTC"] = none_of(sig)
    real = b._auto_signals

    async def slow_then_skip(bases=None):
        res = await real(bases)
        b._auto_gen += 1                                        # пока считали — следующий слот пропустил проверку
        return res
    monkeypatch.setattr(b, "_auto_signals", slow_then_skip)
    run(b._auto_change_job(NS()))
    assert b._auto["BTC"].streak == 0


def test_summary_stops_when_a_part_reached_nobody(b, sent, monkeypatch):
    monkeypatch.setattr(botmod, "fmt_signal_brief", lambda s: f"{s.metrics.base}: " + "z" * 3000)
    sent.fail = {ADMIN}
    run(b._auto_daily_job(NS()))
    assert sent.out == [] and q(b.cfg, "SELECT COUNT(*) n FROM signals")[0]["n"] == 0


def test_long_change_message_falls_back_to_brief(b, sig, sent, monkeypatch):
    cur = _setup_change(b, sig, monkeypatch)
    long = none_of(sig)
    long.reasons = ["очень длинная причина " * 40] * 10
    cur["BTC"] = long
    run(b._auto_change_job(NS()))
    run(b._auto_change_job(NS()))
    text = sent.out[0][1]
    assert len(text) <= botmod.TG_MAX_TEXT and "🔔 BTC" in text and "BTC: сигнала нет" in text


def test_change_skipped_base_breaks_streak(b, sig, sent, monkeypatch):
    cur = _setup_change(b, sig, monkeypatch)
    cur["BTC"] = none_of(sig)
    run(b._auto_change_job(NS()))                               # серия 1
    run(b._auto_change_job(NS(), ["ETH", "SOL"]))               # по BTC среза нет — пропуск
    run(b._auto_change_job(NS()))                               # снова 1
    assert sent.out == []
    run(b._auto_change_job(NS()))
    assert len(sent.out) == 1


def test_pack_splits_long_summary():
    parts = [f"A{i}: " + "x" * 1500 for i in range(5)] + ["хвост"]
    msgs = OptionsBot._pack("[demo]\n🤖 Сигналы", parts)
    assert len(msgs) > 1 and all(len(m) <= botmod.TG_MAX_TEXT for m in msgs)
    assert msgs[0].startswith("<pre>[demo]") and "хвост" in msgs[-1]
    joined = "".join(msgs)
    assert all(f"A{i}:" in joined for i in range(5))
    huge = OptionsBot._pack("h", ["y" * 10000])
    assert len(huge) == 1 and huge[0].startswith("<pre>h\n\ny")            # заголовок не уходит один
    assert len(huge[0]) <= botmod.TG_MAX_TEXT and "обрезано" in huge[0]      # обрезано — с пометкой


def test_daily_summary_in_parts_journaled_only_if_all_arrived(make_client, acfg, market, monkeypatch, sent):
    monkeypatch.setattr("optbot.signals.time", NS(time=lambda: NOW / 1000, monotonic=__import__("time").monotonic))
    bot = OptionsBot(make_client(acfg), acfg)
    bot._app = NS(bot=NS(send_message=sent))
    monkeypatch.setattr(botmod, "fmt_signal_brief", lambda s: f"{s.metrics.base}: " + "z" * 3000)
    run(bot._auto_daily_job(NS()))
    assert len(sent.out) >= 3 and q(acfg, "SELECT COUNT(*) n FROM signal_views")[0]["n"] == 1


def test_collector_failure_breaks_streak(b, sig, sent, monkeypatch):
    """Срез не удался (или проверка пропущена) — серия «подряд» начинается заново."""
    cur = _setup_change(b, sig, monkeypatch)
    cur["BTC"] = none_of(sig)
    run(b._auto_change_job(NS()))                               # серия 1
    b.collector.collect = lambda: 1 / 0
    run(b._collect_job(NS()))
    assert b._auto["BTC"].streak == 0
    run(b._auto_change_job(NS()))
    assert sent.out == []                                       # снова 1 — ещё не смена
    b.collector.collect = lambda: NS(errors=["x"], snapshots=0, skipped=0, failed_bases={"BTC", "ETH", "SOL"})
    run(b._collect_job(NS()))
    assert b._auto["BTC"].streak == 0


def test_skipped_kick_breaks_streak(b, sig, monkeypatch, caplog):
    cur = _setup_change(b, sig, monkeypatch)
    cur["BTC"] = none_of(sig)
    run(b._auto_change_job(NS()))

    async def go():
        b._auto_task = asyncio.get_running_loop().create_task(asyncio.sleep(10))
        b._kick_auto_change(["BTC", "ETH", "SOL"])
        b._auto_task.cancel()
    run(go())
    assert "ещё идёт" in caplog.text and b._auto["BTC"].streak == 0
