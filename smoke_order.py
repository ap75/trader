"""Smoke-тест ордеров: лимитная покупка пута далеко от рынка -> отмена.

Только для demo/testnet: на mainnet скрипт не запускается.
Запуск: .venv/bin/python smoke_order.py
"""
import sys
import time
from decimal import Decimal

from optbot.bybit_client import OptionsClient, OrderRequest
from optbot.config import settings

if settings.env not in ("testnet", "demo"):
    sys.exit(f"smoke_order.py выставляет реальный ордер — только testnet/demo (BYBIT_ENV={settings.env})")
print(f"Окружение: {settings.env}")

c = OptionsClient()
exps = c.expiries("BTC")
if not exps:
    sys.exit("Нет экспираций BTC")
exp = exps[1][0] if len(exps) > 1 else exps[0][0]  # не ближайшая: вдруг истекает прямо сейчас
ch = c.chain("BTC", exp, strikes_around_atm=3)
# самый дорогой пут возле ATM: 10% от его mark заведомо далеко от рынка и выше минимального тика
puts = [p for _, _, p in ch["rows"] if p]  # строки цепочки — уже тикеры
if not puts:
    sys.exit(f"Нет путов в цепочке BTC {exp}")
t = max(puts, key=lambda p: Decimal(p.get("markPrice") or "0"))
put = t["symbol"]
mark, bid = Decimal(t.get("markPrice") or "0"), Decimal(t.get("bid1Price") or "0")
price = (mark * Decimal("0.1")).quantize(Decimal("1"))
if price <= 0 or (bid and price >= bid * Decimal("0.5")) or price >= mark * Decimal("0.5"):
    sys.exit(f"Не удалось подобрать цену далеко от рынка: {put} mark {mark} bid {bid} -> {price}")
print("Инструмент:", put, "mark", mark, "bid", bid, "-> цена", price)

req = c.prepare_order(OrderRequest(symbol=put, side="Buy", qty=Decimal("0.01"), price=price))
print("Ордер:", req.describe(), "|", "; ".join(req.info))
res = c.place_order(req)
print("Выставлен:", res.get("orderId"))
try:
    time.sleep(1)
    o = c.get_order(put, res["orderId"])
    print("Открыт:", o and (o["symbol"], o["side"], o["qty"], o["price"], o["orderStatus"]))
finally:
    print("Отмена:", c.cancel_order(req.symbol, res["orderId"]).get("orderId"))
    time.sleep(1)
    left = c.get_order(put, res["orderId"])
    print("Ордер отменён" if not left else f"ВНИМАНИЕ: ордер всё ещё открыт: {left}")
