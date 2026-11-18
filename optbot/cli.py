"""CLI для опционов Bybit.

Примеры:
    python -m optbot.cli expiries BTC
    python -m optbot.cli chain BTC 27MAR26 -n 6 [--wide]
    python -m optbot.cli ticker BTC-27MAR26-70000-P
    python -m optbot.cli balance | positions | orders [BASE] | greeks [BASE]
    python -m optbot.cli buy  BTC-27MAR26-70000-P 0.01 --price 150
    python -m optbot.cli sell BTC-27MAR26-70000-P 0.01 --iv 55
    python -m optbot.cli buy  BTC-27MAR26-70000-P 0.01 --market
    python -m optbot.cli amend  BTC-27MAR26-70000-P <orderId> [--price 160 | --iv 50] [--qty 0.02]
    python -m optbot.cli cancel BTC-27MAR26-70000-P <orderId>
    python -m optbot.cli cancel-all [BASE]
    python -m optbot.cli close BTC-27MAR26-70000-P [--price 120]
    python -m optbot.cli signals BTC        сигнал (этап 3): стратегия, ноги, риск — без ордеров; пишется в журнал
    python -m optbot.cli signals BTC --horizon long   14–45 дн. и календарь (E1, без журнала)
                                            сигналов (статистика: python -m optbot.sigjournal --stats 30d)

Любое действие с ордерами требует подтверждения y/N.
"""
from __future__ import annotations

import argparse
import sys
from decimal import Decimal, InvalidOperation

from .bybit_client import BybitError, BybitNetworkError, OptionsClient, OrderRequest, RiskError
from .config import settings
from .journal import Journal
from .signals import SignalEngine, fmt_signal
from .sigjournal import SignalJournal
from .fmt import (
    env_banner, fmt_amend_preview, fmt_balance, fmt_chain, fmt_expiries, fmt_greeks, fmt_orders,
    fmt_order_preview, fmt_positions, fmt_ticker, parse_iv, to_dec,
)


def _dec(s: str) -> Decimal:
    try:
        return to_dec(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))


def _iv(s: str) -> Decimal:
    try:
        return parse_iv(s)
    except (InvalidOperation, ValueError) as e:
        raise argparse.ArgumentTypeError(str(e))


def confirm(text: str) -> bool:
    print(f"\n{env_banner(settings.env)}\n{text}")
    try:
        return input("Отправить? [y/N] ").strip().lower() in ("y", "yes", "д", "да")
    except EOFError:
        return False


def submit(c: OptionsClient, req: OrderRequest) -> None:
    req = c.prepare_order(req)
    if not confirm(fmt_order_preview(req, req.ticker)):
        print("Отменено пользователем")
        return
    journal = Journal(c.cfg)
    req.link_id = journal.register_order(None, "CLI", None, source="cli")  # до отправки
    try:
        res = c.place_order(req)
    except BybitNetworkError as e:
        print(f"Сбой связи с биржей: {e}", file=sys.stderr)
        print("Выясняю статус…", file=sys.stderr)
        try:
            status = c.order_status_after_failure(req)
        except Exception as e2:
            status = f"не удалось выяснить ({e2})"
        print(f"Статус: {status}\nНе повторяйте, пока не проверите orders/positions.", file=sys.stderr)
        raise SystemExit(2)
    except Exception:
        journal.forget_order(req.link_id)  # не сетевая ошибка — ордер точно не на бирже
        raise
    print(f"Ордер принят: orderId={res.get('orderId')}  linkId={res.get('orderLinkId')}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="optbot", description="Опционы Bybit (V5)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("expiries", help="список экспираций")
    s.add_argument("base")

    s = sub.add_parser("chain", help="цепочка опционов вокруг ATM")
    s.add_argument("base")
    s.add_argument("expiry", help="напр. 27MAR26")
    s.add_argument("-n", type=int, default=6, help="страйков в каждую сторону от ATM")
    s.add_argument("--wide", action="store_true", help="с дельтами, IV и OI")

    s = sub.add_parser("ticker", help="котировка и греки инструмента")
    s.add_argument("symbol")

    s = sub.add_parser("signals", help="сигнал: стратегия, ноги, риск (без ордеров; история — из БД сборщика)")
    s.add_argument("base")
    s.add_argument("--horizon", choices=("short", "long"), default="short",
                   help="long — 14–45 дн. и календарный спред (бумажные книги E1; в журнал сигналов не пишется)")

    sub.add_parser("balance", help="баланс UTA")
    for name, hlp in (("positions", "открытые позиции"), ("orders", "открытые ордера"),
                      ("greeks", "греки портфеля")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("base", nargs="?")

    for side in ("buy", "sell"):
        s = sub.add_parser(side, help=f"{side} опцион")
        s.add_argument("symbol")
        s.add_argument("qty", type=_dec)
        g = s.add_mutually_exclusive_group(required=True)
        g.add_argument("--price", type=_dec)
        g.add_argument("--iv", type=_iv, help="0.55 или 55 (= 55%%)")
        g.add_argument("--market", action="store_true")
        s.add_argument("--tif", default="GTC", choices=["GTC", "IOC", "FOK", "PostOnly"])
        s.add_argument("--reduce-only", action="store_true")

    s = sub.add_parser("cancel", help="отменить ордер")
    s.add_argument("symbol")
    s.add_argument("order_id")

    s = sub.add_parser("amend", help="изменить открытый лимитный ордер")
    s.add_argument("symbol")
    s.add_argument("order_id")
    s.add_argument("--qty", type=_dec)
    g = s.add_mutually_exclusive_group()
    g.add_argument("--price", type=_dec)
    g.add_argument("--iv", type=_iv)

    s = sub.add_parser("cancel-all", help="отменить все опционные ордера")
    s.add_argument("base", nargs="?")

    s = sub.add_parser("close", help="закрыть позицию целиком (reduce-only)")
    s.add_argument("symbol")
    s.add_argument("--price", type=_dec, help="лимитная цена; без неё — market")
    return p


def run(args: argparse.Namespace, c: OptionsClient) -> None:
    cmd = args.cmd
    if cmd == "expiries":
        print(fmt_expiries(args.base, c.expiries(args.base)))
    elif cmd == "chain":
        print(fmt_chain(c.chain(args.base, args.expiry, args.n), wide=args.wide))
    elif cmd == "ticker":
        print(fmt_ticker(c.ticker(c.resolve_symbol(args.symbol).symbol)))
    elif cmd == "signals" and args.horizon == "long":
        eng = SignalEngine(c, c.cfg)
        mk = eng.market(args.base)
        print(fmt_signal(eng.compute(args.base, horizon="long", market=mk), c.cfg.env))
        print()
        print(fmt_signal(eng.calendar_signal(args.base, market=mk), c.cfg.env, "Календарный спред:"))
    elif cmd == "signals":
        sig = SignalEngine(c, c.cfg).compute(args.base)
        try:  # тоже выдача — в журнал сигналов; сбой записи не мешает показать сигнал
            SignalJournal(c.cfg).record(sig, "cli", user_name="CLI")  # номер — в sig.id
        except Exception as e:
            print(f"Журнал сигналов: не записан ({e})", file=sys.stderr)
        print(fmt_signal(sig, c.cfg.env))
    elif cmd == "balance":
        print(fmt_balance(c.balance()))
    elif cmd == "positions":
        print(fmt_positions(c.positions(args.base)))
    elif cmd == "orders":
        print(fmt_orders(c.open_orders(args.base)))
    elif cmd == "greeks":
        print(fmt_greeks(c.greeks(args.base)))
    elif cmd in ("buy", "sell"):
        submit(c, OrderRequest(
            symbol=args.symbol, side=cmd.capitalize(), qty=args.qty,
            order_type="Market" if args.market else "Limit",
            price=args.price, iv=args.iv, reduce_only=args.reduce_only, time_in_force=args.tif,
        ))
    elif cmd == "cancel":
        sym = c.resolve_symbol(args.symbol).symbol
        if confirm(f"CANCEL {sym} {args.order_id}"):
            print("Отменён:", c.cancel_order(sym, args.order_id).get("orderId"))
    elif cmd == "amend":
        req, orig = c.amend_request(args.symbol, args.order_id, args.qty, args.price, args.iv)
        if confirm(fmt_amend_preview(req, orig, req.ticker)):
            try:
                print("Изменён:", c.amend_order(args.order_id, req).get("orderId"))
            except BybitNetworkError as e:
                print(f"Сбой связи с биржей: {e}\nВыясняю статус…", file=sys.stderr)
                try:
                    status = c.amend_status_after_failure(args.order_id, req)
                except Exception as e2:
                    status = f"не удалось выяснить ({e2})"
                print(f"Статус: {status}\nНе повторяйте, пока не проверите orders.", file=sys.stderr)
                raise SystemExit(2)
        else:
            print("Отменено пользователем")
    elif cmd == "cancel-all":
        what = args.base.upper() if args.base else "ВСЕ базовые активы"
        if confirm(f"CANCEL ALL option orders: {what}"):
            res = c.cancel_all(args.base)
            print(f"Отменено ордеров: {len(res.get('list', []))}")
    elif cmd == "close":
        submit(c, c.close_position_request(args.symbol, args.price))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run(args, OptionsClient())
    except (BybitError, RiskError, ValueError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
