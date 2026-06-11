"""Bloomberg-style dashboard: RSS news + live market data, all in Russian."""

import asyncio
import os
import time
import xml.etree.ElementTree as ET
from dotenv import load_dotenv

load_dotenv()

_MARKET_CACHE: dict = {}
_NEWS_CACHE: dict = {}
_MARKET_TTL = 30    # 30 sec — near real-time
_NEWS_TTL = 300     # 5 min — RSS doesn't update faster


# ── Market data via Yahoo Finance ────────────────────────────────────────────

_TICKERS = {
    # Indices
    "S&P 500":      "^GSPC",
    "NASDAQ":       "^IXIC",
    "DOW":          "^DJI",
    "FTSE 100":     "^FTSE",
    "DAX":          "^GDAXI",
    "Nikkei":       "^N225",
    # Crypto
    "Bitcoin":      "BTC-USD",
    "Ethereum":     "ETH-USD",
    "Solana":       "SOL-USD",
    "BNB":          "BNB-USD",
    # Commodities
    "Gold":         "GC=F",
    "Silver":       "SI=F",
    "Platinum":     "PL=F",
    "Palladium":    "PA=F",
    "Copper":       "HG=F",
    "Oil (WTI)":    "CL=F",
    "Brent":        "BZ=F",
    "Nat. Gas":     "NG=F",
    # FX
    "EUR/USD":      "EURUSD=X",
    "USD/RUB":      "RUB=X",
    "GBP/USD":      "GBPUSD=X",
    "USD/JPY":      "JPY=X",
    "USD/CNY":      "CNY=X",
    # Top US stocks (movers)
    "NVIDIA":       "NVDA",
    "Apple":        "AAPL",
    "Microsoft":    "MSFT",
    "Amazon":       "AMZN",
    "Alphabet":     "GOOGL",
    "Meta":         "META",
    "Tesla":        "TSLA",
    "Berkshire":    "BRK-B",
}


def _fetch_one(name: str, sym: str) -> dict | None:
    import yfinance as yf
    import math
    try:
        info = yf.Ticker(sym).fast_info
        price = info.last_price or 0
        prev = info.regular_market_previous_close or price
        change = ((price - prev) / prev * 100) if prev else 0
        price = 0 if math.isnan(price) or math.isinf(price) else price
        change = 0 if math.isnan(change) or math.isinf(change) else change
        if price > 0:
            return {"name": name, "price": round(price, 4), "change": round(change, 2)}
    except Exception:
        pass
    return None


def _fetch_market_sync() -> list[dict]:
    """Fetch all tickers in parallel — cuts cold-start from ~12s to ~2s. Preserves _TICKERS order."""
    from concurrent.futures import ThreadPoolExecutor
    items = list(_TICKERS.items())
    with ThreadPoolExecutor(max_workers=12) as ex:
        fetched = list(ex.map(lambda kv: _fetch_one(*kv), items))
    return [r for r in fetched if r]


async def get_market_data() -> list[dict]:
    now = time.time()
    if _MARKET_CACHE.get("ts", 0) + _MARKET_TTL > now:
        return _MARKET_CACHE.get("data", [])

    loop = asyncio.get_event_loop()
    have_cache = bool(_MARKET_CACHE.get("data"))
    # На холодном старте Yahoo иногда отдаёт пусто (нужен crumb/throttle) —
    # ретраим с паузой. Если кэш уже есть, не упорствуем (фронт обновится позже).
    attempts = 1 if have_cache else 4
    result = []
    for i in range(attempts):
        try:
            result = await loop.run_in_executor(None, _fetch_market_sync)
        except Exception:
            result = []
        if result:
            break
        if i < attempts - 1:
            await asyncio.sleep(2 * (i + 1))

    if result:
        _MARKET_CACHE["data"] = result
        _MARKET_CACHE["ts"] = now
        return result
    return _MARKET_CACHE.get("data", [])


# ── Bloomberg RSS feeds ───────────────────────────────────────────────────────

_RSS_FEEDS = [
    ("Рынки",     "https://feeds.bloomberg.com/markets/news.rss"),
    ("Технологии","https://feeds.bloomberg.com/technology/news.rss"),
    ("Политика",  "https://feeds.bloomberg.com/politics/news.rss"),
    ("Экономика", "https://feeds.bloomberg.com/economics/news.rss"),
    ("Финансы",   "https://feeds.bloomberg.com/wealth/news.rss"),
]


async def _fetch_rss(name: str, url: str) -> list[dict]:
    import httpx
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(url, headers=headers)
            root = ET.fromstring(r.text)
    except Exception:
        return []

    items = []
    ns = {"media": "http://search.yahoo.com/mrss/"}
    for item in root.findall(".//item")[:8]:
        title = (item.findtext("title") or "").strip()
        desc = (item.findtext("description") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        img = ""
        mc = item.find("media:content", ns)
        if mc is not None:
            img = mc.get("url", "")
        if title:
            items.append({"title": title, "desc": desc[:200], "link": link, "pub": pub, "cat": name, "img": img})
    return items


async def get_news() -> list[dict]:
    now = time.time()
    if _NEWS_CACHE.get("ts", 0) + _NEWS_TTL > now:
        return _NEWS_CACHE.get("data", [])

    tasks = [_fetch_rss(name, url) for name, url in _RSS_FEEDS]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    raw = []
    for r in results:
        if isinstance(r, list):
            raw.extend(r)

    # Deduplicate: primary key = link URL, fallback = first 80 chars of title
    seen: set[str] = set()
    articles = []
    for a in raw:
        key = (a.get("link") or a["title"][:80]).strip().rstrip("/")
        if key and key not in seen:
            seen.add(key)
            articles.append(a)

    _NEWS_CACHE["data"] = articles
    _NEWS_CACHE["ts"] = now
    return articles


_TNEWS_CACHE: dict = {"data": [], "ts": 0}
_TNEWS_TTL = 300            # переведённые новости держим 5 мин
_tnews_refreshing = False   # защита от параллельных перекачек


async def _build_translated_news(lang: str) -> list[dict]:
    """Тяжёлая часть: перевод заголовков и описаний батчами."""
    import translator as tr
    import text_nodes as tn

    articles = await get_news()
    if not articles:
        return []

    nodes = []
    for i, a in enumerate(articles):
        nodes.append({"id": f"t{i}", "text": a["title"]})
        if a.get("desc"):
            nodes.append({"id": f"d{i}", "text": a["desc"]})

    batches = tn.make_batches(nodes, max_chars=4000)
    translations = await tr.translate_all_batches(batches, lang)

    result = []
    for i, a in enumerate(articles):
        result.append({
            **a,
            "title_ru": translations.get(f"t{i}", a["title"]),
            "desc_ru": translations.get(f"d{i}", a.get("desc", "")),
        })
    return result


async def get_translated_news(lang: str = "ru") -> list[dict]:
    """
    Stale-while-revalidate: НИКОГДА не блокирует запрос на перевод.
    - свежий кэш → отдаём сразу
    - устаревший кэш → отдаём старое, перевод обновляем в фоне
    - пустой кэш (только первый раз) → переводим синхронно
    """
    global _tnews_refreshing
    now = time.time()
    fresh = _TNEWS_CACHE["ts"] + _TNEWS_TTL > now
    have = bool(_TNEWS_CACHE["data"])

    if have and fresh:
        return _TNEWS_CACHE["data"]

    if have and not fresh:
        # отдаём stale, обновляем в фоне (один раз)
        if not _tnews_refreshing:
            _tnews_refreshing = True

            async def _refresh():
                global _tnews_refreshing
                try:
                    data = await _build_translated_news(lang)
                    if data:
                        _TNEWS_CACHE["data"] = data
                        _TNEWS_CACHE["ts"] = time.time()
                finally:
                    _tnews_refreshing = False

            asyncio.create_task(_refresh())
        return _TNEWS_CACHE["data"]

    # кэша нет вообще — строим под локом, чтобы конкурентные запросы не дублировали работу
    lock = _TNEWS_CACHE.setdefault("lock", asyncio.Lock())
    if lock.locked():
        # кто-то уже строит — не блокируемся, отдаём что есть (страница уже отрендерена сервером)
        return _TNEWS_CACHE["data"]
    async with lock:
        if _TNEWS_CACHE["data"] and _TNEWS_CACHE["ts"] + _TNEWS_TTL > time.time():
            return _TNEWS_CACHE["data"]
        data = await _build_translated_news(lang)
        if data:
            _TNEWS_CACHE["data"] = data
            _TNEWS_CACHE["ts"] = time.time()
        return data
