"""Core proxy logic: fetch Bloomberg page → translate → rewrite → return HTML."""

import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

BLOOMBERG_BASE = os.getenv("BLOOMBERG_BASE", "https://www.bloomberg.com")
TARGET_LANG = os.getenv("TARGET_LANG", "ru")

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)

# Paywall detection signals — 2+ означает paywall
_PAYWALL_SIGNALS = [
    '"isAccessibleForFree":false',
    'data-track-outcome="hard-wall"',
    'data-module="paywall"',
    '"paywall"',
    'class="fence',
    'paywall-prompt',
    'subscribe-prompt',
    'Log in to read',
    'Subscribe for unlimited access',
    'Already a subscriber',
    'Buy a subscription',
    'trialExpired',
    'gate-content',
]


def _is_paywalled(html: str) -> bool:
    lower = html.lower()
    hits = sum(1 for s in _PAYWALL_SIGNALS if s.lower() in lower)
    return hits >= 2


async def _fetch_url_playwright(url: str, wait: float = 2.0) -> str | None:
    """Fetch any URL via Playwright headless Chromium."""
    from playwright.async_api import async_playwright
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-dev-shm-usage", "--disable-gpu",
                      "--ignore-certificate-errors"],
            )
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 390, "height": 844},
                locale="en-US",
                ignore_https_errors=True,
            )
            page = await context.new_page()
            await page.route("**/*.{png,jpg,jpeg,gif,webp,woff,woff2,ttf,mp4,mp3}",
                             lambda r: r.abort())
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                await asyncio.sleep(wait)
            except Exception:
                pass
            html = await page.content()
            await browser.close()
            return html if len(html) > 3000 else None
    except Exception:
        return None


async def _fetch_via_12ft(bloomberg_url: str) -> str | None:
    """Bypass paywall via 12ft.io (uses Playwright to handle VPN SSL interception)."""
    import urllib.parse
    url = f"https://12ft.io/proxy?q={urllib.parse.quote(bloomberg_url, safe='')}"
    return await _fetch_url_playwright(url, wait=3.0)


async def _fetch_via_archive(bloomberg_url: str) -> str | None:
    """Bypass paywall via archive.ph cached version."""
    import urllib.parse
    url = f"https://archive.ph/newest/{urllib.parse.quote(bloomberg_url, safe=':/')}"
    return await _fetch_url_playwright(url, wait=3.0)


_COOKIES_FILE = os.path.join(os.path.dirname(__file__), "bloomberg-cookies.json")


def _load_cookies() -> list[dict]:
    """Load Bloomberg cookies from file if it exists."""
    import json
    if os.path.exists(_COOKIES_FILE):
        try:
            with open(_COOKIES_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []


async def _fetch_with_playwright(url: str) -> str:
    from playwright.async_api import async_playwright

    cookies = _load_cookies()
    has_cookies = bool(cookies)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            java_script_enabled=True,
            ignore_https_errors=True,
        )

        # Inject real browser cookies to bypass bot check + paywall
        if has_cookies:
            await context.add_cookies(cookies)

        page = await context.new_page()

        await page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "window.chrome={runtime:{}};"
        )

        await page.route(
            "**/*.{png,jpg,jpeg,gif,webp,woff,woff2,ttf,mp4,mp3,svg}",
            lambda route: route.abort(),
        )

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # Extra wait if no cookies (might hit bot check)
            await asyncio.sleep(4 if not has_cookies else 2)
        except Exception:
            pass

        html = await page.content()
        await browser.close()
        return html


def _build_loading_page(path: str) -> str:
    """Return an instant HTML page that shows loading spinner, then polls for result."""
    poll_url = f"/api/status?path={path}"
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bloomberg Translator — Загрузка...</title>
<style>
  body{{margin:0;background:#0a0e1a;color:#e2e8f0;font-family:-apple-system,sans-serif;
       display:flex;flex-direction:column;align-items:center;justify-content:center;
       min-height:100vh;gap:20px;padding:20px;box-sizing:border-box}}
  .spinner{{width:48px;height:48px;border:4px solid #1e293b;
            border-top-color:#3b82f6;border-radius:50%;animation:spin 1s linear infinite}}
  @keyframes spin{{to{{transform:rotate(360deg)}}}}
  .msg{{font-size:16px;color:#94a3b8;text-align:center;max-width:300px;line-height:1.5}}
  .bar{{width:260px;height:4px;background:#1e293b;border-radius:2px;overflow:hidden}}
  .bar-fill{{height:100%;background:#3b82f6;border-radius:2px;
             animation:progress 12s ease-in-out forwards}}
  @keyframes progress{{0%{{width:5%}} 30%{{width:40%}} 70%{{width:75%}} 100%{{width:90%}}}}
</style>
</head>
<body>
<div class="spinner"></div>
<div class="msg">Загружаю страницу Bloomberg<br>и перевожу на русский язык...</div>
<div class="bar"><div class="bar-fill"></div></div>
<div class="msg" style="font-size:13px;opacity:.5">Первая загрузка: ~15 сек<br>Повторная (кэш): &lt;1 сек</div>
<script>
// Poll until the translation is ready, then redirect to it
var target = "/{path}?_translated=1";
var check = setInterval(function(){{
  fetch("/api/ready?path={path}").then(function(r){{return r.json()}})
    .then(function(d){{if(d.ready){{clearInterval(check);window.location.replace(target);}}}})
    .catch(function(){{}});
}}, 2000);
</script>
</body>
</html>"""


async def handle_bloomberg_path(
    path: str,
    target_lang: str = None,
    skip_cache: bool = False,
) -> tuple[str, bool]:
    """
    Fetch, translate and return (html, from_cache).
    On cache hit returns immediately. On miss, performs full pipeline.
    """
    import cache as cache_mod
    import text_nodes as tn
    import link_rewriter as lr
    import translator as tr

    lang = target_lang or TARGET_LANG

    # Cache check
    if not skip_cache:
        cached = cache_mod.get_page(path, lang)
        if cached:
            age = cache_mod.get_page_age(path, lang) or ""
            return cached, True, age

    # Fetch original page with paywall bypass chain
    url = f"{BLOOMBERG_BASE}/{path.lstrip('/')}"
    source = "bloomberg"
    try:
        raw_html = await _fetch_with_playwright(url)
    except Exception as e:
        raise RuntimeError(f"Playwright fetch failed: {e}")

    # Paywall or bot-block (too little content) → try 12ft.io → archive.ph
    is_blocked = _is_paywalled(raw_html) or len(raw_html) < 20_000
    if is_blocked:
        bypass = await _fetch_via_12ft(url)
        if bypass and len(bypass) > 20_000 and not _is_paywalled(bypass):
            raw_html = bypass
            source = "12ft"
        else:
            bypass = await _fetch_via_archive(url)
            if bypass and len(bypass) > 20_000:
                raw_html = bypass
                source = "archive"

    # Extract translatable text nodes, get marked-up HTML
    marked_html, nodes = tn.extract(raw_html)

    # Build translation batches and translate
    batches = tn.make_batches(nodes, max_chars=4000)
    translations = await tr.translate_all_batches(batches, lang)

    # Apply translations to marked HTML
    translated_html = tn.apply(marked_html, translations)

    # Rewrite links and inject assets handling
    rewritten_html = lr.rewrite(translated_html)

    # Inject header bar
    src_label = {"12ft": " • via 12ft", "archive": " • via archive.ph", "bloomberg": ""}.get(source, "")
    final_html = lr.inject_header_bar(rewritten_html, path, lang, cache_age=f"только что{src_label}")

    # Store in cache
    cache_mod.set_page(path, lang, final_html)

    return final_html, False, "только что"
