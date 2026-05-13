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

@app.get("/api/live-hls")
async def api_live_hls():
    """Return raw HLS .m3u8 URL for Bloomberg TV — used by hls.js player and audio capture."""
    import time
    now = time.time()
    if _HLS_CACHE.get("ts", 0) + 1800 > now and _HLS_CACHE.get("url"):
        return JSONResponse({"url": _HLS_CACHE["url"]})

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
    return JSONResponse({"url": url or _HLS_CACHE.get("url", "")})


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
        from live_audio import live_dubbing_loop
        _dub_task = asyncio.create_task(live_dubbing_loop(vid, _dub_callback))

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
    return "#22c55e" if change >= 0 else "#ef4444"


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

    news_html = ""
    for i, (cat, articles) in enumerate(cats.items()):
        # Insert live widget after first news section
        if i == 1:
            news_html += _LIVE_WIDGET
        news_html += f'<div class="section-title"><span class="st-line"></span>{cat}<span class="st-line"></span></div><div class="news-grid">'
        for a in articles[:6]:
            title = a.get("title_ru") or a["title"]
            desc = a.get("desc_ru") or a.get("desc", "")
            link = a.get("link", "#")
            img = a.get("img", "")
            img_html = f'<div class="card-img" style="background-image:url({img})"></div>' if img else '<div class="card-img card-img-placeholder"></div>'
            news_html += (
                f'<a class="card" href="{link}" target="_blank">'
                f'{img_html}'
                f'<div class="card-body">'
                f'<div class="cat-label">{cat}</div>'
                f'<div class="card-title">{title}</div>'
                f'{"<div class=card-desc>" + desc + "</div>" if desc else ""}'
                f'</div>'
                f'</a>'
            )
        news_html += '</div>'

    if not news_html:
        news_html = '<div style="color:#475569;text-align:center;padding:40px">Загрузка новостей...</div>'
    return news_html


def _sw(label: str, key: str, symbol: str) -> str:
    """Generate one sidebar widget HTML."""
    return (
        f'<div class="swidget">'
        f'<div class="sw-header"><div class="sw-label">{label}</div>'
        f'<div class="sw-periods">'
        f'<button class="sw-period active" onclick="loadHist(this,\'{symbol}\',\'1wk\',\'sw-{key}-hist\')">1Н</button>'
        f'<button class="sw-period" onclick="loadHist(this,\'{symbol}\',\'1mo\',\'sw-{key}-hist\')">1М</button>'
        f'<button class="sw-period" onclick="loadHist(this,\'{symbol}\',\'1y\',\'sw-{key}-hist\')">1Г</button>'
        f'</div></div>'
        f'<div class="sw-price" id="sw-{key}-price">—</div>'
        f'<div class="sw-change" id="sw-{key}-ch">'
        f'<span class="sw-arrow" id="sw-{key}-arr"></span>'
        f'<span id="sw-{key}-pct"></span>'
        f'&nbsp;<span style="color:var(--muted);font-weight:400">сегодня</span></div>'
        f'<div class="sw-hist" id="sw-{key}-hist"></div>'
        f'<div class="sw-bar-wrap"><div class="sw-bar" id="sw-{key}-bar" style="width:50%"></div></div>'
        f'</div>'
    )


def _render_dashboard(market: list, news: list) -> str:
    ticker_items = _render_ticker_items(market)
    news_html = _render_news_html(news)
    sidebar_left = (
        _sw('Золото',   'gold',     'GC=F')
        + _sw('Серебро', 'silver',   'SI=F')
        + _sw('Платина', 'platinum', 'PL=F')
        + _sw('Палладий','palladium','PA=F')
    )
    sidebar_right = (
        _sw('Нефть WTI','oil',   'CL=F')
        + _sw('Прир. газ','gas',  'NG=F')
        + _sw('Bitcoin', 'btc',   'BTC-USD')
        + _sw('Ethereum','eth',   'ETH-USD')
    )

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bloomberg RU — Финансовые новости</title>
<style>
:root{{--bg:#07090f;--surface:#0d1117;--surface2:#111827;--border:#1c2537;--accent:#2563eb;--accent2:#3b82f6;--text:#f1f5f9;--muted:#64748b;--green:#22c55e;--red:#ef4444}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Inter',sans-serif;min-height:100vh}}

/* ── Header ── */
.header{{background:rgba(7,9,15,.95);backdrop-filter:blur(12px);border-bottom:1px solid var(--border);padding:0 20px;display:flex;align-items:center;gap:16px;height:52px;position:sticky;top:0;z-index:200}}
.logo{{font-size:17px;font-weight:900;letter-spacing:-.5px;white-space:nowrap;display:flex;align-items:center;gap:6px}}
.logo-b{{color:#fff}}.logo-ru{{background:var(--accent);color:#fff;padding:1px 6px;border-radius:4px;font-size:13px;font-weight:800;letter-spacing:.5px}}
.header-right{{margin-left:auto;display:flex;align-items:center;gap:12px}}
.live-badge{{display:flex;align-items:center;gap:5px;font-size:11px;font-weight:700;color:var(--green);letter-spacing:.5px;text-transform:uppercase}}
.live-dot{{width:6px;height:6px;background:var(--green);border-radius:50%;animation:pulse 2s ease-in-out infinite;flex-shrink:0}}
@keyframes pulse{{0%,100%{{opacity:1;transform:scale(1)}}50%{{opacity:.4;transform:scale(.8)}}}}
.htime{{font-size:11px;color:var(--muted)}}

/* ── Ticker ── */
.ticker-wrap{{background:var(--surface);border-bottom:1px solid var(--border);overflow:hidden;height:40px;display:flex;align-items:center;position:relative}}
.ticker-wrap::before,.ticker-wrap::after{{content:'';position:absolute;top:0;bottom:0;width:60px;z-index:10;pointer-events:none}}
.ticker-wrap::before{{left:0;background:linear-gradient(90deg,var(--surface),transparent)}}
.ticker-wrap::after{{right:0;background:linear-gradient(-90deg,var(--surface),transparent)}}
.ticker-inner{{display:flex;gap:0;animation:scroll 80s linear infinite;white-space:nowrap;will-change:transform}}
.ticker-inner:hover{{animation-play-state:paused}}
@keyframes scroll{{from{{transform:translateX(0)}}to{{transform:translateX(-50%)}}}}
.tick{{display:inline-flex;align-items:center;gap:8px;padding:0 24px;border-right:1px solid var(--border);font-size:12px;cursor:default}}
.tname{{color:var(--muted);font-weight:600;font-size:11px;letter-spacing:.3px}}
.tprice{{color:var(--text);font-weight:700;font-variant-numeric:tabular-nums}}
.tch{{font-size:11px;font-weight:700;font-variant-numeric:tabular-nums}}

/* ── Main layout ── */
.page-wrap{{max-width:1440px;margin:0 auto;padding:24px 20px;display:grid;grid-template-columns:200px 1fr 200px;gap:20px;align-items:start}}
.main{{min-width:0}}
/* Sidebar */
.sidebar{{display:flex;flex-direction:column;gap:14px;position:sticky;top:72px}}
.swidget{{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:16px;display:flex;flex-direction:column;gap:8px;transition:border-color .18s}}
.swidget:hover{{border-color:var(--accent)}}
.sw-header{{display:flex;align-items:center;justify-content:space-between}}
.sw-label{{font-size:10px;font-weight:800;color:var(--muted);letter-spacing:1px;text-transform:uppercase}}
.sw-periods{{display:flex;gap:2px}}
.sw-period{{font-size:9px;font-weight:700;padding:2px 5px;border-radius:4px;border:1px solid var(--border);color:var(--muted);cursor:pointer;transition:all .15s;background:none;letter-spacing:.3px}}
.sw-period:hover,.sw-period.active{{background:var(--accent);border-color:var(--accent);color:#fff}}
.sw-price{{font-size:24px;font-weight:800;letter-spacing:-.5px;font-variant-numeric:tabular-nums;line-height:1}}
.sw-change{{font-size:12px;font-weight:700;display:flex;align-items:center;gap:4px}}
.sw-hist{{font-size:11px;color:var(--muted);min-height:14px}}
.sw-arrow{{font-size:9px}}
.sw-bar-wrap{{width:100%;height:3px;background:var(--border);border-radius:2px;overflow:hidden}}
.sw-bar{{height:100%;border-radius:2px;transition:width .6s ease}}
@media(max-width:1024px){{
  .page-wrap{{grid-template-columns:1fr;grid-template-rows:auto}}
  .sidebar{{flex-direction:row;flex-wrap:wrap;position:static}}
  .swidget{{flex:1;min-width:140px}}
}}
@media(max-width:640px){{.page-wrap{{padding:12px}}}}

/* ── Metrics bar ── */
.metrics-bar{{background:var(--surface);border-bottom:1px solid var(--border);padding:0 20px;overflow-x:auto;scrollbar-width:none}}
.metrics-bar::-webkit-scrollbar{{display:none}}
.metrics-row{{display:flex;gap:0;min-width:max-content;height:80px;align-items:stretch}}
.metric{{display:flex;flex-direction:column;justify-content:center;padding:0 24px;border-right:1px solid var(--border);gap:3px;min-width:140px;cursor:default;transition:background .15s}}
.metric:first-child{{padding-left:0}}
.metric:last-child{{border-right:none}}
.metric:hover{{background:rgba(255,255,255,.03)}}
.metric-label{{font-size:10px;font-weight:700;color:var(--muted);letter-spacing:.8px;text-transform:uppercase}}
.metric-value{{font-size:20px;font-weight:800;letter-spacing:-.5px;font-variant-numeric:tabular-nums;line-height:1}}
.metric-sub{{font-size:11px;font-weight:600;margin-top:1px}}
.metric-bar-wrap{{width:100%;height:3px;background:var(--border);border-radius:2px;margin-top:4px;overflow:hidden}}
.metric-bar-fill{{height:100%;border-radius:2px;transition:width .5s ease}}
/* ── Fear & Greed gauge ── */
.fg-metric{{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:0 28px;border-right:1px solid var(--border);gap:4px;cursor:default}}
.fg-label{{font-size:10px;font-weight:700;color:var(--muted);letter-spacing:.8px;text-transform:uppercase;align-self:flex-start}}
.fg-body{{display:flex;align-items:center;gap:14px}}
.fg-gauge{{position:relative;width:64px;height:34px;overflow:visible}}
.fg-gauge svg{{width:64px;height:34px}}
.fg-score{{font-size:22px;font-weight:900;letter-spacing:-.5px;line-height:1}}
.fg-name{{font-size:11px;font-weight:700;margin-top:1px}}

/* ── Section header ── */
.section-title{{display:flex;align-items:center;gap:12px;font-size:10px;font-weight:800;color:var(--accent2);letter-spacing:2px;text-transform:uppercase;margin:32px 0 16px}}
.section-title:first-child{{margin-top:0}}
.st-line{{flex:1;height:1px;background:var(--border)}}

/* ── News grid ── */
.news-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px;margin-bottom:8px}}

/* ── Card ── */
.card{{display:flex;flex-direction:column;background:var(--surface);border:1px solid var(--border);border-radius:14px;overflow:hidden;text-decoration:none;color:inherit;transition:transform .18s,border-color .18s,box-shadow .18s}}
.card:hover{{transform:translateY(-3px);border-color:var(--accent);box-shadow:0 8px 32px rgba(37,99,235,.15)}}
.card-img{{width:100%;height:172px;background-size:cover;background-position:center;background-color:var(--surface2);flex-shrink:0}}
.card-img-placeholder{{background:linear-gradient(135deg,#111827 0%,#1c2537 100%);display:flex;align-items:center;justify-content:center}}
.card-img-placeholder::after{{content:'◈';font-size:32px;color:#1e3a5f;opacity:.6}}
.card-body{{padding:14px 16px 16px;display:flex;flex-direction:column;gap:8px;flex:1}}
.cat-label{{font-size:10px;font-weight:800;letter-spacing:1.2px;text-transform:uppercase;color:var(--accent2)}}
.card-title{{font-size:14px;font-weight:700;line-height:1.45;color:var(--text)}}
.card-desc{{font-size:12px;color:var(--muted);line-height:1.6;margin-top:2px}}

/* ── Live widget ── */
.live-widget{{background:var(--surface);border:1px solid var(--border);border-radius:16px;overflow:hidden;margin:8px 0 24px}}
.lw-header{{display:flex;align-items:center;gap:12px;padding:12px 16px;border-bottom:1px solid var(--border);background:rgba(37,99,235,.08)}}
.lw-badge{{display:flex;align-items:center;gap:6px;font-size:10px;font-weight:800;color:#ef4444;letter-spacing:1px;text-transform:uppercase}}
.lw-title{{font-size:14px;font-weight:700;color:var(--text)}}
.lw-delay-badge{{font-size:10px;font-weight:700;color:var(--muted);letter-spacing:.3px;background:var(--surface2);padding:2px 7px;border-radius:10px;border:1px solid var(--border)}}
.lw-yt-link{{margin-left:auto;font-size:11px;font-weight:700;color:var(--accent2);text-decoration:none;padding:4px 10px;border:1px solid var(--accent);border-radius:6px;transition:background .15s}}
.lw-yt-link:hover{{background:var(--accent);color:#fff}}
.lw-close{{background:none;border:none;color:var(--muted);cursor:pointer;font-size:16px;padding:2px 6px;border-radius:4px;transition:color .15s;margin-left:8px}}
.lw-close:hover{{color:var(--text)}}
.lw-body{{display:grid;grid-template-columns:1fr 320px;gap:0}}
.lw-video-wrap{{position:relative;padding-top:56.25%;background:#000}}
.lw-video-wrap iframe,.lw-video-wrap #yt-player-div{{position:absolute;top:0;left:0;width:100%;height:100%;border:0}}
.lw-transcript{{display:flex;flex-direction:column;border-left:1px solid var(--border);background:var(--bg)}}
.lw-tr-header{{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;border-bottom:1px solid var(--border);font-size:11px;font-weight:700;color:var(--muted);letter-spacing:.5px;text-transform:uppercase;flex-shrink:0}}
.lw-tr-badge{{font-size:10px;font-weight:700;color:var(--green);animation:pulse 2s ease-in-out infinite}}
.lw-tr-body{{flex:1;overflow-y:auto;padding:12px 14px;display:flex;flex-direction:column;gap:6px;max-height:350px;scrollbar-width:thin;scrollbar-color:var(--border) transparent}}
.tr-line{{font-size:13px;line-height:1.6;color:var(--text);padding:6px 10px;border-radius:8px;background:var(--surface);border-left:3px solid var(--accent)}}
.tr-line.tr-dim{{color:var(--muted);background:none;border-left-color:var(--border);font-size:12px}}
.lw-tr-footer{{padding:8px 14px;border-top:1px solid var(--border);display:flex;align-items:center;gap:10px;flex-shrink:0}}
.lw-dub-btn{{font-size:11px;font-weight:700;padding:5px 12px;border-radius:6px;border:1px solid var(--accent);color:var(--accent2);background:none;cursor:pointer;transition:all .15s;white-space:nowrap;flex-shrink:0}}
.lw-dub-btn:hover,.lw-dub-btn.active{{background:var(--accent);color:#fff}}
.lw-tr-hint{{font-size:11px;color:var(--muted);line-height:1.5}}
@media(max-width:900px){{.lw-body{{grid-template-columns:1fr}}.lw-transcript{{border-left:none;border-top:1px solid var(--border)}}}}
/* ── Mobile ── */
@media(max-width:640px){{
  .news-grid{{grid-template-columns:1fr}}
  .main{{padding:16px 12px}}
  .header{{padding:0 12px}}
  .card-img{{height:140px}}
}}
</style>
</head>
<body>
<header class="header">
  <div class="logo"><span class="logo-b">Bloomberg</span><span class="logo-ru">RU</span></div>
  <div class="header-right">
    <div class="live-badge"><span class="live-dot"></span>LIVE</div>
    <div class="htime" id="htime-txt">{len(news)} новостей · {len(market)} рынков</div>
  </div>
</header>

<div class="ticker-wrap">
  <div class="ticker-inner" id="ticker-inner">
    {ticker_items}{ticker_items}
  </div>
</div>

<div class="metrics-bar">
  <div class="metrics-row" id="metrics-row">
    <div class="fg-metric">
      <div class="fg-label">Индекс жадности</div>
      <div class="fg-body">
        <div class="fg-gauge">
          <svg viewBox="0 0 64 34" fill="none">
            <path d="M4 34 A28 28 0 0 1 60 34" stroke="#1c2537" stroke-width="7" stroke-linecap="round"/>
            <path id="fg-arc" d="M4 34 A28 28 0 0 1 60 34" stroke="#94a3b8" stroke-width="7" stroke-linecap="round"
                  stroke-dasharray="88" stroke-dashoffset="88" style="transition:stroke-dashoffset .8s ease,stroke .8s ease"/>
            <circle id="fg-needle" cx="32" cy="34" r="0" fill="white"/>
          </svg>
        </div>
        <div>
          <div class="fg-score" id="fg-score">—</div>
          <div class="fg-name" id="fg-name">загрузка...</div>
        </div>
      </div>
    </div>
    <div class="metric"><div class="metric-label">Настроение рынка</div><div class="metric-value" id="m-mood">—</div><div class="metric-sub" id="m-mood-sub"></div></div>
    <div class="metric"><div class="metric-label">Рынки растут</div><div class="metric-value" id="m-breadth">—</div><div class="metric-bar-wrap"><div class="metric-bar-fill" id="m-breadth-bar" style="background:var(--green);width:0%"></div></div></div>
    <div class="metric"><div class="metric-label">Лидер роста</div><div class="metric-value" style="font-size:15px" id="m-top-name">—</div><div class="metric-sub" id="m-top-val"></div></div>
    <div class="metric"><div class="metric-label">Лидер падения</div><div class="metric-value" style="font-size:15px" id="m-bot-name">—</div><div class="metric-sub" id="m-bot-val"></div></div>
    <div class="metric"><div class="metric-label">Bitcoin</div><div class="metric-value" id="m-btc">—</div><div class="metric-sub" id="m-btc-ch"></div></div>
    <div class="metric"><div class="metric-label">Gold</div><div class="metric-value" id="m-gold">—</div><div class="metric-sub" id="m-gold-ch"></div></div>
    <div class="metric"><div class="metric-label">USD/RUB</div><div class="metric-value" id="m-rub">—</div><div class="metric-sub" id="m-rub-ch"></div></div>
    <div class="metric"><div class="metric-label">Oil WTI</div><div class="metric-value" id="m-oil">—</div><div class="metric-sub" id="m-oil-ch"></div></div>
  </div>
</div>

<div class="page-wrap">

  <!-- Left sidebar: precious metals -->
  <aside class="sidebar">
    {sidebar_left}
  </aside>

  <!-- Center: news -->
  <main class="main" id="news-main">
    {news_html}
  </main>

  <!-- Right sidebar: energy + crypto -->
  <aside class="sidebar">
    {sidebar_right}
  </aside>

</div>

<script>
function fmtPrice(p){{
  if(p>100)return p.toLocaleString('ru-RU',{{minimumFractionDigits:2,maximumFractionDigits:2}});
  if(p<1)return p.toFixed(4);
  return p.toLocaleString('ru-RU',{{minimumFractionDigits:2,maximumFractionDigits:2}});
}}
function buildTick(m){{
  var clr=m.change>=0?'#22c55e':'#ef4444';
  var sign=m.change>=0?'+':'';
  return '<div class="tick"><span class="tname">'+m.name+'</span>'
    +'<span class="tprice">'+fmtPrice(m.price)+'</span>'
    +'<span class="tch" style="color:'+clr+'">'+sign+m.change.toFixed(2)+'%</span></div>';
}}
function buildCard(a,cat){{
  var title=a.title_ru||a.title;
  var desc=a.desc_ru||a.desc||'';
  var img=a.img?'<div class="card-img" style="background-image:url('+a.img+')"></div>'
                :'<div class="card-img card-img-placeholder"></div>';
  return '<a class="card" href="'+(a.link||'#')+'" target="_blank">'
    +img+'<div class="card-body">'
    +'<div class="cat-label">'+cat+'</div>'
    +'<div class="card-title">'+title+'</div>'
    +(desc?'<div class="card-desc">'+desc+'</div>':'')
    +'</div></a>';
}}
function clr(v){{return v>=0?'#22c55e':'#ef4444';}}
function sign(v){{return v>=0?'+':''}}
function pct(v){{return sign(v)+v.toFixed(2)+'%';}}

function updateMetrics(data){{
  // Sentiment: avg change of indices (excl. currencies & crypto)
  var indices=['S&P 500','NASDAQ','DOW','FTSE 100','DAX','Nikkei'];
  var idxData=data.filter(m=>indices.includes(m.name));
  var avgChange=idxData.length?idxData.reduce((s,m)=>s+m.change,0)/idxData.length:0;
  var moodEl=document.getElementById('m-mood');
  var moodSub=document.getElementById('m-mood-sub');
  if(avgChange>1){{moodEl.textContent='Жадность';moodEl.style.color='#22c55e';moodSub.textContent='Рынки уверенно растут';moodSub.style.color='#22c55e';}}
  else if(avgChange>0){{moodEl.textContent='Оптимизм';moodEl.style.color='#86efac';moodSub.textContent='Умеренный рост';moodSub.style.color='#86efac';}}
  else if(avgChange>-1){{moodEl.textContent='Нейтрально';moodEl.style.color='#94a3b8';moodSub.textContent='Рынки в боковике';moodSub.style.color='#94a3b8';}}
  else if(avgChange>-2){{moodEl.textContent='Тревога';moodEl.style.color='#fb923c';moodSub.textContent='Продавцы давят';moodSub.style.color='#fb923c';}}
  else{{moodEl.textContent='Страх';moodEl.style.color='#ef4444';moodSub.textContent='Распродажа на рынках';moodSub.style.color='#ef4444';}}

  // Breadth: % of all instruments rising
  var rising=data.filter(m=>m.change>=0).length;
  var breadthPct=Math.round(rising/data.length*100);
  document.getElementById('m-breadth').textContent=rising+' из '+data.length;
  document.getElementById('m-breadth').style.color=breadthPct>=50?'#22c55e':'#ef4444';
  var bar=document.getElementById('m-breadth-bar');
  bar.style.width=breadthPct+'%';
  bar.style.background=breadthPct>=50?'#22c55e':'#ef4444';

  // Top gainer / loser
  var sorted=[...data].sort((a,b)=>b.change-a.change);
  var top=sorted[0],bot=sorted[sorted.length-1];
  document.getElementById('m-top-name').textContent=top.name;
  var tv=document.getElementById('m-top-val');tv.textContent=pct(top.change);tv.style.color='#22c55e';
  document.getElementById('m-bot-name').textContent=bot.name;
  var bv=document.getElementById('m-bot-val');bv.textContent=pct(bot.change);bv.style.color='#ef4444';

  // Individual instruments
  var byName={{}};data.forEach(m=>{{byName[m.name]=m;}});
  function setMetric(valId,chId,name,fmt){{
    var m=byName[name];if(!m)return;
    document.getElementById(valId).textContent=fmt?fmt(m.price):fmtPrice(m.price);
    var el=document.getElementById(chId);el.textContent=pct(m.change);el.style.color=clr(m.change);
  }}
  setMetric('m-btc','m-btc-ch','Bitcoin',p=>'$'+Math.round(p).toLocaleString('ru-RU'));
  setMetric('m-gold','m-gold-ch','Gold',p=>'$'+Math.round(p).toLocaleString('ru-RU'));
  setMetric('m-rub','m-rub-ch','USD/RUB',p=>p.toFixed(2)+'₽');
  setMetric('m-oil','m-oil-ch','Oil (WTI)',p=>'$'+p.toFixed(2));
}}

function setSWidget(name,priceId,chId,arrId,pctId,barId,fmt,data){{
  var byName={{}};data.forEach(m=>{{byName[m.name]=m;}});
  var m=byName[name];if(!m)return;
  var p=fmt?fmt(m.price):fmtPrice(m.price);
  document.getElementById(priceId).textContent=p;
  var c=m.change,color=c>=0?'#22c55e':'#ef4444';
  document.getElementById(chId).style.color=color;
  document.getElementById(arrId).textContent=c>=0?'▲':'▼';
  document.getElementById(pctId).textContent=(c>=0?'+':'')+c.toFixed(2)+'%';
  // bar: center=50%, +5% → 100%, -5% → 0%
  var barW=Math.min(100,Math.max(0,50+c*10));
  var bar=document.getElementById(barId);bar.style.width=barW+'%';bar.style.background=color;
}}
function updateSidebars(data){{
  setSWidget('Gold',     'sw-gold-price',     'sw-gold-ch',     'sw-gold-arr',     'sw-gold-pct',     'sw-gold-bar',     p=>'$'+Math.round(p).toLocaleString('ru-RU'), data);
  setSWidget('Silver',   'sw-silver-price',   'sw-silver-ch',   'sw-silver-arr',   'sw-silver-pct',   'sw-silver-bar',   p=>'$'+p.toFixed(2), data);
  setSWidget('Platinum', 'sw-platinum-price', 'sw-platinum-ch', 'sw-platinum-arr', 'sw-platinum-pct', 'sw-platinum-bar', p=>'$'+Math.round(p).toLocaleString('ru-RU'), data);
  setSWidget('Palladium','sw-palladium-price','sw-palladium-ch','sw-palladium-arr','sw-palladium-pct','sw-palladium-bar',p=>'$'+Math.round(p).toLocaleString('ru-RU'), data);
  setSWidget('Oil (WTI)','sw-oil-price',      'sw-oil-ch',      'sw-oil-arr',      'sw-oil-pct',      'sw-oil-bar',      p=>'$'+p.toFixed(2), data);
  setSWidget('Nat. Gas', 'sw-gas-price',      'sw-gas-ch',      'sw-gas-arr',      'sw-gas-pct',      'sw-gas-bar',      p=>'$'+p.toFixed(3), data);
  setSWidget('Bitcoin',  'sw-btc-price',      'sw-btc-ch',      'sw-btc-arr',      'sw-btc-pct',      'sw-btc-bar',      p=>'$'+Math.round(p).toLocaleString('ru-RU'), data);
  setSWidget('Ethereum', 'sw-eth-price',      'sw-eth-ch',      'sw-eth-arr',      'sw-eth-pct',      'sw-eth-bar',      p=>'$'+Math.round(p).toLocaleString('ru-RU'), data);
}}
function refreshMarket(){{
  fetch('/api/market').then(r=>r.json()).then(data=>{{
    if(!data||!data.length)return;
    var html='';data.forEach(m=>{{html+=buildTick(m);}});
    document.getElementById('ticker-inner').innerHTML=html+html;
    updateMetrics(data);
    updateSidebars(data);
  }}).catch(()=>{{}});
}}
refreshMarket();
setInterval(refreshMarket,30000);
function refreshNews(){{
  fetch('/api/news').then(r=>r.json()).then(data=>{{
    if(!data||!data.length)return;
    // Deduplicate by link, then by title prefix
    var seen=new Set(),deduped=[];
    data.forEach(a=>{{
      var key=(a.link||a.title.slice(0,80)).replace(/\/$/,'');
      if(!seen.has(key)){{seen.add(key);deduped.push(a);}}
    }});
    data=deduped;
    var cats={{}};
    data.forEach(a=>{{if(!cats[a.cat])cats[a.cat]=[];cats[a.cat].push(a);}});
    var html='';
    Object.keys(cats).forEach(cat=>{{
      html+='<div class="section-title"><span class="st-line"></span>'+cat+'<span class="st-line"></span></div><div class="news-grid">';
      cats[cat].slice(0,6).forEach(a=>{{html+=buildCard(a,cat);}});
      html+='</div>';
    }});
    if(html)document.getElementById('news-main').innerHTML=html;
  }}).catch(()=>{{}});
}}
setInterval(refreshNews,300000);

// Fear & Greed gauge
var fgColors={{
  'Extreme Fear':'#ef4444','Fear':'#f97316',
  'Neutral':'#eab308','Greed':'#84cc16','Extreme Greed':'#22c55e'
}};
var fgLabelsRu={{
  'Extreme Fear':'Крайний страх','Fear':'Страх',
  'Neutral':'Нейтрально','Greed':'Жадность','Extreme Greed':'Крайняя жадность'
}};
function loadFearGreed(){{
  fetch('/api/fear-greed').then(r=>r.json()).then(d=>{{
    if(!d||d.value==null) return;
    var v=d.value, label=d.label;
    var color=fgColors[label]||'#94a3b8';
    var labelRu=fgLabelsRu[label]||label;
    // Score + label
    var scoreEl=document.getElementById('fg-score');
    scoreEl.textContent=v;
    scoreEl.style.color=color;
    var nameEl=document.getElementById('fg-name');
    nameEl.textContent=labelRu;
    nameEl.style.color=color;
    // Animate arc: total arc length ≈ 88 (π×28), dashoffset goes from 88→0 as v→100
    var arc=document.getElementById('fg-arc');
    var offset=88-(v/100*88);
    arc.style.strokeDashoffset=offset;
    arc.style.stroke=color;
  }}).catch(()=>{{}});
}}
loadFearGreed();
setInterval(loadFearGreed, 3600000); // refresh hourly

// Bloomberg TV — hls.js direct HLS stream (no YouTube UI)
(function(){{
  var s=document.createElement('script');
  s.src='https://cdn.jsdelivr.net/npm/hls.js@latest';
  s.onload=function(){{
    fetch('/api/live-hls').then(function(r){{return r.json();}}).then(function(d){{
      if(!d.url)return;
      var video=document.getElementById('bloomberg-video');
      if(!video)return;
      if(Hls.isSupported()){{
        var hls=new Hls({{enableWorker:true,lowLatencyMode:true}});
        hls.loadSource(d.url);
        hls.attachMedia(video);
        hls.on(Hls.Events.MANIFEST_PARSED,function(){{video.play().catch(function(){{}});}});
      }}else if(video.canPlayType('application/vnd.apple.mpegurl')){{
        video.src=d.url;
        video.addEventListener('loadedmetadata',function(){{video.play().catch(function(){{}});}});
      }}
    }}).catch(function(){{}});
  }};
  document.head.appendChild(s);
}})();

// Historical period switcher
var periodLabels={{'1wk':'за неделю','1mo':'за месяц','1y':'за год'}};
function loadHist(btn,symbol,period,histId){{
  // Toggle active button in the same widget
  var btns=btn.parentNode.querySelectorAll('.sw-period');
  btns.forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  var el=document.getElementById(histId);
  el.textContent='...';
  fetch('/api/market/history?symbol='+encodeURIComponent(symbol)+'&period='+period)
    .then(r=>r.json()).then(d=>{{
      if(d.change==null){{el.textContent='нет данных';return;}}
      var c=d.change,color=c>=0?'#22c55e':'#ef4444';
      var label=periodLabels[period]||period;
      el.innerHTML='<span style="color:'+color+';font-weight:700">'+(c>=0?'+':'')+c.toFixed(2)+'%</span> '+label;
    }}).catch(()=>{{el.textContent='';}});
}}
// ── Bloomberg TV Russian Dubbing ──────────────────────────────────────────
var _dubES=null,_dubActive=false;
var _tts=window.speechSynthesis;
var _ruVoice=null;
function _loadVoices(){{
  var voices=_tts.getVoices();
  _ruVoice=voices.find(function(v){{return v.lang.startsWith('ru');}});
}}
if(_tts.onvoiceschanged!==undefined)_tts.onvoiceschanged=_loadVoices;
_loadVoices();
function _speakRu(text){{
  if(!text||!_tts)return;
  _tts.cancel();
  var u=new SpeechSynthesisUtterance(text);
  u.lang='ru-RU';u.rate=1.05;
  if(_ruVoice)u.voice=_ruVoice;
  _tts.speak(u);
}}
function _addTranscriptLine(text){{
  var body=document.getElementById('tr-body');
  if(!body)return;
  var dim=body.querySelector('.tr-dim');if(dim)dim.remove();
  var line=document.createElement('div');
  line.className='tr-line';line.textContent=text;
  body.appendChild(line);
  while(body.children.length>10)body.removeChild(body.firstChild);
  body.scrollTop=body.scrollHeight;
}}
function toggleDubbing(){{
  var btn=document.getElementById('dub-btn');
  var hint=document.getElementById('tr-hint');
  if(_dubActive){{
    if(_dubES){{_dubES.close();_dubES=null;}}
    _tts.cancel();_dubActive=false;
    btn.textContent='Включить озвучку RU';btn.classList.remove('active');
    if(hint)hint.textContent='Озвучка остановлена';
  }}else{{
    _dubActive=true;
    btn.textContent='Остановить озвучку';btn.classList.add('active');
    if(hint)hint.textContent='Подключаюсь... (задержка ~5 сек)';
    _dubES=new EventSource('/api/live-transcript');
    _dubES.onmessage=function(e){{
      var d=JSON.parse(e.data);
      if(!d.text)return;
      _speakRu(d.text);
      _addTranscriptLine(d.text);
      if(hint)hint.textContent='Озвучка активна • последнее обновление: '+new Date().toLocaleTimeString('ru-RU');
    }};
    _dubES.onerror=function(){{
      if(hint)hint.textContent='Ошибка соединения, переподключение...';
    }};
  }}
}}

// Load default history (1wk) for all widgets on startup
var defaults=[
  ['GC=F','sw-gold-hist'],['SI=F','sw-silver-hist'],
  ['PL=F','sw-platinum-hist'],['PA=F','sw-palladium-hist'],
  ['CL=F','sw-oil-hist'],['NG=F','sw-gas-hist'],
  ['BTC-USD','sw-btc-hist'],['ETH-USD','sw-eth-hist']
];
defaults.forEach(function(d){{
  fetch('/api/market/history?symbol='+encodeURIComponent(d[0])+'&period=1wk')
    .then(r=>r.json()).then(function(res){{
      if(res.change==null)return;
      var c=res.change,color=c>=0?'#22c55e':'#ef4444';
      var el=document.getElementById(d[1]);if(!el)return;
      el.innerHTML='<span style="color:'+color+';font-weight:700">'+(c>=0?'+':'')+c.toFixed(2)+'%</span> за неделю';
    }}).catch(()=>{{}});
}});
</script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8080, reload=False, workers=1)
