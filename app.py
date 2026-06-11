"""FastAPI application: catchall Bloomberg proxy."""

import asyncio
import json
import os
from collections import defaultdict

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Bloomberg Proxy", docs_url=None, redoc_url=None)

# In-memory dashboard cache — populated at startup, refreshed every 5 min
_DASH_HTML: str = ""
_DASH_LOCK = asyncio.Lock()


async def _refresh_dashboard():
    """Fetch market + news, render HTML, store in _DASH_HTML."""
    global _DASH_HTML
    try:
        from dashboard import get_market_data, get_translated_news
        market, news = await asyncio.gather(
            get_market_data(),
            get_translated_news(TARGET_LANG := os.getenv("TARGET_LANG", "ru")),
        )
        html = _render_dashboard(market, news)
        async with _DASH_LOCK:
            _DASH_HTML = html
    except Exception:
        pass


async def _background_refresh_loop():
    """Refresh full dashboard every 5 min. Market data is live via /api/market."""
    while True:
        await asyncio.sleep(300)
        await _refresh_dashboard()


@app.on_event("startup")
async def startup():
    """Pre-load dashboard data so first request is instant."""
    asyncio.create_task(_initial_load())

async def _initial_load():
    """Build dashboard immediately on startup, then hand off to refresh loop."""
    await _refresh_dashboard()
    asyncio.create_task(_background_refresh_loop())

# CORS — нужно для bookmarklet (bloomberg.com → наш сервер)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

TARGET_LANG = os.getenv("TARGET_LANG", "ru")
BLOOMBERG_BASE = os.getenv("BLOOMBERG_BASE", "https://www.bloomberg.com")

# Track which paths are being translated in the background
_in_progress: set[str] = set()
_ready: set[str] = set()


# ── Pass-through for Bloomberg's own resources ─────────────────────────────

_PASSTHROUGH_PREFIXES = (
    "_next/", "api/", "favicon", "robots.txt",
    "sitemap", "manifest", ".well-known",
)

_PASSTHROUGH_EXTENSIONS = (
    ".js", ".css", ".png", ".jpg", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".map", ".json",
)


def _is_passthrough(path: str) -> bool:
    for pfx in _PASSTHROUGH_PREFIXES:
        if path.startswith(pfx):
            return True
    for ext in _PASSTHROUGH_EXTENSIONS:
        if path.endswith(ext):
            return True
    return False


# ── Health & API endpoints ──────────────────────────────────────────────────

@app.get("/health")
async def health():
    import cache
    return {"status": "ok", "cache": cache.stats()}


@app.get("/api/ready")
async def api_ready(path: str):
    return {"ready": path in _ready}


@app.get("/api/cache/stats")
async def cache_stats():
    import cache
    return cache.stats()


@app.post("/api/cache/clear")
async def cache_clear():
    import cache
    n = cache.clear()
    _ready.clear()
    return {"cleared": n}


@app.post("/api/translate-html")
async def translate_html(request: Request):
    """Принимает HTML страницы Bloomberg от bookmarklet, переводит и возвращает."""
    import text_nodes as tn
    import link_rewriter as lr
    import translator as tr

    body = await request.json()
    raw_html: str = body.get("html", "")
    path: str = body.get("path", "")
    lang: str = body.get("lang", TARGET_LANG)

    if not raw_html or len(raw_html) < 1000:
        return JSONResponse({"error": "empty html"}, status_code=400)

    marked_html, nodes = tn.extract(raw_html)
    if not nodes:
        return HTMLResponse(content=raw_html)

    batches = tn.make_batches(nodes, max_chars=4000)
    translations = await tr.translate_all_batches(batches, lang)
    translated = tn.apply(marked_html, translations)
    rewritten = lr.rewrite(translated)
    final = lr.inject_header_bar(rewritten, path, lang, cache_age="только что (bookmarklet)")

    return HTMLResponse(content=final)


# ── Background translation task ─────────────────────────────────────────────

async def _translate_in_background(path: str, lang: str) -> None:
    try:
        from proxy import handle_bloomberg_path
        await handle_bloomberg_path(path, lang)
        _ready.add(path)
    except Exception:
        pass
    finally:
        _in_progress.discard(path)


# ── Main catchall proxy route ───────────────────────────────────────────────

LOADING_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="2">
<title>Bloomberg Translator — Переводим...</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0a0e1a;color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       min-height:100vh;display:flex;flex-direction:column;align-items:center;
       justify-content:center;gap:24px;padding:24px;text-align:center}}
  .logo{{font-size:22px;font-weight:700;color:#3b82f6;letter-spacing:-.5px}}
  .spinner{{width:52px;height:52px;border:4px solid #1e293b;border-top-color:#3b82f6;
            border-radius:50%;animation:s 1s linear infinite}}
  @keyframes s{{to{{transform:rotate(360deg)}}}}
  p{{color:#94a3b8;font-size:15px;line-height:1.6;max-width:280px}}
  .steps{{display:flex;flex-direction:column;gap:8px;width:100%;max-width:260px}}
  .step{{background:#111827;border:1px solid #1e2d40;border-radius:8px;
         padding:10px 14px;font-size:13px;color:#64748b;text-align:left;
         display:flex;align-items:center;gap:10px}}
  .step.active{{border-color:#3b82f6;color:#93c5fd}}
  .step.active::before{{content:"⟳";animation:s .8s linear infinite;display:inline-block}}
  .step:not(.active)::before{{content:"✓";color:#10b981}}
</style>
</head>
<body>
<div class="logo">Bloomberg Translator</div>
<div class="spinner"></div>
<p>Загружаем страницу Bloomberg<br>и переводим на русский...</p>
<div class="steps">
  <div class="step">Получаем страницу с Bloomberg</div>
  <div class="step active">Переводим текст через Claude AI</div>
  <div class="step" style="opacity:.4">Отдаём вам готовый результат</div>
</div>
<p style="font-size:12px;opacity:.4">Страница обновится автоматически</p>
</body>
</html>"""


# ── Dashboard root (MUST be before /{path:path}) ────────────────────────────

_SKELETON_HTML = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8">
<meta http-equiv="refresh" content="3">
<title>Bloomberg RU — Загрузка...</title>
<style>
  body{background:#070b14;color:#e2e8f0;font-family:-apple-system,sans-serif;
  display:flex;align-items:center;justify-content:center;min-height:100vh;
  flex-direction:column;gap:20px;text-align:center}
  .sp{width:48px;height:48px;border:4px solid #1e293b;border-top-color:#3b82f6;
  border-radius:50%;animation:s 1s linear infinite}
  @keyframes s{to{transform:rotate(360deg)}}
</style></head>
<body>
<div style="font-size:20px;font-weight:800;color:#3b82f6">Bloomberg RU</div>
<div class="sp"></div>
<div>Загружаем данные рынка и переводим новости...</div>
<div style="font-size:13px;opacity:.5">Страница обновится автоматически через 3 сек</div>
</body></html>"""


@app.get("/")
async def root():
    async with _DASH_LOCK:
        html = _DASH_HTML
    if html:
        return HTMLResponse(content=html)
    return HTMLResponse(content=_SKELETON_HTML)


@app.get("/api/market")
async def api_market():
    from dashboard import get_market_data
    return JSONResponse(await get_market_data())


@app.get("/api/market/history")
async def api_market_history(symbol: str, period: str = "1mo"):
    """Return % change for a ticker over 1wk / 1mo / 1y."""
    import asyncio
    allowed = {"1wk", "1mo", "1y"}
    if period not in allowed:
        period = "1mo"

    def _fetch():
        import yfinance as yf
        import math
        t = yf.Ticker(symbol)
        hist = t.history(period=period)
        if hist.empty:
            return {"change": None}
        first = hist["Close"].iloc[0]
        last = hist["Close"].iloc[-1]
        if not first or math.isnan(first) or math.isnan(last):
            return {"change": None}
        return {"change": round((last - first) / first * 100, 2), "first": round(first, 4), "last": round(last, 4)}

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _fetch)
    return JSONResponse(result)


_QUOTE_CACHE: dict = {}
_QUOTE_TTL = 30

@app.get("/api/quote")
async def api_quote(symbol: str = "SPY"):
    """Сводка по тикеру: дн. диапазон, 52-нед макс/мин, объём, капитализация."""
    import time, math
    now = time.time()
    hit = _QUOTE_CACHE.get(symbol)
    if hit and hit[1] + _QUOTE_TTL > now:
        return JSONResponse(hit[0])

    def _num(x):
        try:
            x = float(x)
            return None if (math.isnan(x) or math.isinf(x)) else x
        except Exception:
            return None

    def _fetch():
        import yfinance as yf
        fi = yf.Ticker(symbol).fast_info
        return {
            "symbol": symbol,
            "price": _num(getattr(fi, "last_price", None)),
            "prev": _num(getattr(fi, "previous_close", None)),
            "day_high": _num(getattr(fi, "day_high", None)),
            "day_low": _num(getattr(fi, "day_low", None)),
            "year_high": _num(getattr(fi, "year_high", None)),
            "year_low": _num(getattr(fi, "year_low", None)),
            "volume": _num(getattr(fi, "last_volume", None)),
            "market_cap": _num(getattr(fi, "market_cap", None)),
            "currency": getattr(fi, "currency", None) or "USD",
        }

    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(None, _fetch)
        if data and data.get("price"):
            _QUOTE_CACHE[symbol] = (data, now)
        return JSONResponse(data)
    except Exception as e:
        if hit:
            return JSONResponse(hit[0])
        return JSONResponse({"symbol": symbol, "error": str(e)})


_OHLCV_CACHE: dict = {}
_OHLCV_TTL = 60  # свечи кэшируем 60с: одни и те же графики просят многие зрители

@app.get("/api/ohlcv")
async def api_ohlcv(symbol: str = "SPY", period: str = "1mo", interval: str = "1d"):
    import time
    allowed_p = {"1d","5d","1mo","3mo","6mo","1y","5y","max"}
    allowed_i = {"1m","2m","5m","15m","30m","60m","1h","1d","1wk","1mo"}
    if period not in allowed_p: period = "1mo"
    if interval not in allowed_i: interval = "1d"

    ckey = f"{symbol}|{period}|{interval}"
    now = time.time()
    hit = _OHLCV_CACHE.get(ckey)
    if hit and hit[1] + _OHLCV_TTL > now:
        return JSONResponse(hit[0])

    def _fetch():
        import yfinance as yf, math
        hist = yf.Ticker(symbol).history(period=period, interval=interval)
        if hist.empty:
            return []
        out = []
        for dt, row in hist.iterrows():
            t = dt.strftime("%Y-%m-%d") if interval in ("1d","1wk","1mo") else int(dt.timestamp())
            o,h,l,c = float(row["Open"]),float(row["High"]),float(row["Low"]),float(row["Close"])
            if any(math.isnan(x) or math.isinf(x) for x in [o,h,l,c]):
                continue
            try:
                v = float(row["Volume"])
                v = 0 if math.isnan(v) or math.isinf(v) else int(v)
            except Exception:
                v = 0
            out.append({"time":t,"open":round(o,4),"high":round(h,4),"low":round(l,4),"close":round(c,4),"volume":v})
        return out

    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(None, _fetch)
        if data:
            _OHLCV_CACHE[ckey] = (data, now)
        return JSONResponse(data)
    except Exception as e:
        if hit:  # отдаём устаревшие свечи при сбое
            return JSONResponse(hit[0])
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/ai-brief")
async def api_ai_brief():
    from dashboard import get_market_data
    market = await get_market_data()

    def _call():
        import groq
        key = os.getenv("GROQ_API_KEY")
        if not key:
            return "Настройте GROQ_API_KEY для AI-анализа."
        summary = "\n".join(
            f"{m['name']}: {m['price']} ({'+'if m['change']>=0 else ''}{m['change']}%)"
            for m in market[:16]
        )
        msg = groq.Groq(api_key=key).chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=280,
            messages=[{"role":"user","content":
                f"Дай краткий профессиональный анализ текущей рыночной ситуации в 3-4 предложениях на русском языке.\n"
                f"Данные:\n{summary}\n"
                f"Пиши как Bloomberg-аналитик: конкретно, без воды, с акцентом на ключевые движущие факторы. Без заголовков."}]
        )
        return msg.choices[0].message.content

    loop = asyncio.get_event_loop()
    try:
        return JSONResponse({"brief": await loop.run_in_executor(None, _call)})
    except Exception:
        return JSONResponse({"brief": "AI-анализ временно недоступен"})


@app.get("/api/news")
async def api_news():
    from dashboard import get_translated_news
    return JSONResponse(await get_translated_news(TARGET_LANG))


_LIVE_ID_CACHE: dict = {}

@app.get("/api/live-id")
async def api_live_id():
    """Return current Bloomberg TV YouTube live stream video ID."""
    import asyncio, re, time
    now = time.time()
    if _LIVE_ID_CACHE.get("ts", 0) + 1800 > now and _LIVE_ID_CACHE.get("id"):
        return JSONResponse({"id": _LIVE_ID_CACHE["id"]})

    def _fetch():
        import httpx
        for url in [
            "https://www.youtube.com/channel/UCIALMKvObZNtJ6AmdCLP7Lg/live",
            "https://www.youtube.com/@BloombergTV/live",
        ]:
            try:
                r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10, follow_redirects=True)
                m = re.search(r'watch\?v=([A-Za-z0-9_-]{11})', r.text)
                if m:
                    return m.group(1)
            except Exception:
                continue
        return None

    loop = asyncio.get_event_loop()
    vid = await loop.run_in_executor(None, _fetch)
    if vid:
        _LIVE_ID_CACHE["id"] = vid
        _LIVE_ID_CACHE["ts"] = now
    return JSONResponse({"id": vid or _LIVE_ID_CACHE.get("id", "iEpJwprxDdk")})


_HLS_CACHE: dict = {}
# Кэш проксированных плейлистов: ключ=src → (текст, ts). Живой HLS обновляется ~5с,
# поэтому 2с кэша достаточно и снимает нагрузку с googlevideo при множестве зрителей.
_PL_CACHE: dict = {}
_PL_TTL = 2.0
_PL_LOCKS: dict = {}

async def _get_upstream_hls() -> str:
    """Resolve the googlevideo HLS manifest URL for Bloomberg TV (cached 30 min)."""
    import time
    now = time.time()
    if _HLS_CACHE.get("ts", 0) + 1800 > now and _HLS_CACHE.get("url"):
        return _HLS_CACHE["url"]

    vid = _LIVE_ID_CACHE.get("id", "iEpJwprxDdk")

    def _fetch():
        import subprocess, os, shutil
        ytdlp = (
            os.path.join(os.path.dirname(__file__), ".venv", "bin", "yt-dlp")
            if os.path.exists(os.path.join(os.path.dirname(__file__), ".venv", "bin", "yt-dlp"))
            else shutil.which("yt-dlp") or "yt-dlp"
        )
        result = subprocess.run(
            [ytdlp, "--no-warnings", "--get-url", f"https://www.youtube.com/watch?v={vid}"],
            capture_output=True, text=True, timeout=40,
        )
        lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip() and "manifest" in l]
        return lines[0] if lines else None

    loop = asyncio.get_event_loop()
    url = await loop.run_in_executor(None, _fetch)
    if url:
        _HLS_CACHE["url"] = url
        _HLS_CACHE["ts"] = now
    return url or _HLS_CACHE.get("url", "")


@app.get("/api/live-hls")
async def api_live_hls():
    """Return a SAME-ORIGIN proxied HLS URL so hls.js can play it (googlevideo lacks CORS)."""
    upstream = await _get_upstream_hls()
    if not upstream:
        return JSONResponse({"url": ""})
    # Point the browser at our proxy, which adds CORS and fetches from the server IP.
    return JSONResponse({"url": "/api/hls/index.m3u8"})


def _b64u(s: str) -> str:
    import base64
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _unb64u(s: str) -> str:
    import base64
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad).decode()


def _rewrite_playlist(text: str, upstream: str) -> str:
    """Перепишем URL сегментов/вложенных плейлистов через наш прокси."""
    from urllib.parse import urljoin
    out_lines = []
    prev_tag = ""
    for line in text.splitlines():
        s = line.strip()
        if not s:
            out_lines.append(line)
            continue
        if s.startswith("#"):
            out_lines.append(line)
            prev_tag = s
            continue
        abs_url = urljoin(upstream, s)
        if "STREAM-INF" in prev_tag or abs_url.rstrip().endswith(".m3u8"):
            out_lines.append(f"/api/hls/index.m3u8?src={_b64u(abs_url)}")
        else:
            out_lines.append(f"/api/hls/seg?u={_b64u(abs_url)}")
        prev_tag = ""
    return "\n".join(out_lines) + "\n"


@app.get("/api/hls/index.m3u8")
async def api_hls_playlist(src: str = ""):
    """Прокси HLS-плейлиста с коротким кэшем, ретраями и stale-fallback (CORS добавляем сами)."""
    import httpx, time

    upstream = _unb64u(src) if src else await _get_upstream_hls()
    if not upstream:
        return JSONResponse({"error": "no stream"}, status_code=503)

    key = src or "_root"
    now = time.time()
    cached = _PL_CACHE.get(key)
    if cached and cached[1] + _PL_TTL > now:
        return StreamingResponse(
            iter([cached[0].encode()]),
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
        )

    # один апстрим-фетч на ключ за раз — остальные конкурентные запросы ждут лок и берут кэш
    lock = _PL_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _PL_CACHE.get(key)
        if cached and cached[1] + _PL_TTL > time.time():
            body = cached[0]
        else:
            body = None
            last_err = ""
            for attempt in range(3):
                try:
                    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as c:
                        r = await c.get(upstream, headers={"User-Agent": "Mozilla/5.0"})
                        if r.status_code == 200 and r.text.startswith("#EXTM3U"):
                            body = _rewrite_playlist(r.text, upstream)
                            _PL_CACHE[key] = (body, time.time())
                            break
                        last_err = f"upstream status {r.status_code}"
                except Exception as e:
                    last_err = f"{type(e).__name__}: {e}"
                await asyncio.sleep(0.3 * (attempt + 1))
            if body is None:
                # отдаём устаревший плейлист, если есть, чтобы плеер не падал
                if cached:
                    body = cached[0]
                else:
                    return JSONResponse({"error": last_err or "upstream failed"}, status_code=502)

    return StreamingResponse(
        iter([body.encode()]),
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
    )


@app.get("/api/hls/seg")
async def api_hls_segment(u: str):
    """Proxy a single HLS media segment from googlevideo (server IP matches the URL's IP lock)."""
    import httpx
    seg_url = _unb64u(u)

    async def _stream():
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            async with c.stream("GET", seg_url, headers={"User-Agent": "Mozilla/5.0"}) as r:
                async for chunk in r.aiter_bytes(65536):
                    yield chunk

    return StreamingResponse(
        _stream(),
        media_type="video/mp2t",
        headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
    )


@app.get("/api/translate-text")
async def api_translate_text(text: str, lang: str = "ru"):
    """Translate a single text string — used by live transcription."""
    import asyncio
    from deep_translator import GoogleTranslator

    def _tr():
        return GoogleTranslator(source="auto", target=lang).translate(text) or text

    loop = asyncio.get_event_loop()
    translated = await loop.run_in_executor(None, _tr)
    return JSONResponse({"translated": translated})


_FG_CACHE: dict = {}

@app.get("/api/fear-greed")
async def api_fear_greed():
    import time, asyncio, httpx
    now = time.time()
    if _FG_CACHE.get("ts", 0) + 3600 > now:
        return JSONResponse(_FG_CACHE["data"])

    def _fetch():
        try:
            r = httpx.get("https://api.alternative.me/fng/", timeout=8)
            d = r.json()["data"][0]
            return {
                "value": int(d["value"]),
                "label": d["value_classification"],
                "source": "crypto",
            }
        except Exception:
            return None

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _fetch)
    if result:
        _FG_CACHE["data"] = result
        _FG_CACHE["ts"] = now
    return JSONResponse(result or _FG_CACHE.get("data", {"value": 50, "label": "Neutral", "source": "cached"}))


# ── Live dubbing: SSE endpoint ─────────────────────────────────────────────

_dub_subscribers: list[asyncio.Queue] = []
_dub_task: asyncio.Task | None = None


async def _dub_callback(ru_text: str) -> None:
    for q in list(_dub_subscribers):
        try:
            q.put_nowait({"text": ru_text})
        except asyncio.QueueFull:
            pass


@app.get("/api/live-transcript")
async def live_transcript():
    """SSE: stream Russian translations of Bloomberg TV audio."""
    global _dub_task

    vid = _LIVE_ID_CACHE.get("id", "iEpJwprxDdk")

    queue: asyncio.Queue = asyncio.Queue(maxsize=20)
    _dub_subscribers.append(queue)

    if _dub_task is None or _dub_task.done():
        try:
            from live_audio import live_dubbing_loop
            _dub_task = asyncio.create_task(live_dubbing_loop(vid, _dub_callback))
        except Exception:
            _dub_task = None

    async def event_stream():
        try:
            yield 'data: {"text":null}\n\n'
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=25)
                    yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            if queue in _dub_subscribers:
                _dub_subscribers.remove(queue)
            if not _dub_subscribers and _dub_task and not _dub_task.done():
                _dub_task.cancel()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Catch-all proxy (MUST be last) ──────────────────────────────────────────

@app.get("/{path:path}")
async def proxy_handler(
    path: str,
    request: Request,
    background_tasks: BackgroundTasks,
    _translated: str = "",
):
    lang = TARGET_LANG

    if _is_passthrough(path):
        return RedirectResponse(f"{BLOOMBERG_BASE}/{path}", status_code=302)

    import cache as cache_mod
    cached = cache_mod.get_page(path, lang)
    if cached:
        return HTMLResponse(content=cached, status_code=200)

    if path in _ready:
        _ready.discard(path)
        cached = cache_mod.get_page(path, lang)
        if cached:
            return HTMLResponse(content=cached, status_code=200)

    if path not in _in_progress:
        _in_progress.add(path)
        background_tasks.add_task(_translate_in_background, path, lang)

    return HTMLResponse(content=LOADING_HTML, status_code=200)


def _market_color(change: float) -> str:
    return "#22d47a" if change >= 0 else "#ff4466"


def _render_ticker_items(market: list) -> str:
    items = ""
    for m in market:
        clr = _market_color(m["change"])
        sign = "+" if m["change"] >= 0 else ""
        p = m["price"]
        price_str = f"${p:,.2f}" if p > 100 else f"{p:.4f}" if p < 1 else f"{p:,.2f}"
        items += (
            f'<div class="tick">'
            f'<span class="tname">{m["name"]}</span>'
            f'<span class="tprice">{price_str}</span>'
            f'<span class="tch" style="color:{clr}">{sign}{m["change"]}%</span>'
            f'</div>'
        )
    return items


_LIVE_WIDGET = """
<div class="live-widget" id="live-widget">
  <div class="lw-header">
    <div class="lw-badge"><span class="live-dot"></span>ПРЯМОЙ ЭФИР</div>
    <div class="lw-title">Bloomberg Television</div>
    <span class="lw-delay-badge">задержка ~4 сек</span>
    <button class="lw-close" onclick="document.getElementById('live-widget').style.display='none'" title="Скрыть">✕</button>
  </div>
  <div class="lw-body">
    <div class="lw-video-wrap">
      <video id="bloomberg-video" controls autoplay muted playsinline
             style="position:absolute;top:0;left:0;width:100%;height:100%;background:#000;border:0"></video>
    </div>
    <div class="lw-transcript">
      <div class="lw-tr-header">
        <span>Перевод в реальном времени</span>
        <span class="lw-tr-badge" id="tr-status">●</span>
      </div>
      <div class="lw-tr-body" id="tr-body">
        <div class="tr-line tr-dim">Нажмите «Включить озвучку» — через ~4 сек начнётся русский перевод голосом.</div>
      </div>
      <div class="lw-tr-footer">
        <button class="lw-dub-btn" id="dub-btn" onclick="toggleDubbing()">▶ Включить озвучку RU</button>
        <div class="lw-tr-hint" id="tr-hint">Задержка ≤5 сек • Whisper + Google Translate</div>
      </div>
    </div>
  </div>
</div>"""




def _render_news_html(news: list) -> str:
    cats = {}
    for a in news:
        cats.setdefault(a["cat"], []).append(a)
    html = ""
    for cat, articles in cats.items():
        html += f'<div class="nc-group">{cat}</div><div class="nc-grid">'
        for a in articles[:6]:
            title = a.get("title_ru") or a["title"]
            desc = (a.get("desc_ru") or a.get("desc", ""))[:130]
            link = a.get("link", "#")
            img = a.get("img", "")
            img_html = (
                f'<div class="nc-img" style="background-image:url({img})"></div>'
                if img else '<div class="nc-img nc-img-ph"></div>'
            )
            html += (
                f'<a class="nc" href="{link}" target="_blank" rel="noopener">'
                f'{img_html}'
                f'<div class="nc-body">'
                f'<div class="nc-cat">{cat}</div>'
                f'<div class="nc-title">{title}</div>'
                f'{"<div class=nc-desc>" + desc + "</div>" if desc else ""}'
                f'</div></a>'
            )
        html += '</div>'
    return html or '<div class="nc-empty">Загрузка новостей...</div>'


def _render_dashboard(market: list, news: list) -> str:
    ticker_items = _render_ticker_items(market)
    news_html = _render_news_html(news)

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bloomberg Terminal · RU</title>
<style>
:root{{
  --bg:#060c18;--sf:#0c1624;--sf2:#111e33;--bd:#1a2840;
  --amber:#f59e0b;--amber2:#fbbf24;
  --green:#22d47a;--red:#ff4466;
  --text:#dde6f0;--muted:#4a6080;--dim:#1a2840;
  --mono:'SF Mono','Courier New',monospace;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
html,body{{height:100%}}
body{{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px;overflow-x:hidden}}

/* HEADER */
.hdr{{background:#030810;border-bottom:2px solid var(--amber);display:flex;align-items:center;gap:12px;padding:0 14px;height:46px;position:sticky;top:0;z-index:500}}
.logo{{font-size:17px;font-weight:900;letter-spacing:-1px;color:#fff;white-space:nowrap;flex-shrink:0;display:flex;align-items:center;gap:6px}}
.logo-b{{color:var(--amber)}}
.logo-ru{{background:var(--amber);color:#000;padding:1px 6px;border-radius:3px;font-size:10px;font-weight:900;letter-spacing:1px}}
.hdr-idx{{display:flex;flex:1;overflow:hidden;border-left:1px solid var(--bd);margin-left:8px}}
.hdr-item{{display:flex;align-items:center;gap:5px;padding:0 12px;border-right:1px solid var(--bd);font-size:11px;white-space:nowrap;cursor:pointer;transition:background .12s;height:46px}}
.hdr-item:hover{{background:rgba(245,158,11,.07)}}
.hdr-name{{color:var(--muted);font-weight:600;font-size:9px;letter-spacing:.5px;text-transform:uppercase}}
.hdr-val{{font-weight:800;font-variant-numeric:tabular-nums;font-family:var(--mono)}}
.hdr-ch{{font-size:10px;font-weight:700;font-family:var(--mono)}}
.hdr-right{{margin-left:auto;display:flex;align-items:center;gap:10px;flex-shrink:0}}
.live-dot{{width:7px;height:7px;border-radius:50%;background:var(--green);animation:blink 2s ease-in-out infinite}}
@keyframes blink{{0%,100%{{opacity:1}}50%{{opacity:.25}}}}
.live-txt{{font-size:9px;font-weight:900;color:var(--green);letter-spacing:1.5px}}
.hdr-clock{{font-family:var(--mono);font-size:11px;color:var(--muted)}}
.sess-badge{{display:flex;align-items:center;gap:5px;font-size:10px;font-weight:700;padding:3px 8px;border:1px solid var(--bd);border-radius:5px;white-space:nowrap}}
.sess-dot{{width:6px;height:6px;border-radius:50%;background:var(--muted);flex-shrink:0}}
.sess-dot.open{{background:var(--green);box-shadow:0 0 6px var(--green)}}
.sess-dot.closed{{background:var(--red)}}
.sess-dot.pre{{background:var(--amber)}}

/* TICKER */
.ticker{{background:#030810;border-bottom:1px solid var(--bd);height:32px;overflow:hidden;display:flex;align-items:center;position:relative}}
.ticker::before,.ticker::after{{content:'';position:absolute;top:0;bottom:0;width:32px;z-index:10;pointer-events:none}}
.ticker::before{{left:0;background:linear-gradient(90deg,#030810,transparent)}}
.ticker::after{{right:0;background:linear-gradient(-90deg,#030810,transparent)}}
.ticker-inner{{display:flex;animation:scroll 150s linear infinite;will-change:transform;white-space:nowrap}}
.ticker-inner:hover{{animation-play-state:paused}}
@keyframes scroll{{from{{transform:translateX(0)}}to{{transform:translateX(-50%)}}}}
.tick{{display:inline-flex;align-items:center;gap:5px;padding:0 14px;border-right:1px solid var(--bd);height:32px}}
.tn{{color:var(--muted);font-weight:700;font-size:9px;letter-spacing:.3px}}
.tp{{font-weight:800;font-family:var(--mono);font-size:11px;font-variant-numeric:tabular-nums}}
.tc{{font-size:9px;font-weight:700;font-family:var(--mono)}}

/* PANEL HEADER */
.panel-hdr{{display:flex;align-items:center;gap:7px;padding:7px 12px;border-bottom:1px solid var(--bd);background:rgba(245,158,11,.04);flex-shrink:0}}
.panel-title{{font-size:9px;font-weight:900;letter-spacing:2px;color:var(--amber);text-transform:uppercase}}

/* MAIN GRID */
.main{{display:grid;grid-template-columns:370px 1fr 255px;gap:8px;padding:8px;align-items:start}}

/* TV PANEL */
.tv-panel{{display:flex;flex-direction:column;background:var(--sf);border:1px solid var(--bd);border-radius:8px;overflow:hidden}}
.tv-wrap{{position:relative;padding-top:56.25%;background:#000;flex-shrink:0}}
.tv-wrap video{{position:absolute;top:0;left:0;width:100%;height:100%;border:0;background:#000}}
.tr-panel{{display:flex;flex-direction:column;max-height:240px}}
.tr-header{{display:flex;align-items:center;justify-content:space-between;padding:7px 12px;border-bottom:1px solid var(--bd);border-top:1px solid var(--bd);font-size:9px;font-weight:800;color:var(--muted);letter-spacing:1px;text-transform:uppercase}}
.tr-live{{color:var(--green);animation:blink 2s ease-in-out infinite}}
.tr-body{{flex:1;overflow-y:auto;padding:8px 12px;display:flex;flex-direction:column;gap:4px;scrollbar-width:thin;scrollbar-color:var(--bd) transparent}}
.tr-line{{font-size:12px;line-height:1.55;color:var(--text);padding:5px 9px;border-radius:5px;background:var(--sf2);border-left:3px solid var(--amber)}}
.tr-line.tr-dim{{color:var(--muted);background:none;border-left-color:var(--bd);font-size:11px}}
.tr-footer{{padding:7px 12px;border-top:1px solid var(--bd);display:flex;align-items:center;gap:8px;flex-shrink:0}}
.dub-btn{{font-size:10px;font-weight:800;padding:5px 11px;border-radius:5px;border:1px solid var(--amber);color:var(--amber);background:none;cursor:pointer;transition:all .15s;white-space:nowrap;letter-spacing:.3px}}
.dub-btn:hover,.dub-btn.active{{background:var(--amber);color:#000}}
.tr-hint{{font-size:10px;color:var(--muted);flex:1;line-height:1.4}}

/* CHART PANEL */
.chart-panel{{display:flex;flex-direction:column;background:var(--sf);border:1px solid var(--bd);border-radius:8px;overflow:hidden}}
.chart-controls{{display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid var(--bd);flex-wrap:wrap;background:rgba(245,158,11,.03)}}
.sym-wrap{{display:flex;align-items:center;border:1px solid var(--bd);border-radius:5px;overflow:hidden;background:var(--bg)}}
.sym-input{{background:none;border:none;color:var(--text);font-size:12px;padding:5px 9px;width:175px;font-family:var(--mono);outline:none}}
.sym-input::placeholder{{color:var(--muted)}}
.sym-go{{background:var(--amber);border:none;color:#000;font-weight:900;font-size:13px;padding:5px 10px;cursor:pointer;transition:opacity .15s;line-height:1}}
.sym-go:hover{{opacity:.85}}
.pbtn-group{{display:flex;gap:2px}}
.pbtn{{background:none;border:1px solid var(--bd);color:var(--muted);font-size:10px;font-weight:700;padding:4px 8px;border-radius:4px;cursor:pointer;transition:all .12s}}
.pbtn:hover{{border-color:var(--amber);color:var(--amber)}}
.pbtn.active{{background:var(--amber);border-color:var(--amber);color:#000}}
.chart-info{{display:flex;align-items:baseline;gap:10px;padding:5px 12px;border-bottom:1px solid var(--bd);background:var(--sf2);flex-wrap:wrap;flex-shrink:0}}
.ci-sym{{font-size:13px;font-weight:800}}
.ci-price{{font-size:22px;font-weight:900;font-family:var(--mono);font-variant-numeric:tabular-nums}}
.ci-change{{font-size:12px;font-weight:700;font-family:var(--mono)}}
.ci-period{{font-size:10px;color:var(--muted);margin-left:auto}}
.chart-stats{{display:flex;gap:0;flex-wrap:wrap;padding:0 4px;border-bottom:1px solid var(--bd);background:var(--sf);min-height:0}}
.cs-item{{display:flex;flex-direction:column;gap:1px;padding:5px 12px;border-right:1px solid var(--bd)}}
.cs-lbl{{font-size:8px;font-weight:800;color:var(--muted);letter-spacing:.6px;text-transform:uppercase}}
.cs-val{{font-size:11px;font-weight:700;font-family:var(--mono);color:var(--text)}}
#chart-container{{min-height:440px;flex:1}}

/* MARKETS PANEL */
.mkt-panel{{background:var(--sf);border:1px solid var(--bd);border-radius:8px;overflow:hidden;display:flex;flex-direction:column}}
.mkt-group{{font-size:8px;font-weight:900;letter-spacing:2.5px;color:var(--muted);text-transform:uppercase;padding:7px 12px 3px;border-top:1px solid var(--bd)}}
.mkt-group:first-child{{border-top:none}}
.mkt-row{{display:flex;align-items:center;padding:4px 12px;border-bottom:1px solid rgba(26,40,64,.4);cursor:pointer;transition:background .1s}}
.mkt-row:hover{{background:rgba(245,158,11,.05)}}
.mkt-name{{flex:1;font-size:11px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.mkt-price{{font-family:var(--mono);font-size:10px;font-weight:700;font-variant-numeric:tabular-nums;min-width:70px;text-align:right}}
.mkt-ch{{font-family:var(--mono);font-size:10px;font-weight:700;min-width:56px;text-align:right}}
.star{{flex-shrink:0;width:14px;text-align:center;font-size:11px;color:var(--dim);cursor:pointer;margin-right:5px;transition:color .12s;user-select:none}}
.star:hover{{color:var(--muted)}}
.star.on{{color:var(--amber)}}
#mkt-watch:empty::after{{content:'нажми ★ у инструмента';display:block;font-size:9px;color:var(--muted);padding:4px 12px 6px;font-style:italic}}

/* BOTTOM GRID */
.bottom{{display:grid;grid-template-columns:230px 1fr 1fr;gap:8px;padding:0 8px 8px}}

/* FEAR & GREED */
.fg-card{{background:var(--sf);border:1px solid var(--bd);border-radius:8px;overflow:hidden}}
.fg-body{{padding:12px;display:flex;flex-direction:column;gap:10px;align-items:center}}
.fg-gauge-wrap{{display:flex;flex-direction:column;align-items:center;gap:4px}}
.fg-val{{font-size:52px;font-weight:900;font-family:var(--mono);line-height:1}}
.fg-lbl{{font-size:11px;font-weight:700;letter-spacing:.5px}}
.breadth-row{{width:100%;display:flex;flex-direction:column;gap:4px}}
.breadth-label{{font-size:9px;font-weight:800;color:var(--muted);letter-spacing:1px;text-transform:uppercase}}
.breadth-bg{{height:4px;background:var(--bd);border-radius:2px;overflow:hidden}}
.breadth-fill{{height:100%;border-radius:2px;transition:width .5s ease}}
.breadth-txt{{font-size:10px;font-weight:700}}

/* MOVERS */
.movers-card{{background:var(--sf);border:1px solid var(--bd);border-radius:8px;overflow:hidden;display:flex;flex-direction:column}}
.movers-tabs{{display:flex;border-bottom:1px solid var(--bd)}}
.mtab{{flex:1;padding:7px;font-size:10px;font-weight:800;border:none;background:none;color:var(--muted);cursor:pointer;transition:all .12s;border-bottom:2px solid transparent;margin-bottom:-1px;letter-spacing:.3px}}
.mtab.active{{color:var(--amber);border-bottom-color:var(--amber)}}
.mover-row{{display:flex;align-items:center;padding:6px 12px;border-bottom:1px solid rgba(26,40,64,.4);cursor:pointer;transition:background .1s}}
.mover-row:hover{{background:rgba(245,158,11,.05)}}
.mover-name{{font-size:11px;font-weight:700;flex:1;color:var(--text)}}
.mover-price{{font-family:var(--mono);font-size:10px;font-weight:700;min-width:60px;text-align:right;font-variant-numeric:tabular-nums}}
.mover-ch{{font-family:var(--mono);font-size:11px;font-weight:800;min-width:56px;text-align:right}}

/* AI BRIEF */
.ai-card{{background:var(--sf);border:1px solid var(--bd);border-radius:8px;overflow:hidden;display:flex;flex-direction:column}}
.ai-hdr{{display:flex;align-items:center;justify-content:space-between;padding:8px 12px;border-bottom:1px solid var(--bd);background:rgba(245,158,11,.04)}}
.ai-refresh{{cursor:pointer;color:var(--amber);font-size:15px;transition:transform .5s;user-select:none}}
.ai-refresh:hover{{transform:rotate(180deg)}}
.ai-body{{padding:12px 14px;font-size:12px;line-height:1.75;color:var(--text);flex:1;min-height:120px}}
.ai-body.loading{{color:var(--muted);font-style:italic}}

/* NEWS */
.news-section{{padding:0 8px 24px}}
.nc-group{{font-size:9px;font-weight:900;letter-spacing:2.5px;color:var(--amber);text-transform:uppercase;padding:14px 0 6px;border-top:1px solid var(--bd);margin-top:6px}}
.nc-group:first-child{{margin-top:0;border-top:none;padding-top:4px}}
.nc-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:10px}}
.nc{{display:flex;flex-direction:column;background:var(--sf);border:1px solid var(--bd);border-radius:7px;overflow:hidden;text-decoration:none;color:inherit;transition:transform .15s,border-color .15s}}
.nc:hover{{transform:translateY(-2px);border-color:var(--amber)}}
.nc-img{{width:100%;height:144px;background-size:cover;background-position:center;background-color:var(--sf2)}}
.nc-img-ph{{background:linear-gradient(135deg,var(--sf2),var(--dim))}}
.nc-body{{padding:10px 12px;display:flex;flex-direction:column;gap:5px}}
.nc-cat{{font-size:8px;font-weight:900;letter-spacing:1.5px;color:var(--amber);text-transform:uppercase}}
.nc-title{{font-size:13px;font-weight:700;line-height:1.45;color:var(--text)}}
.nc-desc{{font-size:11px;color:var(--muted);line-height:1.5}}
.nc-empty{{color:var(--muted);text-align:center;padding:40px;font-size:12px}}

/* RESPONSIVE */
@media(max-width:1200px){{.main{{grid-template-columns:330px 1fr 235px}}}}
@media(max-width:960px){{
  .main{{grid-template-columns:1fr}}
  .bottom{{grid-template-columns:1fr}}
  .hdr-idx{{display:none}}
}}
</style>
</head>
<body>

<!-- HEADER -->
<header class="hdr">
  <div class="logo"><span class="logo-b">B</span>LOOMBERG<span class="logo-ru">RU</span></div>
  <div class="hdr-idx" id="hdr-idx"></div>
  <div class="hdr-right">
    <span class="sess-badge" id="sess-badge" title="Сессия NYSE/NASDAQ">
      <span class="sess-dot" id="sess-dot"></span><span id="sess-txt">—</span>
    </span>
    <span class="live-dot"></span>
    <span class="live-txt">LIVE</span>
    <span class="hdr-clock" id="hdr-clock"></span>
  </div>
</header>

<!-- TICKER -->
<div class="ticker">
  <div class="ticker-inner" id="ticker-inner">{ticker_items}{ticker_items}</div>
</div>

<!-- MAIN GRID -->
<div class="main">

  <!-- LEFT: Bloomberg TV -->
  <div class="tv-panel">
    <div class="panel-hdr">
      <span class="live-dot"></span>
      <span class="panel-title">Bloomberg Television</span>
    </div>
    <div class="tv-wrap">
      <video id="bloomberg-video" controls autoplay muted playsinline></video>
    </div>
    <div class="tr-panel">
      <div class="tr-header">
        <span>RU Перевод</span>
        <span class="tr-live" id="tr-status">●</span>
      </div>
      <div class="tr-body" id="tr-body">
        <div class="tr-line tr-dim">Нажмите «Озвучка RU» — через ~4 сек появится живой перевод голосом.</div>
      </div>
      <div class="tr-footer">
        <button class="dub-btn" id="dub-btn" onclick="toggleDubbing()">▶ Озвучка RU</button>
        <span class="tr-hint" id="tr-hint">Whisper + перевод · ≤5 сек задержки</span>
      </div>
    </div>
  </div>

  <!-- CENTER: Interactive Chart -->
  <div class="chart-panel">
    <div class="chart-controls">
      <div class="sym-wrap">
        <input type="text" id="sym-input" class="sym-input" value="SPY"
               placeholder="SPY, NVDA, BTC-USD, GC=F..."
               onkeydown="if(event.key==='Enter')loadChart()">
        <button class="sym-go" onclick="loadChart()">→</button>
      </div>
      <div class="pbtn-group">
        <button class="pbtn" data-p="1d" data-i="5m" onclick="setPeriod(this)">1Д</button>
        <button class="pbtn" data-p="5d" data-i="30m" onclick="setPeriod(this)">5Д</button>
        <button class="pbtn active" data-p="1mo" data-i="1d" onclick="setPeriod(this)">1М</button>
        <button class="pbtn" data-p="3mo" data-i="1d" onclick="setPeriod(this)">3М</button>
        <button class="pbtn" data-p="6mo" data-i="1d" onclick="setPeriod(this)">6М</button>
        <button class="pbtn" data-p="1y" data-i="1d" onclick="setPeriod(this)">1Г</button>
        <button class="pbtn" data-p="5y" data-i="1wk" onclick="setPeriod(this)">5Г</button>
      </div>
      <div class="pbtn-group" style="margin-left:auto">
        <button class="pbtn active" id="ind-ma" onclick="toggleInd('ma',this)" title="Скользящие средние 20/50">MA</button>
        <button class="pbtn active" id="ind-vol" onclick="toggleInd('vol',this)" title="Объём торгов">Объём</button>
      </div>
    </div>
    <div class="chart-info">
      <span class="ci-sym" id="ci-sym">S&amp;P 500 (SPY)</span>
      <span class="ci-price" id="ci-price">—</span>
      <span class="ci-change" id="ci-change"></span>
      <span class="ci-period" id="ci-period">1 месяц</span>
    </div>
    <div class="chart-stats" id="chart-stats"></div>
    <div id="chart-container" style="min-height:440px;flex:1"></div>
  </div>

  <!-- RIGHT: Market Data -->
  <div class="mkt-panel">
    <div class="mkt-group">★ Избранное</div>
    <div id="mkt-watch"></div>
    <div class="mkt-group">Индексы</div>
    <div id="mkt-indices"></div>
    <div class="mkt-group">Крипто</div>
    <div id="mkt-crypto"></div>
    <div class="mkt-group">Сырьё</div>
    <div id="mkt-comm"></div>
    <div class="mkt-group">Валюты</div>
    <div id="mkt-fx"></div>
    <div class="mkt-group">Акции US</div>
    <div id="mkt-stocks"></div>
  </div>

</div><!-- /main -->

<!-- BOTTOM -->
<div class="bottom">

  <!-- Fear & Greed -->
  <div class="fg-card">
    <div class="panel-hdr">
      <span class="panel-title">Страх &amp; Жадность</span>
    </div>
    <div class="fg-body">
      <div class="fg-gauge-wrap">
        <svg width="130" height="70" viewBox="0 0 130 70">
          <path d="M10 65 A55 55 0 0 1 120 65" stroke="#1a2840" stroke-width="11" stroke-linecap="round" fill="none"/>
          <path id="fg-arc" d="M10 65 A55 55 0 0 1 120 65" stroke="#4a6080" stroke-width="11"
                stroke-linecap="round" fill="none"
                stroke-dasharray="172" stroke-dashoffset="172"
                style="transition:stroke-dashoffset .9s ease,stroke .9s ease"/>
        </svg>
        <div class="fg-val" id="fg-val">—</div>
        <div class="fg-lbl" id="fg-lbl">загрузка...</div>
      </div>
      <div class="breadth-row">
        <div class="breadth-label">Рынки растут</div>
        <div class="breadth-bg"><div class="breadth-fill" id="breadth-bar" style="width:50%;background:var(--green)"></div></div>
        <div class="breadth-txt" id="breadth-txt"></div>
      </div>
    </div>
  </div>

  <!-- Top Movers -->
  <div class="movers-card">
    <div class="panel-hdr"><span class="panel-title">Топ Движение</span></div>
    <div class="movers-tabs">
      <button class="mtab active" id="tab-g" onclick="showMovers('gainers')">▲ Лидеры роста</button>
      <button class="mtab" id="tab-l" onclick="showMovers('losers')">▼ Лидеры падения</button>
    </div>
    <div id="movers-list"></div>
  </div>

  <!-- AI Brief -->
  <div class="ai-card">
    <div class="ai-hdr">
      <span class="panel-title">AI Анализ рынка</span>
      <span class="ai-refresh" onclick="loadAI()" title="Обновить">↻</span>
    </div>
    <div class="ai-body loading" id="ai-body">Загрузка анализа...</div>
  </div>

</div><!-- /bottom -->

<!-- NEWS -->
<div class="news-section">
  <div id="news-main">{news_html}</div>
</div>

<script>
// CLOCK + СТАТУС СЕССИИ NYSE
(function(){{
  var el=document.getElementById('hdr-clock');
  function fmtDur(mins){{
    mins=Math.max(0,Math.round(mins));
    var h=Math.floor(mins/60),m=mins%60;
    return (h>0?h+'ч ':'')+m+'м';
  }}
  function updateSession(){{
    var dot=document.getElementById('sess-dot'),txt=document.getElementById('sess-txt');
    if(!dot||!txt)return;
    var et=new Date(new Date().toLocaleString('en-US',{{timeZone:'America/New_York'}}));
    var dow=et.getDay(),mins=et.getHours()*60+et.getMinutes();
    var OPEN=570,CLOSE=960,PRE=240,AFT=1200; // 9:30 / 16:00 / 4:00 / 20:00
    var wd=dow>=1&&dow<=5;
    dot.className='sess-dot';
    if(wd&&mins>=OPEN&&mins<CLOSE){{
      dot.classList.add('open');txt.textContent='США открыто · до закрытия '+fmtDur(CLOSE-mins);
    }}else if(wd&&mins>=PRE&&mins<OPEN){{
      dot.classList.add('pre');txt.textContent='Пре-маркет · до открытия '+fmtDur(OPEN-mins);
    }}else if(wd&&mins>=CLOSE&&mins<AFT){{
      dot.classList.add('pre');txt.textContent='Пост-маркет · открытие '+fmtDur(24*60-mins+OPEN);
    }}else{{
      dot.classList.add('closed');
      txt.textContent=(dow===0||dow===6)?'США закрыто · выходной':'США закрыто';
    }}
  }}
  setInterval(function(){{
    el.textContent=new Date().toLocaleTimeString('ru-RU',{{timeZone:'Europe/Moscow',hour12:false}})+' МСК';
  }},1000);
  updateSession();setInterval(updateSession,30000);
}})();

// PRICE FORMATTER
function fp(p,name){{
  if(p===null||p===undefined||isNaN(p))return '—';
  if(name&&name.includes('/'))return p.toFixed(4);
  if(p>10000)return '$'+Math.round(p).toLocaleString('ru-RU');
  if(p>1000)return '$'+p.toLocaleString('ru-RU',{{minimumFractionDigits:2,maximumFractionDigits:2}});
  if(p>100)return p.toFixed(2);
  if(p>1)return p.toFixed(4);
  return p.toFixed(6);
}}
function clr(v){{return v>=0?'var(--green)':'var(--red)';}}
function pct(v){{return (v>=0?'+':'')+v.toFixed(2)+'%';}}

// MARKET DATA
var _mkt=[],_moverData={{gainers:[],losers:[]}},_activeTab='gainers';
var INDICES=['S&P 500','NASDAQ','DOW','FTSE 100','DAX','Nikkei'];
var CRYPTO=['Bitcoin','Ethereum','Solana','BNB'];
var COMM=['Gold','Silver','Platinum','Palladium','Copper','Oil (WTI)','Brent','Nat. Gas'];
var FX=['EUR/USD','USD/RUB','GBP/USD','USD/JPY','USD/CNY'];
var STOCKS=['NVIDIA','Apple','Microsoft','Amazon','Alphabet','Meta','Tesla','Berkshire'];

// SYM MAP: display name → Yahoo ticker
var SYM={{
  'S&P 500':'^GSPC','NASDAQ':'^IXIC','DOW':'^DJI','FTSE 100':'^FTSE','DAX':'^GDAXI','Nikkei':'^N225',
  'Bitcoin':'BTC-USD','Ethereum':'ETH-USD','Solana':'SOL-USD','BNB':'BNB-USD',
  'Gold':'GC=F','Silver':'SI=F','Platinum':'PL=F','Palladium':'PA=F',
  'Copper':'HG=F','Oil (WTI)':'CL=F','Brent':'BZ=F','Nat. Gas':'NG=F',
  'EUR/USD':'EURUSD=X','USD/RUB':'RUB=X','GBP/USD':'GBPUSD=X','USD/JPY':'JPY=X','USD/CNY':'CNY=X',
  'NVIDIA':'NVDA','Apple':'AAPL','Microsoft':'MSFT','Amazon':'AMZN',
  'Alphabet':'GOOGL','Meta':'META','Tesla':'TSLA','Berkshire':'BRK-B',
}};

function mktRows(names,data){{
  var d={{}};data.forEach(m=>d[m.name]=m);
  return names.map(n=>{{
    var m=d[n];if(!m)return '';
    return '<div class="mkt-row" data-n="'+n+'">'
      +'<span class="star'+(isWatched(n)?' on':'')+'" data-star="'+n+'">★</span>'
      +'<span class="mkt-name">'+n+'</span>'
      +'<span class="mkt-price">'+fp(m.price,n)+'</span>'
      +'<span class="mkt-ch" style="color:'+clr(m.change)+'">'+pct(m.change)+'</span></div>';
  }}).join('');
}}

function updateHeader(data){{
  var d={{}};data.forEach(m=>d[m.name]=m);
  var show=['S&P 500','NASDAQ','DOW','Bitcoin','Gold','Oil (WTI)'];
  document.getElementById('hdr-idx').innerHTML=show.map(n=>{{
    var m=d[n];if(!m)return '';
    return '<div class="hdr-item" data-n="'+n+'">'
      +'<span class="hdr-name">'+n+'</span>'
      +'<span class="hdr-val">'+fp(m.price,n)+'</span>'
      +'<span class="hdr-ch" style="color:'+clr(m.change)+'">'+pct(m.change)+'</span></div>';
  }}).join('');
}}

function updateTicker(data){{
  var html=data.map(m=>
    '<span class="tick"><span class="tn">'+m.name+'</span>'
    +'<span class="tp">'+fp(m.price,m.name)+'</span>'
    +'<span class="tc" style="color:'+clr(m.change)+'">'+pct(m.change)+'</span></span>'
  ).join('');
  document.getElementById('ticker-inner').innerHTML=html+html;
}}

function updateBreadth(data){{
  var rising=data.filter(m=>m.change>=0).length;
  var p=Math.round(rising/data.length*100);
  var bar=document.getElementById('breadth-bar');
  bar.style.width=p+'%';bar.style.background=p>=50?'var(--green)':'var(--red)';
  var t=document.getElementById('breadth-txt');
  t.textContent=rising+' из '+data.length+' ('+p+'%)';
  t.style.color=p>=50?'var(--green)':'var(--red)';
}}

function updateMovers(data){{
  var all=[...data].sort((a,b)=>b.change-a.change);
  _moverData={{gainers:all.slice(0,8),losers:all.slice(-8).reverse()}};
  showMovers(_activeTab);
}}

function showMovers(tab){{
  _activeTab=tab;
  document.getElementById('tab-g').className='mtab'+(tab==='gainers'?' active':'');
  document.getElementById('tab-l').className='mtab'+(tab==='losers'?' active':'');
  var rows=_moverData[tab]||[];
  document.getElementById('movers-list').innerHTML=rows.map(m=>
    '<div class="mover-row" data-n="'+m.name+'">'
    +'<span class="mover-name">'+m.name+'</span>'
    +'<span class="mover-price">'+fp(m.price,m.name)+'</span>'
    +'<span class="mover-ch" style="color:'+clr(m.change)+'">'+pct(m.change)+'</span></div>'
  ).join('');
}}

// ── WATCHLIST (избранное, хранится в браузере) ──
var _watch=[];
try{{_watch=JSON.parse(localStorage.getItem('bbg_watch')||'[]');}}catch(e){{_watch=[];}}
function isWatched(n){{return _watch.indexOf(n)>=0;}}
function toggleWatch(n){{
  var i=_watch.indexOf(n);
  if(i>=0)_watch.splice(i,1); else _watch.push(n);
  try{{localStorage.setItem('bbg_watch',JSON.stringify(_watch));}}catch(e){{}}
  if(_mkt.length)renderMkt(_mkt);
}}
function renderWatch(data){{
  var el=document.getElementById('mkt-watch');if(!el)return;
  el.innerHTML=_watch.length?mktRows(_watch,data):'';
}}

function renderMkt(data){{
  _mkt=data;
  updateTicker(data);
  updateHeader(data);
  renderWatch(data);
  document.getElementById('mkt-indices').innerHTML=mktRows(INDICES,data);
  document.getElementById('mkt-crypto').innerHTML=mktRows(CRYPTO,data);
  document.getElementById('mkt-comm').innerHTML=mktRows(COMM,data);
  document.getElementById('mkt-fx').innerHTML=mktRows(FX,data);
  document.getElementById('mkt-stocks').innerHTML=mktRows(STOCKS,data);
  updateBreadth(data);
  updateMovers(data);
}}

function refreshMarket(){{
  fetch('/api/market').then(r=>r.json()).then(data=>{{
    if(!data||!data.length)return;
    renderMkt(data);
  }}).catch(function(){{}});
}}
// Fast-retry until first data arrives (cold cache takes ~10s), then settle to 30s polling.
var _mktReady=false,_mktTries=0;
function _pollMarket(){{
  fetch('/api/market').then(r=>r.json()).then(function(data){{
    if(data&&data.length){{
      renderMkt(data);
      if(!_mktReady){{_mktReady=true;setInterval(refreshMarket,30000);}}
    }}else if(!_mktReady&&_mktTries<30){{
      _mktTries++;setTimeout(_pollMarket,2000);
    }}
  }}).catch(function(){{if(!_mktReady&&_mktTries<30){{_mktTries++;setTimeout(_pollMarket,2000);}}}});
}}
_pollMarket();

// Делегированный клик: звезда → избранное, строка с data-n → график.
// (никаких инлайновых onclick со строковыми аргументами — устраняет класс бага с экранированием)
document.addEventListener('click',function(e){{
  var star=e.target.closest('.star');
  if(star){{e.stopPropagation();toggleWatch(star.getAttribute('data-star'));return;}}
  var row=e.target.closest('[data-n]');
  if(row){{clickMkt(row.getAttribute('data-n'));}}
}});

// CLICK MARKET ROW → load chart
function clickMkt(name){{
  var sym=SYM[name]||name;
  _curSym=sym;
  document.getElementById('sym-input').value=sym;
  document.getElementById('ci-sym').textContent=name+' ('+sym+')';
  loadChartData(sym,_curPeriod,_curInterval);
}}

// FEAR & GREED
var FG_COLORS={{'Extreme Fear':'#ef4444','Fear':'#f97316','Neutral':'#eab308','Greed':'#84cc16','Extreme Greed':'#22c55e'}};
var FG_RU={{'Extreme Fear':'Крайний страх','Fear':'Страх','Neutral':'Нейтрально','Greed':'Жадность','Extreme Greed':'Крайняя жадность'}};
function loadFG(){{
  fetch('/api/fear-greed').then(r=>r.json()).then(d=>{{
    if(!d||d.value==null)return;
    var c=FG_COLORS[d.label]||'#94a3b8';
    document.getElementById('fg-val').textContent=d.value;
    document.getElementById('fg-val').style.color=c;
    document.getElementById('fg-lbl').textContent=FG_RU[d.label]||d.label;
    document.getElementById('fg-lbl').style.color=c;
    document.getElementById('fg-arc').style.strokeDashoffset=172-(d.value/100*172);
    document.getElementById('fg-arc').style.stroke=c;
  }}).catch(function(){{}});
}}
loadFG();setInterval(loadFG,3600000);

// AI BRIEF
function loadAI(){{
  var el=document.getElementById('ai-body');
  el.className='ai-body loading';el.textContent='Анализирую рынок...';
  fetch('/api/ai-brief').then(r=>r.json()).then(d=>{{
    el.className='ai-body';el.textContent=d.brief||'Нет данных';
  }}).catch(function(){{el.className='ai-body';el.textContent='AI недоступен';}} );
}}
setTimeout(loadAI,2000);

// LIGHTWEIGHT CHARTS
var _chart=null,_series=null,_volSeries=null,_ma20Series=null,_ma50Series=null;
var _curSym='SPY',_curPeriod='1mo',_curInterval='1d',_lastData=[],_showMA=true,_showVol=true;
var PERIOD_NAMES={{'1d':'1 день','5d':'5 дней','1mo':'1 месяц','3mo':'3 месяца','6mo':'6 месяцев','1y':'1 год','5y':'5 лет'}};

function sma(data,n){{
  var out=[],sum=0;
  for(var i=0;i<data.length;i++){{
    sum+=data[i].close;
    if(i>=n)sum-=data[i-n].close;
    if(i>=n-1)out.push({{time:data[i].time,value:sum/n}});
  }}
  return out;
}}

function initChart(){{
  var c=document.getElementById('chart-container');
  _chart=LightweightCharts.createChart(c,{{
    layout:{{background:{{color:'#0c1624'}},textColor:'#4a6080'}},
    grid:{{vertLines:{{color:'#1a2840'}},horzLines:{{color:'#1a2840'}}}},
    crosshair:{{
      mode:LightweightCharts.CrosshairMode.Normal,
      vertLine:{{color:'#f59e0b',width:1,style:2}},
      horzLine:{{color:'#f59e0b',width:1,style:2}}
    }},
    rightPriceScale:{{borderColor:'#1a2840'}},
    timeScale:{{borderColor:'#1a2840',timeVisible:true,secondsVisible:false}},
    width:c.clientWidth,
    height:Math.max(440,c.clientHeight||440),
  }});
  // объём — гистограмма внизу, на отдельной шкале
  _volSeries=_chart.addHistogramSeries({{priceFormat:{{type:'volume'}},priceScaleId:'vol'}});
  _volSeries.priceScale().applyOptions({{scaleMargins:{{top:0.82,bottom:0}}}});
  // свечи
  _series=_chart.addCandlestickSeries({{
    upColor:'#22d47a',downColor:'#ff4466',
    borderUpColor:'#22d47a',borderDownColor:'#ff4466',
    wickUpColor:'#22d47a',wickDownColor:'#ff4466',
  }});
  // скользящие средние поверх свечей
  _ma20Series=_chart.addLineSeries({{color:'#3b82f6',lineWidth:1,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false}});
  _ma50Series=_chart.addLineSeries({{color:'#f59e0b',lineWidth:1,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false}});
  var ro=new ResizeObserver(function(entries){{
    for(var e of entries){{
      _chart.resize(e.contentRect.width,Math.max(440,e.contentRect.height));
    }}
  }});
  ro.observe(c);
  loadChartData('SPY','1mo','1d');
}}

function applyIndicators(){{
  if(!_lastData.length||!_volSeries)return;
  if(_showVol){{
    _volSeries.setData(_lastData.map(function(d){{
      return {{time:d.time,value:d.volume||0,color:(d.close>=d.open?'rgba(34,212,122,.5)':'rgba(255,68,102,.5)')}};
    }}));
  }}else{{_volSeries.setData([]);}}
  if(_showMA&&_lastData.length>=20){{
    _ma20Series.setData(sma(_lastData,20));
    _ma50Series.setData(_lastData.length>=50?sma(_lastData,50):[]);
  }}else{{_ma20Series.setData([]);_ma50Series.setData([]);}}
}}

function bigNum(v){{
  if(v===null||v===undefined||isNaN(v))return '—';
  var a=Math.abs(v);
  if(a>=1e12)return '$'+(v/1e12).toFixed(2)+'трлн';
  if(a>=1e9)return '$'+(v/1e9).toFixed(2)+'млрд';
  if(a>=1e6)return '$'+(v/1e6).toFixed(2)+'млн';
  if(a>=1e3)return (v/1e3).toFixed(1)+'K';
  return ''+Math.round(v);
}}
function volNum(v){{
  if(v===null||v===undefined||isNaN(v)||v===0)return '—';
  var a=Math.abs(v);
  if(a>=1e9)return (v/1e9).toFixed(2)+'млрд';
  if(a>=1e6)return (v/1e6).toFixed(2)+'млн';
  if(a>=1e3)return (v/1e3).toFixed(1)+'K';
  return ''+Math.round(v);
}}
var _statsQuote={{}},_statsRange=null,_statsSym='';
function renderStats(){{
  var el=document.getElementById('chart-stats');if(!el)return;
  var q=_statsQuote||{{}},r=_statsRange,sym=_statsSym;
  function it(lbl,val){{return '<div class="cs-item"><span class="cs-lbl">'+lbl+'</span><span class="cs-val">'+val+'</span></div>';}}
  var html='';
  // диапазон за период — из данных графика, есть всегда
  if(r)html+=it('Макс/мин ('+r.label+')',fp(r.lo,sym)+' – '+fp(r.hi,sym));
  // дн. диапазон и 52 нед — из quote, если Yahoo отдал
  if(q.day_low!=null&&q.day_high!=null)html+=it('Дн. диапазон',fp(q.day_low,sym)+' – '+fp(q.day_high,sym));
  if(q.year_low!=null&&q.year_high!=null)html+=it('52 нед.',fp(q.year_low,sym)+' – '+fp(q.year_high,sym));
  if(q.volume)html+=it('Объём',volNum(q.volume));
  if(q.market_cap)html+=it('Капитализация',bigNum(q.market_cap));
  if(q.prev!=null)html+=it('Пред. закрытие',fp(q.prev,sym));
  el.innerHTML=html;
}}
function loadQuote(sym){{
  _statsQuote={{}};_statsSym=sym;
  fetch('/api/quote?symbol='+encodeURIComponent(sym)).then(r=>r.json()).then(function(q){{
    _statsQuote=(q&&!q.error)?q:{{}};renderStats();
  }}).catch(function(){{renderStats();}});
}}

function loadChartData(sym,period,interval){{
  if(!_series)return;
  document.getElementById('ci-price').textContent='...';
  document.getElementById('ci-change').textContent='';
  document.getElementById('ci-period').textContent=PERIOD_NAMES[period]||period;
  _statsRange=null;
  loadQuote(sym);
  fetch('/api/ohlcv?symbol='+encodeURIComponent(sym)+'&period='+period+'&interval='+interval)
    .then(r=>r.json())
    .then(function(data){{
      if(!Array.isArray(data)||!data.length){{document.getElementById('ci-price').textContent='нет данных';return;}}
      _lastData=data;
      _series.setData(data.map(function(d){{return {{time:d.time,open:d.open,high:d.high,low:d.low,close:d.close}};}}));
      applyIndicators();
      _chart.timeScale().fitContent();
      var last=data[data.length-1],first=data[0];
      var chg=(last.close-first.open)/first.open*100;
      document.getElementById('ci-price').textContent=fp(last.close,sym);
      var cel=document.getElementById('ci-change');
      cel.textContent=pct(chg);cel.style.color=chg>=0?'var(--green)':'var(--red)';
      // диапазон за период — из свечей (есть всегда)
      var hi=-Infinity,lo=Infinity;
      data.forEach(function(d){{if(d.high>hi)hi=d.high;if(d.low<lo)lo=d.low;}});
      _statsRange={{hi:hi,lo:lo,label:PERIOD_NAMES[period]||period}};
      renderStats();
    }}).catch(function(){{document.getElementById('ci-price').textContent='ошибка';}});
}}

function toggleInd(kind,btn){{
  if(kind==='ma'){{_showMA=!_showMA;}}else{{_showVol=!_showVol;}}
  btn.classList.toggle('active');
  applyIndicators();
}}

function setPeriod(btn){{
  document.querySelectorAll('.pbtn[data-p]').forEach(function(b){{b.classList.remove('active');}});
  btn.classList.add('active');
  _curPeriod=btn.dataset.p;_curInterval=btn.dataset.i;
  loadChartData(_curSym,_curPeriod,_curInterval);
}}

function loadChart(){{
  var v=document.getElementById('sym-input').value.trim().toUpperCase();
  if(!v)return;
  _curSym=v;
  document.getElementById('ci-sym').textContent=v;
  loadChartData(v,_curPeriod,_curInterval);
}}

// Load Lightweight Charts from CDN
(function(){{
  var sc=document.createElement('script');
  sc.src='https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js';
  sc.onload=initChart;
  sc.onerror=function(){{
    document.getElementById('chart-container').innerHTML=
      '<div style="color:var(--muted);padding:30px;text-align:center;font-size:12px">⚠ Не удалось загрузить графики.<br>Нужен интернет.</div>';
  }};
  document.head.appendChild(sc);
}})();

// BLOOMBERG TV (hls.js)
(function(){{
  var sc=document.createElement('script');
  sc.src='https://cdn.jsdelivr.net/npm/hls.js@latest';
  sc.onload=function(){{
    fetch('/api/live-hls').then(r=>r.json()).then(function(d){{
      if(!d.url)return;
      var v=document.getElementById('bloomberg-video');
      if(Hls.isSupported()){{
        var hls=new Hls({{enableWorker:true,lowLatencyMode:true}});
        hls.loadSource(d.url);hls.attachMedia(v);
        hls.on(Hls.Events.MANIFEST_PARSED,function(){{v.play().catch(function(){{}});}});
      }}else if(v.canPlayType('application/vnd.apple.mpegurl')){{
        v.src=d.url;
        v.addEventListener('loadedmetadata',function(){{v.play().catch(function(){{}});}});
      }}
    }}).catch(function(){{}});
  }};
  document.head.appendChild(sc);
}})();

// RUSSIAN DUBBING
var _dubES=null,_dubActive=false,_tts=window.speechSynthesis,_ruVoice=null;
var _ttsQueue=[],_speaking=false,_lastAdded='',_chromeFix=null;
function _loadVoices(){{_ruVoice=_tts.getVoices().find(v=>v.lang.startsWith('ru'));}}
if(_tts.onvoiceschanged!==undefined)_tts.onvoiceschanged=_loadVoices;
_loadVoices();

function _pump(){{
  if(_speaking||_ttsQueue.length===0)return;
  _speaking=true;
  var text=_ttsQueue.shift();
  var u=new SpeechSynthesisUtterance(text);
  u.lang='ru-RU';u.rate=1.05;if(_ruVoice)u.voice=_ruVoice;
  u.onend=function(){{_speaking=false;_pump();}};
  u.onerror=function(){{_speaking=false;_pump();}};
  _tts.speak(u);
}}

function _speakRu(text){{
  if(!text||!_tts)return;
  if(text===_lastAdded)return;
  _lastAdded=text;
  _ttsQueue.push(text);
  if(_ttsQueue.length>2)_ttsQueue.splice(0,_ttsQueue.length-2);
  _pump();
}}

function _updateHint(){{
  var hint=document.getElementById('tr-hint');
  if(!hint||!_dubActive)return;
  var q=_ttsQueue.length;
  if(q>0){{
    hint.textContent='Активно · в очереди: '+q+' · '+new Date().toLocaleTimeString('ru-RU');
  }}else{{
    hint.textContent='Активно · '+new Date().toLocaleTimeString('ru-RU');
  }}
}}

function _addLine(text){{
  var body=document.getElementById('tr-body');if(!body)return;
  var dim=body.querySelector('.tr-dim');if(dim)dim.remove();
  var line=document.createElement('div');line.className='tr-line';line.textContent=text;
  body.appendChild(line);
  while(body.children.length>12)body.removeChild(body.firstChild);
  body.scrollTop=body.scrollHeight;
}}

function toggleDubbing(){{
  var btn=document.getElementById('dub-btn'),hint=document.getElementById('tr-hint');
  if(_dubActive){{
    if(_dubES){{_dubES.close();_dubES=null;}}
    if(_chromeFix){{clearInterval(_chromeFix);_chromeFix=null;}}
    _ttsQueue=[];_speaking=false;_lastAdded='';
    _tts.cancel();_dubActive=false;
    btn.textContent='▶ Озвучка RU';btn.classList.remove('active');
    if(hint)hint.textContent='Остановлено';
  }}else{{
    _dubActive=true;
    btn.textContent='■ Стоп';btn.classList.add('active');
    if(hint)hint.textContent='Подключение... (~4 сек)';
    _chromeFix=setInterval(function(){{
      if(_tts.speaking){{_tts.pause();_tts.resume();}}
    }},10000);
    _dubES=new EventSource('/api/live-transcript');
    _dubES.onmessage=function(e){{
      var d=JSON.parse(e.data);if(!d.text)return;
      _speakRu(d.text);_addLine(d.text);
      _updateHint();
    }};
    _dubES.onerror=function(){{if(hint)hint.textContent='Переподключение...';}} ;
  }}
}}

// NEWS REFRESH (every 5 min)
function refreshNews(){{
  fetch('/api/news').then(r=>r.json()).then(function(data){{
    if(!data||!data.length)return;
    var cats={{}};data.forEach(a=>{{if(!cats[a.cat])cats[a.cat]=[];cats[a.cat].push(a);}});
    var html='';
    Object.keys(cats).forEach(function(cat){{
      html+='<div class="nc-group">'+cat+'</div><div class="nc-grid">';
      cats[cat].slice(0,6).forEach(function(a){{
        var t=a.title_ru||a.title,d=a.desc_ru||a.desc||'';
        var img=a.img?'<div class="nc-img" style="background-image:url('+a.img+')"></div>':'<div class="nc-img nc-img-ph"></div>';
        html+='<a class="nc" href="'+(a.link||'#')+'" target="_blank">'+img
          +'<div class="nc-body"><div class="nc-cat">'+cat+'</div>'
          +'<div class="nc-title">'+t+'</div>'
          +(d?'<div class="nc-desc">'+d.slice(0,130)+'</div>':'')
          +'</div></a>';
      }});
      html+='</div>';
    }});
    if(html)document.getElementById('news-main').innerHTML=html;
  }}).catch(function(){{}});
}}
setInterval(refreshNews,300000);
</script>
</body>
</html>"""



if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8080, reload=False, workers=1)
