"""DVOL Deribit — история IV30 для перцентиля длинного горизонта BTC/ETH (research этап 7.4).

Своей истории IV30 у бота мало, а DVOL (30-дневный индекс IV Deribit) идёт с 2021 г. и почти не отличается от
IV30 бота: IV30 / DVOL ≈ 0.94, корреляция дневных изменений 0.98–0.99. Перцентиль от постоянного множителя не
меняется, поэтому, пока своей истории IV30 меньше OWN_DAYS дней (с первого среза сборщика), перцентиль IV30 —
место IV30 / k в истории DVOL за SIG_LOOKBACK_DAYS. k — медиана IV30 / DVOL на совпавших часах этого окна (своя
оценка, не меньше MIN_OVERLAP часов), иначе — оценка research (REF_RATIO). SOL — DVOL нет, как раньше.

DVOL в момент X — close часового бара, начавшегося в X − 1 ч (research 7.1). Бот докачивает часовые бары раз в
сутки (`DVOL_URL`, пусто — не качать) в таблицу dvol_h1 — окно SIG_LOOKBACK_DAYS с запасом; последний бар старше
STALE_MS — перцентиль снова по своей истории, администратору — предупреждение.

    python -m optbot.dvol --update | --show
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import db
from .config import Settings, settings
from .fmt import DAY_MS, HOUR_MS

log = logging.getLogger("optbot.dvol")

BASES = ("BTC", "ETH")                 # у Deribit DVOL SOL нет (был только 2022-05 … 2022-11)
REF_RATIO = {"BTC": 0.938, "ETH": 0.947}   # IV30 бота / DVOL, research этап 7.4 (2026-11-15 … 11-22)
OWN_DAYS = 365                         # своей истории IV30 меньше — перцентиль по DVOL
MIN_OVERLAP = 48                       # совпавших часов (IV30 бота и DVOL) для своей оценки k
STALE_MS = 3 * DAY_MS                  # последний бар DVOL старше — не используется
PAGE = 1000                            # баров на страницу Deribit
MAX_PAGES = 50
TIMEOUT_S = 30
SPARE_DAYS = 2                         # запас к окну SIG_LOOKBACK_DAYS при докачке


def fetch_page(url: str, base: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
    """До PAGE последних часовых баров DVOL с меткой в [start_ms, end_ms] -> [(начало бара, close долей)]."""
    q = urllib.parse.urlencode(dict(currency=base, start_timestamp=start_ms, end_timestamp=end_ms, resolution=3600))
    req = urllib.request.Request(f"{url}?{q}", headers={"User-Agent": "options-bot"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        js = json.loads(r.read().decode("utf-8"))
    if "result" not in js:
        raise ValueError(f"ответ Deribit без result: {str(js.get('error', js))[:200]}")
    out = []
    for x in js["result"].get("data") or []:
        ts, close = int(x[0]), float(x[4])
        if start_ms <= ts <= end_ms and 0 < close < 1000:
            out.append((ts, close / 100))
    return out


def _fill(conn, fetcher, url: str, base: str, lo: int, hi: int, now_ms: int) -> int:
    """Бары с меткой в [lo, hi] — страницами назад от hi; каждая страница пишется сразу. -> строк добавлено."""
    n, end = 0, hi
    for _ in range(MAX_PAGES):
        if end < lo:
            break
        page = fetcher(url, base, lo, end)
        if not page:
            break
        with conn:
            n += max(0, conn.executemany("INSERT OR IGNORE INTO dvol_h1 (base, ts, close) VALUES (?,?,?)",
                                         [(base, t, v) for t, v in page if t + HOUR_MS <= now_ms]).rowcount)
        oldest = min(t for t, _ in page)
        if len(page) < PAGE or oldest <= lo or oldest > end:
            break
        end = oldest - 1
    else:
        log.warning("DVOL %s: докачано не полностью (больше %d страниц)", base, MAX_PAGES)
    return n


@dataclass
class UpdateResult:
    added: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)      # активы, у которых последний бар старше STALE_MS

    def __str__(self) -> str:
        parts = [f"{b} +{n}" for b, n in self.added.items()]
        parts += [f"ошибка: {e}" for e in self.errors]
        parts += [f"{b}: устарел" for b in self.stale]
        return "DVOL: " + ("; ".join(parts) or "нечего делать")


def update(conn, cfg: Settings = settings, now_ms: int | None = None, fetcher=fetch_page,
           bases: tuple[str, ...] = BASES) -> UpdateResult:
    """Докачать часовые бары DVOL за окно SIG_LOOKBACK_DAYS (+ запас): новые — от последнего сохранённого,
    недостающее начало окна — назад от первого. Без DVOL_URL — только проверка свежести."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    res = UpdateResult()
    lo = now_ms - (cfg.sig.lookback_days + SPARE_DAYS) * DAY_MS
    for base in bases:
        if base not in cfg.base_coins:
            continue
        if cfg.dvol_url:
            try:
                r = conn.execute("SELECT MIN(ts) a, MAX(ts) b FROM dvol_h1 WHERE base = ? AND ts >= ?",
                                 (base, lo)).fetchone()
                n = _fill(conn, fetcher, cfg.dvol_url, base, max(lo, r["b"] or lo), now_ms, now_ms)
                if r["a"] is not None and r["a"] > lo + HOUR_MS:
                    n += _fill(conn, fetcher, cfg.dvol_url, base, lo, r["a"] - 1, now_ms)
                res.added[base] = n
            except Exception as e:
                log.warning("DVOL %s не докачан: %s", base, e)
                res.errors.append(f"{base}: {type(e).__name__}: {e}")
        last = last_known_ms(conn, base)
        if last is None or now_ms - last > STALE_MS:
            res.stale.append(base)
    return res


def last_known_ms(conn, base: str) -> int | None:
    """С какого момента известно последнее значение (конец последнего бара)."""
    t = conn.execute("SELECT MAX(ts) t FROM dvol_h1 WHERE base = ?", (base,)).fetchone()["t"]
    return None if t is None else t + HOUR_MS


def bars(conn, base: str, lo_ms: int, hi_ms: int) -> dict[int, float]:
    """{начало бара: DVOL} для значений, известных в [lo_ms, hi_ms] (конец бара в окне)."""
    return {r["ts"]: r["close"] for r in conn.execute(
        "SELECT ts, close FROM dvol_h1 WHERE base = ? AND ts >= ? AND ts <= ? ORDER BY ts",
        (base, lo_ms - HOUR_MS, hi_ms - HOUR_MS))}


def bar_for(ts_ms: int) -> int:
    """Бар DVOL, известный в момент ts_ms: начавшийся за час до начала текущего часа."""
    return ts_ms // HOUR_MS * HOUR_MS - HOUR_MS


def ratio(own: list[tuple[int, float]], dv: dict[int, float], base: str) -> tuple[float | None, int, str]:
    """k = медиана IV30 / DVOL по срезам с известным DVOL -> (k, совпавших часов, own | research)."""
    by_hour: dict[int, float] = {}
    for ts, iv in own:
        b = bar_for(ts)
        if iv and b in dv and dv[b] > 0:
            by_hour[b] = iv / dv[b]          # несколько срезов в час — последний
    if len(by_hour) >= MIN_OVERLAP:
        return statistics.median(by_hour.values()), len(by_hour), "own"
    return REF_RATIO.get(base), len(by_hour), "research"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    ap = argparse.ArgumentParser(prog="python -m optbot.dvol", description="DVOL Deribit для перцентиля IV30")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--update", action="store_true", help="докачать часовые бары DVOL (BTC, ETH)")
    g.add_argument("--show", action="store_true", help="что накоплено и последнее значение")
    a = ap.parse_args(argv)
    conn = db.connect(settings.db_file)
    try:
        if a.update:
            res = update(conn, settings)
            print(f"[{settings.env}] {res}")
            if res.errors:
                return 1
        for b in BASES:
            r = conn.execute("SELECT COUNT(*) n, MIN(ts) a, MAX(ts) z FROM dvol_h1 WHERE base = ?", (b,)).fetchone()
            if not r["n"]:
                print(f"{b}: DVOL нет")
                continue
            last = conn.execute("SELECT close FROM dvol_h1 WHERE base = ? AND ts = ?", (b, r["z"])).fetchone()
            d = lambda ms: time.strftime("%Y-%m-%d %H:%M", time.gmtime(ms / 1000))  # noqa: E731
            print(f"{b}: {r['n']} ч, {d(r['a'])} … {d(r['z'] + HOUR_MS)} UTC, последнее {last['close'] * 100:.1f}%")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
