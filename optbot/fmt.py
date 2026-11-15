"""Текстовое форматирование ответов API — общее для CLI и Telegram-бота.

Все функции возвращают простой текст; таблицы рассчитаны на моноширинный шрифт
(в Telegram оборачиваются в <pre>).
"""
from __future__ import annotations

import time

from datetime import datetime, timezone
from decimal import Decimal

from .bybit_client import _d


def px(x, dash: bool = True) -> str:
    """Компактная цена: 1090 / 12.5 / 0.045. Ноль -> '-' (нет котировки)."""
    d = _d(x)
    if d == 0:
        return "-" if dash else "0"
    a = abs(d)
    if a >= 100:
        return f"{d:.0f}"
    if a >= 1:
        return f"{d:.2f}"
    s = f"{d:.4f}".rstrip("0")  # 0.8800 -> 0.88, 0.0450 -> 0.045
    return s + "0" * (2 - len(s.split(".")[1]))


def num(x) -> str:
    """Число без лишних нулей: страйки, количества, балансы (72, 1.5, 50000)."""
    d = _d(x)
    return f"{d.normalize():f}"


def to_dec(raw: str) -> Decimal:
    """Строка -> Decimal с понятной ошибкой."""
    try:
        d = Decimal(raw.strip().replace(",", "."))
    except Exception:
        raise ValueError(f"Не число: {raw}")
    if not d.is_finite():
        raise ValueError(f"Не число: {raw}")
    return d


def pct(x) -> str:
    """IV 0.5717 -> '57.2'."""
    d = _d(x)
    return "-" if d == 0 else f"{d * 100:.1f}"


def money(x) -> str:
    return f"{_d(x):,.2f}"


def _table(header: list[str], rows: list[list[str]], align: str) -> str:
    """align: строка из 'l'/'r' на каждую колонку."""
    widths = [max(len(r[i]) for r in [header] + rows) for i in range(len(header))]

    def line(r):
        return " ".join(c.ljust(w) if a == "l" else c.rjust(w) for c, w, a in zip(r, widths, align)).rstrip()

    return "\n".join([line(header)] + [line(r) for r in rows])


def fmt_expiries(base: str, exps: list[tuple[str, datetime]]) -> str:
    now = datetime.now(timezone.utc)
    lines = [f"{base.upper()}: экспирации"]
    for code, dt in exps:
        days = (dt - now).total_seconds() / 86400
        lines.append(f"  {code:<8} {dt:%Y-%m-%d %H:%M} UTC  ({days:.1f} дн.)")
    return "\n".join(lines)


def fmt_chain(ch: dict, wide: bool = False) -> str:
    """Цепочка: колл слева, пут справа, '*' — ATM-страйк.

    Компактный режим (телефон): bid/ask колла, страйк, bid/ask пута, markIV.
    Широкий (терминал): добавляет дельты и IV по обеим сторонам, OI.
    """
    head = f"{ch['base']} {ch['expiry']}  F={px(ch['underlying'])}  ATM={num(ch['atm_strike'])}"
    rows = []
    for k, c, p in ch["rows"]:
        c, p = c or {}, p or {}
        mark = "*" if k == ch["atm_strike"] else ""
        strike = f"{mark}{num(k)}"
        if wide:
            rows.append([
                num(c.get("openInterest")), f"{_d(c.get('delta')):.2f}" if c else "-", pct(c.get("markIv")),
                px(c.get("bid1Price")), px(c.get("ask1Price")),
                strike,
                px(p.get("bid1Price")), px(p.get("ask1Price")),
                pct(p.get("markIv")), f"{_d(p.get('delta')):.2f}" if p else "-", num(p.get("openInterest")),
            ])
        else:
            iv = c.get("markIv") or p.get("markIv")
            rows.append([px(c.get("bid1Price")), px(c.get("ask1Price")), strike,
                         px(p.get("bid1Price")), px(p.get("ask1Price")), pct(iv)])
    if wide:
        header = ["C.OI", "C.Δ", "C.IV", "C.bid", "C.ask", "Strike", "P.bid", "P.ask", "P.IV", "P.Δ", "P.OI"]
        align = "rrrrrrrrrrr"
    else:
        header = ["C.bid", "C.ask", "Strike", "P.bid", "P.ask", "IV"]
        align = "rrrrrr"
    return head + "\n" + _table(header, rows, align)


def fmt_ticker(t: dict) -> str:
    return "\n".join([
        t.get("symbol", "?"),
        f"bid {px(t.get('bid1Price'))} x {t.get('bid1Size', '-')}  (IV {pct(t.get('bid1Iv'))})",
        f"ask {px(t.get('ask1Price'))} x {t.get('ask1Size', '-')}  (IV {pct(t.get('ask1Iv'))})",
        f"mark {px(t.get('markPrice'))}  markIV {pct(t.get('markIv'))}  last {px(t.get('lastPrice'))}",
        f"underlying {px(t.get('underlyingPrice'))}  index {px(t.get('indexPrice'))}",
        f"Δ {_d(t.get('delta')):.4f}  Γ {_d(t.get('gamma')):.6f}  "
        f"V {_d(t.get('vega')):.2f}  Θ {_d(t.get('theta')):.2f}",
        f"OI {t.get('openInterest', '-')}  vol24h {t.get('volume24h', '-')}",
    ])


def fmt_balance(b: dict) -> str:
    if not b:
        return "Баланс: пусто"
    lines = [
        f"Аккаунт {b.get('accountType', '')}",
        f"  Equity      {money(b.get('totalEquity'))} USD",
        f"  Available   {money(b.get('totalAvailableBalance'))} USD",
        f"  IM / MM     {money(b.get('totalInitialMargin'))} / {money(b.get('totalMaintenanceMargin'))}",
    ]
    coins = [c for c in b.get("coin", []) if _d(c.get("equity")) != 0]
    if coins:
        rows = [[c["coin"], num(c.get("equity")), money(c.get("unrealisedPnl"))] for c in coins]
        lines.append(_table(["Coin", "Equity", "uPnL"], rows, "lrr"))
    return "\n".join(lines)


HOUR_MS = 3_600_000
DAY_MS = 86_400_000


def utc(ms: int, seconds: bool = False) -> str:
    """Миллисекунды -> 'ГГГГ-ММ-ДД ЧЧ:ММ[:СС]' в UTC."""
    return time.strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M", time.gmtime(ms / 1000))


def short_symbol(sym: str) -> str:
    """BTC-17NOV26-65500-C-USDT -> BTC-17NOV26-65500-C."""
    parts = sym.split("-")
    return "-".join(parts[:4]) if len(parts) == 5 else sym


def fmt_positions(ps: list[dict]) -> str:
    if not ps:
        return "Открытых позиций нет"
    out = []
    for p in ps:
        sign = "+" if p.get("side") == "Buy" else "-"
        out.append(
            f"{short_symbol(p['symbol'])}  {sign}{p.get('size')}\n"
            f"  avg {px(p.get('avgPrice'))}  mark {px(p.get('markPrice'))}  "
            f"uPnL {px(p.get('unrealisedPnl'), False)}\n"
            f"  Δ {_d(p.get('delta')):.4f}  Γ {_d(p.get('gamma')):.6f}  "
            f"V {_d(p.get('vega')):.2f}  Θ {_d(p.get('theta')):.2f}"
        )
    return "\n".join(out)


def fmt_orders(os_: list[dict]) -> str:
    if not os_:
        return "Открытых ордеров нет"
    out = []
    for o in os_:
        iv = f" (IV {pct(o['orderIv'])}%)" if _d(o.get("orderIv")) else ""
        price = f"@ {px(o.get('price'))}{iv}"
        out.append(
            f"{short_symbol(o['symbol'])}  {o.get('side')} {o.get('qty')} {price}  "
            f"[{o.get('orderStatus')}, исп. {o.get('cumExecQty', '0')}]\n"
            f"  id {o.get('orderId')}"
        )
    return "\n".join(out)


def fmt_greeks(gs: list[dict]) -> str:
    if not gs:
        return "Греки портфеля: нет данных (нет позиций)"
    rows = [[g.get("baseCoin", "?"), f"{_d(g.get('totalDelta')):.4f}", f"{_d(g.get('totalGamma')):.6f}",
             f"{_d(g.get('totalVega')):.2f}", f"{_d(g.get('totalTheta')):.2f}"] for g in gs]
    return _table(["Coin", "Δ", "Γ", "Vega", "Θ"], rows, "lrrrr")


def env_banner(env: str) -> str:
    return "⚠️ MAINNET — РЕАЛЬНЫЕ ДЕНЬГИ" if env == "mainnet" else f"[{env}]"


def parse_iv(raw: str) -> Decimal:
    """'0.55', '55', '55%' -> Decimal('0.55'). Значения > 3 считаются процентами."""
    d = to_dec(raw.strip().rstrip("%"))
    if d > 3:
        d = d / 100
    if not (Decimal("0.01") <= d <= Decimal("3")):
        raise ValueError(f"Странная IV: {raw}")
    return d


def fmt_order_preview(req, t: dict | None) -> str:
    """Текст подтверждения ордера: что отправляем + текущий рынок + премия."""
    lines = [req.describe()]
    if t:
        lines.append(f"рынок: bid {px(t.get('bid1Price'))} / ask {px(t.get('ask1Price'))}  "
                     f"mark {px(t.get('markPrice'))}  IV {pct(t.get('markIv'))}%")
    if req.order_type == "Limit" and req.iv is None:
        lines.append(f"премия: {req.qty * req.price:.2f}")
    elif t:
        ref = _d(t.get("ask1Price") if req.side == "Buy" else t.get("bid1Price")) or _d(t.get("markPrice"))
        lines.append(f"премия ≈ {req.qty * ref:.2f} (по {'ask' if req.side == 'Buy' else 'bid'})")
    lines += getattr(req, "info", [])
    return "\n".join(lines)


def fmt_amend_preview(req, orig: dict, t: dict | None) -> str:
    """Подтверждение изменения ордера: было -> стало + рынок."""
    iv = f" (IV {pct(orig['orderIv'])}%)" if _d(orig.get("orderIv")) else ""
    lines = [
        f"AMEND {short_symbol(req.symbol)}  id {orig['orderId']}",
        f"было:  {orig['side'].upper()} {orig['qty']} @ {px(orig.get('price'))}{iv}",
        "стало: " + fmt_order_preview(req, t),
    ]
    return "\n".join(lines)
