"""
Bloomberg Terminal RU — стресс-тест на 10 000 прогонов.

Бьёт по каждому эндпоинту много раз, валидирует КАЖДУЮ метрику
(цены, изменения, свечи графика, страх/жадность, новости, HLS-поток),
считает pass/fail и печатает финальный отчёт.

Запуск:  .venv/bin/python tests/stress_test.py [--runs N] [--base URL]
"""

import argparse
import asyncio
import time
from collections import defaultdict

BASE = "http://localhost:8080"  # noqa: overwritten in main()
TOTAL_RUNS = 10000
CONCURRENCY = 8        # один dev-воркер uvicorn: блокирующие вызовы → держим умеренно
RETRIES = 2            # транзиентный сброс соединения ≠ сломанная метрика — даём ретрай

# Группы инструментов, которые ОБЯЗАНЫ присутствовать в /api/market
REQUIRED = {
    "Индексы": ["S&P 500", "NASDAQ", "DOW", "FTSE 100", "DAX", "Nikkei"],
    "Крипто": ["Bitcoin", "Ethereum", "Solana", "BNB"],
    "Сырьё": ["Gold", "Silver", "Platinum", "Palladium", "Copper", "Oil (WTI)", "Brent", "Nat. Gas"],
    "Валюты": ["EUR/USD", "USD/RUB", "GBP/USD", "USD/JPY", "USD/CNY"],
    "Акции US": ["NVIDIA", "Apple", "Microsoft", "Amazon", "Alphabet", "Meta", "Tesla", "Berkshire"],
}
ALL_REQUIRED = {n for grp in REQUIRED.values() for n in grp}

OHLCV_SYMBOLS = ["SPY", "NVDA", "AAPL", "BTC-USD", "GC=F", "^GSPC", "TSLA", "EURUSD=X"]
OHLCV_PERIODS = [("1d", "5m"), ("5d", "30m"), ("1mo", "1d"), ("3mo", "1d"),
                 ("6mo", "1d"), ("1y", "1d"), ("5y", "1wk")]

# Результаты: endpoint -> {"ok": n, "fail": n, "errors": [..], "latency": [..]}
stats: dict = defaultdict(lambda: {"ok": 0, "fail": 0, "errors": [], "lat": []})


def record(ep: str, ok: bool, lat: float, err: str = ""):
    s = stats[ep]
    s["lat"].append(lat)
    if ok:
        s["ok"] += 1
    else:
        s["fail"] += 1
        if err and len(s["errors"]) < 5:
            s["errors"].append(err)


async def _get(client, url, **kw):
    """GET с ретраями на транзиентные сетевые сбросы (одиночный dev-воркер под нагрузкой)."""
    import httpx
    last = None
    for attempt in range(RETRIES + 1):
        try:
            return await client.get(url, **kw)
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last = e
            await asyncio.sleep(0.25 * (attempt + 1))
    raise last


# ── Валидаторы каждого эндпоинта ──────────────────────────────────────────

async def check_health(client):
    import httpx
    t0 = time.time()
    try:
        r = await _get(client, f"{BASE}/health")
        d = r.json()
        ok = r.status_code == 200 and d.get("status") == "ok"
        record("health", ok, time.time() - t0, "" if ok else str(d)[:100])
    except Exception as e:
        record("health", False, time.time() - t0, f"{type(e).__name__}: {e}")


async def check_market(client):
    import httpx
    t0 = time.time()
    try:
        r = await _get(client, f"{BASE}/api/market")
        d = r.json()
        names = {m["name"] for m in d}
        missing = ALL_REQUIRED - names
        bad = [m for m in d if not isinstance(m.get("price"), (int, float))
               or not isinstance(m.get("change"), (int, float)) or m["price"] <= 0]
        ok = r.status_code == 200 and len(d) >= 28 and not missing and not bad
        err = ""
        if missing:
            err = f"нет инструментов: {sorted(missing)[:5]}"
        elif bad:
            err = f"битые значения: {[b.get('name') for b in bad[:3]]}"
        record("market", ok, time.time() - t0, err)
    except Exception as e:
        record("market", False, time.time() - t0, f"{type(e).__name__}: {e}")


async def check_fear_greed(client):
    t0 = time.time()
    try:
        r = await _get(client, f"{BASE}/api/fear-greed")
        d = r.json()
        v = d.get("value")
        ok = r.status_code == 200 and isinstance(v, (int, float)) and 0 <= v <= 100 and d.get("label")
        record("fear-greed", ok, time.time() - t0, "" if ok else str(d)[:100])
    except Exception as e:
        record("fear-greed", False, time.time() - t0, f"{type(e).__name__}: {e}")


async def check_ohlcv(client, symbol, period, interval):
    t0 = time.time()
    try:
        r = await _get(client, f"{BASE}/api/ohlcv",
                             params={"symbol": symbol, "period": period, "interval": interval})
        d = r.json()
        ok = (r.status_code == 200 and isinstance(d, list) and len(d) >= 1
              and all(k in d[0] for k in ("time", "open", "high", "low", "close")))
        # high>=low sanity на первых свечах
        if ok:
            for c in d[:20]:
                if c["high"] < c["low"] or c["close"] <= 0:
                    ok = False
                    break
        record(f"ohlcv", ok, time.time() - t0,
               "" if ok else f"{symbol}/{period}: {str(d)[:80]}")
    except Exception as e:
        record("ohlcv", False, time.time() - t0, f"{symbol}/{period} {type(e).__name__}: {e}")


async def check_news(client):
    t0 = time.time()
    try:
        r = await _get(client, f"{BASE}/api/news")
        d = r.json()
        ok = r.status_code == 200 and isinstance(d, list) and len(d) >= 1 and "title" in d[0]
        record("news", ok, time.time() - t0, "" if ok else str(d)[:100])
    except Exception as e:
        record("news", False, time.time() - t0, f"{type(e).__name__}: {e}")


async def check_live_hls(client):
    t0 = time.time()
    try:
        r = await _get(client, f"{BASE}/api/live-hls")
        d = r.json()
        ok = r.status_code == 200 and d.get("url", "").endswith(".m3u8")
        record("live-hls", ok, time.time() - t0, "" if ok else str(d)[:100])
    except Exception as e:
        record("live-hls", False, time.time() - t0, f"{type(e).__name__}: {e}")


async def check_hls_playlist(client):
    t0 = time.time()
    try:
        r = await _get(client, f"{BASE}/api/hls/index.m3u8")
        txt = r.text
        ok = (r.status_code == 200 and txt.startswith("#EXTM3U")
              and "/api/hls/seg" in txt
              and r.headers.get("access-control-allow-origin") == "*")
        record("hls-playlist", ok, time.time() - t0,
               "" if ok else f"status={r.status_code} head={txt[:60]!r}")
    except Exception as e:
        record("hls-playlist", False, time.time() - t0, f"{type(e).__name__}: {e}")


# ── Раннер ─────────────────────────────────────────────────────────────────

async def worker(client, task_queue):
    while True:
        try:
            fn = task_queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        await fn(client)
        task_queue.task_done()


async def main():
    global BASE
    import httpx
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=TOTAL_RUNS)
    parser.add_argument("--base", type=str, default=BASE)
    args = parser.parse_args()
    BASE = args.base
    runs = args.runs

    # Распределение прогонов по эндпоинтам.
    # Кэшируемые/лёгкие — много; внешние (ohlcv) — представительная выборка по символам/периодам.
    queue: asyncio.Queue = asyncio.Queue()
    weights = {
        "market": 0.34, "live-hls": 0.14, "hls-playlist": 0.12,
        "fear-greed": 0.12, "news": 0.10, "health": 0.08, "ohlcv": 0.10,
    }
    counts = {k: int(runs * w) for k, w in weights.items()}
    # добор до runs
    counts["market"] += runs - sum(counts.values())

    oi = 0
    for _ in range(counts["health"]):       queue.put_nowait(check_health)
    for _ in range(counts["market"]):       queue.put_nowait(check_market)
    for _ in range(counts["fear-greed"]):   queue.put_nowait(check_fear_greed)
    for _ in range(counts["news"]):         queue.put_nowait(check_news)
    for _ in range(counts["live-hls"]):     queue.put_nowait(check_live_hls)
    for _ in range(counts["hls-playlist"]): queue.put_nowait(check_hls_playlist)
    for _ in range(counts["ohlcv"]):
        sym = OHLCV_SYMBOLS[oi % len(OHLCV_SYMBOLS)]
        per, iv = OHLCV_PERIODS[oi % len(OHLCV_PERIODS)]
        oi += 1
        queue.put_nowait(lambda c, s=sym, p=per, i=iv: check_ohlcv(c, s, p, i))

    total = queue.qsize()
    print(f"▶ Стресс-тест: {total} прогонов, конкурентность {CONCURRENCY}, цель {BASE}")
    print(f"  распределение: {counts}\n")

    t0 = time.time()
    async with httpx.AsyncClient(timeout=40) as client:
        workers = [asyncio.create_task(worker(client, queue)) for _ in range(CONCURRENCY)]
        # прогресс
        last = 0
        while not queue.empty():
            await asyncio.sleep(2)
            done = total - queue.qsize()
            if done - last >= 200 or queue.empty():
                print(f"  ... {done}/{total} ({done*100//total}%)")
                last = done
        await queue.join()
        for w in workers:
            w.cancel()

    elapsed = time.time() - t0

    # ── Отчёт ──
    print("\n" + "=" * 64)
    print("  ИТОГОВЫЙ ОТЧЁТ")
    print("=" * 64)
    print(f"{'Эндпоинт':<16}{'OK':>8}{'FAIL':>7}{'%':>7}{'ср.мс':>9}{'p95.мс':>9}")
    print("-" * 64)
    grand_ok = grand_fail = 0
    for ep in sorted(stats):
        s = stats[ep]
        n = s["ok"] + s["fail"]
        grand_ok += s["ok"]; grand_fail += s["fail"]
        lat = sorted(s["lat"])
        avg = sum(lat) / len(lat) * 1000 if lat else 0
        p95 = lat[int(len(lat) * 0.95)] * 1000 if lat else 0
        pct = s["ok"] * 100 / n if n else 0
        print(f"{ep:<16}{s['ok']:>8}{s['fail']:>7}{pct:>6.1f}%{avg:>9.0f}{p95:>9.0f}")
    print("-" * 64)
    tot = grand_ok + grand_fail
    print(f"{'ВСЕГО':<16}{grand_ok:>8}{grand_fail:>7}{grand_ok*100/tot:>6.1f}%")
    print(f"\n⏱  {tot} прогонов за {elapsed:.1f}с  =  {tot/elapsed:.0f} запр/сек")

    # Ошибки
    any_err = False
    for ep in sorted(stats):
        if stats[ep]["errors"]:
            any_err = True
            print(f"\n✗ {ep} — примеры ошибок:")
            for e in stats[ep]["errors"]:
                print(f"    {e}")
    if not any_err and grand_fail == 0:
        print("\n✅ ВСЕ ПРОГОНЫ УСПЕШНЫ — каждая метрика валидна на всех итерациях.")

    return grand_fail


if __name__ == "__main__":
    import sys
    fails = asyncio.run(main())
    sys.exit(1 if fails else 0)
