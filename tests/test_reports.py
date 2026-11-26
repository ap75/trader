"""Отчёты бумажных книг (optbot/reports.py, этап E3): /books, /stress, еженедельный отчёт."""
import asyncio
import dataclasses
import json
import math
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from optbot import config, db, reports
from optbot.config import PaperSettings
from optbot.fmt import DAY_MS
from optbot.reports import b76

NOW = 1_795_000_000_000 // DAY_MS * DAY_MS + 10 * 3600_000     # 10:00 UTC
EXP = NOW + 5 * DAY_MS


@pytest.fixture
def rcfg(cfg):
    return dataclasses.replace(cfg, paper=PaperSettings(enabled=True, bases=("BTC", "ETH")),
                               base_coins=["BTC", "ETH", "SOL"])


def conn_of(cfg):
    return db.connect(cfg.db_file)


def add_trade(conn, book, legs, qty=1.0, status="open", forward=60000.0, **kw):
    h, strat, base = book.split(":")[:3]
    row = dict(book=book, horizon=h, strategy=strat, base=base, opened_ms=NOW - DAY_MS, expiry="X", expiry_ms=EXP,
               legs=json.dumps(legs), qty=qty, max_loss=100.0, entry_fees=1.0, forward=forward, status=status)
    row.update(kw)
    with conn:
        cur = conn.execute(f"INSERT INTO paper_trades ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                           list(row.values()))
    return cur.lastrowid


def leg(sym, kind, k, side, exp=EXP):
    return dict(symbol=sym, kind=kind, strike=k, side=side, expiry_ms=exp, fill=100.0)


def mark(conn, sym, iv, s, ts=NOW - 60_000):
    with conn:
        conn.execute("INSERT INTO leg_marks (ts, symbol, mark, bid, ask, iv, underlying) VALUES (?,?,?,?,?,?,?)",
                     (ts, sym, 100.0, 99.0, 101.0, iv, s))


def candles(conn, base, closes):
    """Часовые свечи: по одной на день (последняя свеча дня — 23:00)."""
    start = NOW // DAY_MS * DAY_MS - len(closes) * DAY_MS
    with conn:
        conn.executemany("INSERT INTO perp_h1 (base, ts, close) VALUES (?,?,?)",
                         [(base, start + i * DAY_MS + 23 * 3600_000, c) for i, c in enumerate(closes)])


# ---------- Black-76 ----------
def test_b76_parity_and_greeks_by_finite_differences():
    f, k, t, s = 60000.0, 58000.0, 7 / 365, 0.55
    c, p = b76("C", f, k, t, s), b76("P", f, k, t, s)
    assert c[0] - p[0] == pytest.approx(f - k)                                   # паритет при r = 0
    h = 1.0
    assert p[1] == pytest.approx((b76("P", f + h, k, t, s)[0] - b76("P", f - h, k, t, s)[0]) / (2 * h), rel=1e-5)
    assert p[2] == pytest.approx((b76("P", f + h, k, t, s)[1] - b76("P", f - h, k, t, s)[1]) / (2 * h), rel=1e-4)
    assert p[3] == pytest.approx(b76("P", f, k, t, s + 0.005)[0] - b76("P", f, k, t, s - 0.005)[0], rel=1e-3)
    dt = 1 / 365
    assert p[4] == pytest.approx(b76("P", f, k, t - dt / 2, s)[0] - b76("P", f, k, t + dt / 2, s)[0], rel=1e-2)
    assert b76("P", f, 70000, 0, s)[:2] == (10000, -1.0)                       # на экспирации — внутренняя


# ---------- бета ----------
def test_beta_from_daily_returns(rcfg):
    conn = conn_of(rcfg)
    btc, eth, b, e = [], [], 50000.0, 3000.0
    for i in range(100):
        r = 0.02 * math.sin(i * 1.7)
        b *= math.exp(r)
        e *= math.exp(1.5 * r)
        btc.append(b)
        eth.append(e)
    candles(conn, "BTC", btc)
    candles(conn, "ETH", eth)
    v, n = reports.beta(conn, "ETH", NOW)
    assert v == pytest.approx(1.5) and n == reports.BETA_DAYS
    assert reports.beta(conn, "SOL", NOW) == (1.0, 0)                            # нет свечей — 1
    assert reports.beta(conn, "BTC", NOW)[0] == 1.0
    conn.close()


# ---------- стресс ----------
def test_stress_reprices_legs_and_scales_by_beta(rcfg):
    conn = conn_of(rcfg)
    btc, eth, b, e = [], [], 50000.0, 3000.0
    for i in range(100):
        r = 0.02 * math.sin(i * 1.7)
        b, e = b * math.exp(r), e * math.exp(2 * r)
        btc.append(b)
        eth.append(e)
    candles(conn, "BTC", btc)
    candles(conn, "ETH", eth)
    legs = [leg("BTC-P60", "P", 60000, -1), leg("BTC-P55", "P", 55000, +1)]
    add_trade(conn, "short:bull_put:BTC", legs, qty=0.5)
    mark(conn, "BTC-P60", 0.5, 61000)
    mark(conn, "BTC-P55", 0.6, 61000)
    add_trade(conn, "long:bull_put:ETH", [leg("ETH-C3", "C", 3000, +1)], qty=2, forward=3000)
    mark(conn, "ETH-C3", 0.7, 3100)
    add_trade(conn, "short:bear_call:BTC", [leg("BTC-C70", "C", 70000, -1)])        # нет котировки
    add_trade(conn, "short:bull_put:BTC:har", [leg("OLD", "P", 1, -1, exp=NOW - 1)])  # истекла — ждёт поставки
    with conn:
        conn.execute("INSERT INTO perp_books (id, base, strategy, capital, cash, qty, entry, last_bar_ms, "
                     "last_funding_ms, created_ms, params) VALUES ('perp:ema_4h_ls:ETH','ETH','x',10000,10000,-1.5,"
                     "3000,0,0,0,'{}')")
        conn.execute("INSERT INTO perp_equity (ts, book, equity, cash, qty, price) VALUES (?,?,?,?,?,?)",
                     (NOW, "perp:ema_4h_ls:ETH", 10000, 10000, -1.5, 3100))
    st = reports.stress(rcfg, NOW, conn)
    conn.close()
    t = (EXP - NOW) / reports.YEAR_MS

    def spread(f, iv_add=0.0):
        return 0.5 * (-b76("P", f, 60000, t, 0.5 + iv_add)[0] + b76("P", f, 55000, t, 0.6 + iv_add)[0])
    base0 = spread(61000)
    got = st.books["short:bull_put:BTC"]
    assert got[1] == pytest.approx(spread(61000 * 0.9) - base0)                   # BTC −10%
    assert got[3] == pytest.approx(spread(61000 * 1.2) - base0)
    assert got[5] == pytest.approx(spread(61000, 0.5) - base0)                    # IV +50 пунктов
    assert got[0] < got[1] < 0 < got[2]                                            # бычий пут-спред
    bt = st.betas["ETH"][0]
    assert bt == pytest.approx(2.0)
    e0 = b76("C", 3100, 3000, t, 0.7)[0]
    assert st.books["long:bull_put:ETH"][2] == pytest.approx(2 * (b76("C", 3100 * (1 + bt * 0.1), 3000, t, 0.7)[0] - e0))
    assert st.perp["perp:ema_4h_ls:ETH"][0] == pytest.approx(-1.5 * 3100 * bt * -0.2)   # шорт выигрывает на падении
    assert st.perp["perp:ema_4h_ls:ETH"][4:6] == [0.0, 0.0]                         # к IV не чувствительны
    assert st.perp["perp:ema_4h_ls:ETH"][7] == pytest.approx(st.perp["perp:ema_4h_ls:ETH"][0])   # −20/+50 = −20%
    # совмещённые: −20% & IV+50 — у путов IV +75 (×1.5), колл ETH — +50
    assert got[7] == pytest.approx(0.5 * (-b76("P", 61000 * 0.8, 60000, t, 0.5 + 0.75)[0]
                                          + b76("P", 61000 * 0.8, 55000, t, 0.6 + 0.75)[0]) - base0)
    assert got[8] == pytest.approx(spread(61000 * 1.1, 0.1) - base0)              # рост: путы и коллы одинаково
    assert st.books["long:bull_put:ETH"][6] == pytest.approx(
        2 * (b76("C", 3100 * (1 - bt * 0.1), 3000, t, 0.7 + 0.2)[0] - e0))
    assert st.missing == ["#3 short:bear_call:BTC"] and st.trades == 2
    g = st.greeks[("short:bull_put:BTC", "BTC")]
    d = 0.5 * (-b76("P", 61000, 60000, t, 0.5)[1] + b76("P", 61000, 55000, t, 0.6)[1])
    assert g.delta == pytest.approx(d) and g.delta_usdt == pytest.approx(d * 61000) and g.theta > 0 and g.vega < 0
    text = reports.fmt_stress(st, rcfg)
    assert "ETH β 2.00" in text and "Σ RV" in text and "Σ HAR" in text and "% от 10,000" in text
    assert "Σ перпетуалы" in text and "#3 short:bear_call:BTC" in text and "Греки открытых" in text
    # длинный горизонт — в обеих системах: Σ HAR = только длинная ETH (короткая :har истекла)
    har_line = next(x for x in text.splitlines() if x.startswith("Σ HAR"))
    assert float(har_line.split()[4]) == pytest.approx(round(st.books["long:bull_put:ETH"][2]), abs=1)


def test_stress_empty(rcfg):
    st = reports.stress(rcfg, NOW)
    assert "открытых позиций нет" in reports.fmt_stress(st, rcfg)


# ---------- /books ----------
def test_curve_metrics():
    pts = [(NOW - 10 * DAY_MS, 0.0)] + [(NOW - (9 - i) * DAY_MS, v) for i, v in
                                        enumerate([10, 30, 5, 20, 40, 35, 50, 45, 60, 70])]
    m = reports.curve_metrics(pts, NOW - 9 * DAY_MS, 10000)
    assert m["dd"] == -25 and m["dd_pct"] == pytest.approx(-0.0025) and m["days"] == 10
    days = [10, 20, -25, 15, 20, -5, 15, -5, 15, 10]
    mean, sd = sum(days) / 10, math.sqrt(sum((x - 7) ** 2 for x in days) / 10)
    assert m["sharpe"] == pytest.approx(mean / sd * math.sqrt(365))
    assert m["sortino"] == pytest.approx(mean / math.sqrt((625 + 25 + 25) / 10) * math.sqrt(365))
    flat = reports.curve_metrics([(NOW - i * DAY_MS, 1.0) for i in range(10, 0, -1)], 0, 10000)
    assert flat["sharpe"] is None and flat["sortino"] is None and flat["dd"] == 0


def test_books_report_metrics_filters_and_csv(rcfg):
    conn = conn_of(rcfg)
    start = NOW - 30 * DAY_MS
    for book, pnl, im in (("short:bull_put:BTC", 40.0, 400.0), ("short:bull_put:BTC", -100.0, 400.0),
                          ("long:iron_condor:ETH", 25.0, 500.0), ("short:bull_put:BTC:har", 30.0, 400.0)):
        add_trade(conn, book, [], status="closed", closed_ms=NOW - 5 * DAY_MS, pnl=pnl, r_mult=pnl / 100,
                  im_regular=im)
    add_trade(conn, "short:bull_put:BTC", [], status="closed", closed_ms=start - DAY_MS, pnl=999, r_mult=9.99)
    add_trade(conn, "short:bull_put:BTC", [], mark_pnl=-7.0)
    eq = {"short:bull_put:BTC": [0, 40, -60, -55, -60, -60, -67], "long:iron_condor:ETH": [0, 0, 0, 25, 25, 25, 25]}
    with conn:
        for book, vals in eq.items():
            for i, v in enumerate(vals):
                conn.execute("INSERT INTO book_equity (ts, book, realized, unrealized, open_n, open_risk) "
                             "VALUES (?,?,?,0,0,0)", (NOW - (6 - i) * DAY_MS, book, v))
    rep = reports.books_report(start, NOW + 1, "30 дн.", rcfg, conn=conn)
    rows = {r.book: r for r in rep.rows}
    bp = rows["short:bull_put:BTC"]
    assert (bp.n, bp.wins, bp.pnl, bp.open_n) == (2, 1, -60, 1)                    # старая сделка — вне периода
    assert bp.im_ret == pytest.approx(-60 / 800) and bp.dd == -107 and bp.worst == -100
    sysr = {r.book: r for r in rep.systems}
    assert sysr["RV"].n == 3 and sysr["RV"].pnl == pytest.approx(-35)              # длинная — и в RV …
    assert sysr["HAR"].n == 2 and sysr["HAR"].pnl == pytest.approx(55)            # … и в HAR
    assert sysr["RV"].dd == pytest.approx(-100)                                    # сумма кривых: 40 → −60
    assert rep.open_lines and "по mark -7.00" in rep.open_lines[0]
    text = reports.fmt_books(rep, rcfg)
    assert "short:bull_put:BTC: 2 сд. (приб. 50%), открыто 1, PnL -60.00 (-0.60%)" in text
    assert "на IM -7.5% (PM н/д)" in text and "Шарп" in text and "По системам" in text
    only = reports.books_report(start, NOW + 1, "30 дн.", rcfg, horizon="long", base="ETH", conn=conn)
    assert [r.book for r in only.rows] == ["long:iron_condor:ETH"] and only.versions == []
    csv_text = reports.books_csv(rep).decode("utf-8-sig").splitlines()
    assert csv_text[0].split(",") == reports.CSV_HEAD and any(x.startswith("short:bull_put:BTC,2,1,") for x in csv_text)
    conn.close()


def test_books_report_perp_and_correlation(rcfg):
    conn = conn_of(rcfg)
    vals = [0, 10, 5, 20, 15, 30, 25, 40]
    with conn:
        conn.execute("INSERT INTO perp_books (id, base, strategy, capital, cash, qty, entry, last_bar_ms, "
                     "last_funding_ms, created_ms, params) VALUES ('perp:ema_4h_ls:BTC','BTC','x',10000,10000,0.1,"
                     "60000,0,0,0,'{}')")
        for i, v in enumerate(vals):
            ts = NOW - (7 - i) * DAY_MS
            conn.execute("INSERT INTO perp_equity (ts, book, equity, cash, qty, price) VALUES (?,?,?,?,?,?)",
                         (ts, "perp:ema_4h_ls:BTC", 10000 + v, 10000, 0.1, 60000))
            conn.execute("INSERT INTO book_equity (ts, book, realized, unrealized, open_n, open_risk) "
                         "VALUES (?,?,?,0,0,0)", (ts, "short:bull_put:BTC", -v))
        conn.execute("INSERT INTO perp_trades (book, base, side, status, opened_ms, closed_ms, entry_px, exit_px, qty,"
                     " risk, fees, funding, pnl, r_mult) VALUES ('perp:ema_4h_ls:BTC','BTC',1,'closed',?,?,1,1,1,"
                     "100,1,0,50,0.5)", (NOW - 3 * DAY_MS, NOW - 2 * DAY_MS))
    rep = reports.books_report(NOW - 30 * DAY_MS, NOW + 1, "30 дн.", rcfg, conn=conn)
    p, = rep.perp
    assert (p.n, p.pnl, p.open_n, p.im_ret) == (1, 50, 1, None) and p.dd == -5
    rho, n = rep.perp_corr["perp:ema_4h_ls:BTC"]["RV"]
    assert rho == pytest.approx(-1) and n == 7
    text = reports.fmt_books(rep, rcfg)
    assert "perp:ema_4h_ls:BTC: 1 сд." in text and "на IM" not in text.split("Книги перпетуалов")[1]
    assert "perp:ema_4h_ls:BTC: -1.00" in text
    assert reports.books_report(NOW - 30 * DAY_MS, NOW + 1, "x", rcfg, horizon="short", conn=conn).perp == []
    conn.close()


def test_split_text():
    parts = reports.split_text("\n".join(f"строка {i}" for i in range(1000)), 500)
    assert all(len(p) <= 500 for p in parts) and "\n".join(parts).count("строка") == 1000
    assert reports.split_text("x" * 50, 10) == ["x" * 9 + "…"]


def test_weekly_setting(monkeypatch):
    monkeypatch.setenv("X_WEEKLY", "fri 17:30")
    assert config._weekly("X_WEEKLY", "mon 09:15") == (4, 17, 30)
    monkeypatch.setenv("X_WEEKLY", "off")
    assert config._weekly("X_WEEKLY", "mon 09:15") is None
    monkeypatch.setenv("X_WEEKLY", "пятница 25:00")
    assert config._weekly("X_WEEKLY", "mon 09:15") == (0, 9, 15)


def test_cli(rcfg, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(reports, "settings", rcfg)
    assert reports.main(["--stress"]) == 0 and "Стресс" in capsys.readouterr().out
    f = tmp_path / "b.csv"
    assert reports.main(["--books", "7d", "--horizon", "short", "--base", "btc", "--csv", str(f)]) == 0
    assert "Бумажные книги · short · BTC, 7 дн." in capsys.readouterr().out and f.read_bytes().startswith(b"\xef\xbb\xbf")


# ---------- бот ----------
def test_bot_books_stress_and_weekly(rcfg, make_client, monkeypatch):
    from optbot.bot import OptionsBot
    from tests.test_bot import TG, VIEWER, GROUP
    bot = OptionsBot(make_client(rcfg), rcfg)
    tg = TG()
    conn = conn_of(rcfg)
    add_trade(conn, "short:bull_put:BTC", [], status="closed", closed_ms=NOW - DAY_MS, pnl=12.5, r_mult=0.125)
    conn.close()
    monkeypatch.setattr("optbot.journal.time.time", lambda: NOW / 1000)
    u = tg.upd(VIEWER, "g")
    sent = []
    u.effective_chat.send_document = AsyncMock(side_effect=lambda **k: sent.append(k["filename"]))
    asyncio.run(bot.cmd_books(u, tg.ctx("short", "BTC", "7d", "csv")))
    assert "Бумажные книги · short · BTC, 7 дн." in tg.last() and "PnL +12.50" in tg.last()
    assert sent == ["books-demo-short-btc-7дн.csv"] or sent[0].startswith("books-")
    asyncio.run(bot.cmd_books(tg.upd(VIEWER, "dm"), tg.ctx("BTC", "ETH")))
    assert "Формат: /books" in tg.last()
    asyncio.run(bot.cmd_stress(tg.upd(VIEWER, "dm"), tg.ctx()))
    assert "Стресс открытых" in tg.last()
    parts = bot.books_weekly_text(NOW)
    assert parts[0].startswith("Худший совмещённый сценарий") and "Еженедельный отчёт" in parts[0] and "Стресс открытых" in "\n".join(parts)
    line = next(x for x in parts[0].splitlines() if x.startswith("Без ответа бэктеста"))
    assert "Бычий пут-спред (кредит): за период 1 сд., PnL +12.50" in line and "всего 1 сд." in line
    assert "книг в плюсе 1 из 1" in line and "1 из 50" in line and "стрэддл" not in line
    got = []
    monkeypatch.setattr(bot, "_broadcast", AsyncMock(side_effect=lambda msg, what: got.append(msg) or 2))
    monkeypatch.setattr(bot, "books_weekly_text", lambda: ["a", "b"])
    asyncio.run(bot._books_weekly_job(NS()))
    assert got == ["<pre>a</pre>", "<pre>b</pre>"]
    assert GROUP in bot._targets()


def test_first_partial_day_counted_from_point_before_period():
    start = NOW - 7 * DAY_MS                                   # «7d»: посреди дня
    pts = [(start - 3600_000, 100.0)] + [(start + i * DAY_MS + 3600_000, 100.0 + 10 * (i + 1)) for i in range(7)]
    m = reports.curve_metrics(pts, start, 10000)
    assert m["days"] == 7


def test_stress_beta_for_any_open_base_and_missing_underlying(rcfg):
    conn = conn_of(rcfg)
    sol, btc, b, s_ = [], [], 50000.0, 100.0
    for i in range(100):
        r = 0.02 * math.sin(i * 1.3)
        b, s_ = b * math.exp(r), s_ * math.exp(1.3 * r)
        btc.append(b)
        sol.append(s_)
    candles(conn, "BTC", btc)
    candles(conn, "SOL", sol)
    add_trade(conn, "short:bull_put:SOL", [leg("S1", "P", 100, -1)], forward=None)   # SOL не в PAPER_BASES
    mark(conn, "S1", 0.8, None)                                                         # и underlying нет
    with conn:
        conn.execute("INSERT INTO perp_books (id, base, strategy, capital, cash, qty, entry, last_bar_ms, "
                     "last_funding_ms, created_ms, params) VALUES ('perp:x:SOL','SOL','x',10000,10000,10,100,0,0,0,'{}')")
        conn.execute("INSERT INTO perp_equity (ts, book, equity, cash, qty, price) VALUES (?,?,?,?,?,?)",
                     (NOW, "perp:x:SOL", 10000, 10000, 10, 100))
    st = reports.stress(rcfg, NOW, conn)
    conn.close()
    assert st.betas["SOL"][0] == pytest.approx(1.3, rel=0.02)
    assert st.perp["perp:x:SOL"][0] == pytest.approx(10 * 100 * st.betas["SOL"][0] * -0.2)
    assert st.missing == ["#1 short:bull_put:SOL"]


def test_systems_keep_disabled_versions_with_data(rcfg):
    off = dataclasses.replace(rcfg, sig=dataclasses.replace(rcfg.sig, max_cost_pct=0))
    assert all(not c for _, _, c in reports.systems(off))
    assert any(c for _, _, c in reports.systems(off, ["short:bull_put:BTC:cost"]))


def test_im_return_with_zero_im_straddle():
    t = [dict(pnl=50.0, r_mult=0.5, im_regular=900.0), dict(pnl=-20.0, r_mult=-0.2, im_regular=0.0)]
    assert reports._row("RV", t, [], 0, 10000, 0).im_ret == pytest.approx(30 / 900)
    assert reports._row("s", t[1:], [], 0, 10000, 0).im_ret is None
    assert reports._row("s", [dict(pnl=1.0, r_mult=0.1, im_regular=None)], [], 0, 10000, 0).im_ret is None


def test_daily_close_needs_last_hour(rcfg):
    conn = conn_of(rcfg)
    d = NOW // DAY_MS * DAY_MS - DAY_MS
    with conn:
        conn.executemany("INSERT INTO perp_h1 (base, ts, close) VALUES ('BTC', ?, ?)",
                         [(d - DAY_MS + 23 * 3600_000, 1.0), (d + 14 * 3600_000, 2.0)])
    assert reports.daily_closes(conn, "BTC", 0, NOW) == {d - DAY_MS: 1.0}
    conn.close()


def test_combined_scenarios_worst_line_and_put_mult(rcfg):
    conn = conn_of(rcfg)
    add_trade(conn, "short:bull_put:BTC", [leg("A", "P", 60000, -1)], qty=0.1)
    mark(conn, "A", 0.5, 61000)
    st = reports.stress(rcfg, NOW, conn)
    hi = reports.stress(dataclasses.replace(rcfg, paper=dataclasses.replace(rcfg.paper, stress_put_iv_mult=3)), NOW, conn)
    conn.close()
    v, v3 = st.books["short:bull_put:BTC"], hi.books["short:bull_put:BTC"]
    assert v3[7] < v[7] < v[0] and v3[0] == v[0] and v3[5] == v[5]   # множитель — только в сценариях падения
    worst = reports.worst_combined(st, rcfg)
    assert worst.startswith("Худший совмещённый сценарий: RV ") and "BTC -20% & IV +50" in worst
    text = reports.fmt_stress(st, rcfg)
    assert "Совмещённые" in text and "×1.5" in text and "−20/+50" in text
    assert reports.worst_combined(reports.Stress(NOW, {}), rcfg).endswith("открытых позиций нет")


def test_worst_line_mentions_perp_and_unpriced():
    st = reports.Stress(NOW, {}, perp={"perp:x:BTC": [-100.0 * k for k in range(10)]}, perp_capital=10000,
                        missing=["#1 x"])
    line = reports.worst_combined(st)
    assert "перпетуалы -900 (-9.0%, BTC +20% & IV +10)" in line and "без оценки 1 сд." in line
    assert reports.leg_iv_shift("P", -0.03, 0.2, 1.5) == pytest.approx(0.3)
    assert reports.leg_iv_shift("P", 0.03, 0.2, 1.5) == 0.2 and reports.leg_iv_shift("C", -0.1, 0.2, 1.5) == 0.2


def test_watch_line_counts_period_total_and_books(rcfg):
    conn = conn_of(rcfg)
    add_trade(conn, "short:bull_put:BTC", [], status="closed", closed_ms=NOW - DAY_MS, pnl=10.0, r_mult=0.1)
    add_trade(conn, "short:bull_put:BTC", [], status="closed", closed_ms=NOW - 20 * DAY_MS, pnl=-30.0, r_mult=-0.3)
    add_trade(conn, "long:bull_put:ETH:cost", [], status="closed", closed_ms=NOW - 2 * DAY_MS, pnl=5.0, r_mult=0.05)
    add_trade(conn, "short:iron_condor:BTC", [], status="closed", closed_ms=NOW - DAY_MS, pnl=99.0, r_mult=1.0)
    add_trade(conn, "short:bull_put:SOL", [], status="open")
    line = reports.watch_line(NOW - 7 * DAY_MS, NOW, rcfg, conn)
    conn.close()
    assert "за период 2 сд., PnL +15.00; всего 3 сд., PnL -15.00, ΣR -0.15" in line
    assert "книг в плюсе 1 из 2" in line and "больше всего сделок в книге 2 из 50" in line and "+99" not in line
