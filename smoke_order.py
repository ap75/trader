import time
from decimal import Decimal
from optbot.bybit_client import OptionsClient, OrderRequest

c = OptionsClient()
exp = c.expiries('BTC')[1][0]
ch = c.chain('BTC', exp, strikes_around_atm=0)
put = ch['rows'][0][2]['symbol']
print('Инструмент:', put)

req = c.prepare_order(OrderRequest(symbol=put, side='Buy', qty=Decimal('0.01'), price=Decimal('10')))
print('Ордер:', req.describe())
res = c.place_order(req)
print('Выставлен:', res)

time.sleep(1)
orders = c.open_orders('BTC')
print('Открытые ордера:', [(o['symbol'], o['side'], o['qty'], o['price'], o['orderStatus']) for o in orders])

print('Отмена:', c.cancel_order(put, res['orderId']))
time.sleep(1)
print('Осталось ордеров:', len(c.open_orders('BTC')))
