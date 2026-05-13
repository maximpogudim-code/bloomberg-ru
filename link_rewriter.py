"""Rewrite bloomberg.com links to stay within our proxy domain."""

import os
from bs4 import BeautifulSoup

BLOOMBERG_BASE = os.getenv("BLOOMBERG_BASE", "https://www.bloomberg.com")
_BLOOMBERG_VARIANTS = [
    "https://www.bloomberg.com",
    "https://bloomberg.com",
    "http://www.bloomberg.com",
    "http://bloomberg.com",
]


def rewrite(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")

    # 1. Rewrite internal Bloomberg <a href> → relative paths (stay on proxy)
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        for variant in _BLOOMBERG_VARIANTS:
            if href.startswith(variant):
                tag["href"] = href[len(variant):]  # keep path only
                break

    # 2. Fix relative resource URLs → absolute bloomberg.com URLs
    #    so CSS/JS/images still load from bloomberg.com
    for tag in soup.find_all(True):
        for attr in ("src", "href", "data-src", "srcset"):
            val = tag.get(attr, "")
            if val and val.startswith("/") and not val.startswith("//"):
                # Check it's not already an <a> href (handled above)
                if tag.name != "a" or attr != "href":
                    tag[attr] = BLOOMBERG_BASE + val

    # 3. Remove Content-Security-Policy meta tags (blocks our injected assets)
    for meta in soup.find_all("meta", {"http-equiv": lambda v: v and "security-policy" in v.lower()}):
        meta.decompose()

    # 4. Disable Bloomberg's JS re-hydration (prevents overwriting our translations)
    # Add a script that stubs React's hydration call
    stub = soup.new_tag("script")
    stub.string = (
        "window.__bloomberg_translator = true; "
        "if(window.__webpack_require__){window.__webpack_require__ = function(){};};"
    )
    if soup.head:
        soup.head.insert(0, stub)

    return str(soup)


def inject_header_bar(html: str, path: str, lang: str, cache_age: str = "") -> str:
    """Inject a fixed header bar at the top of every page."""
    soup = BeautifulSoup(html, "lxml")

    flag = {"ru": "🇷🇺", "de": "🇩🇪", "fr": "🇫🇷", "zh": "🇨🇳", "ja": "🇯🇵"}.get(lang, "🌐")
    cache_info = f"• Кэш: {cache_age}" if cache_age else "• Переведено сейчас"

    bar_html = f"""
<div id="bt-bar" style="position:fixed;top:0;left:0;right:0;z-index:2147483647;
     background:#1d4ed8;color:#fff;padding:7px 14px;
     font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     font-size:12px;display:flex;align-items:center;gap:10px;
     box-shadow:0 2px 8px rgba(0,0,0,.4);">
  <b style="font-size:13px">Bloomberg Translator</b>
  <span>{flag} {lang.upper()}</span>
  <span style="opacity:.65">{cache_info}</span>
  <span style="margin-left:auto;display:flex;gap:8px">
    <a href="javascript:history.back()" style="color:#93c5fd;text-decoration:none">← Назад</a>
    <a href="/" style="color:#93c5fd;text-decoration:none">Главная</a>
  </span>
</div>
<div style="height:34px"></div>
"""

    if soup.body:
        bar_tag = BeautifulSoup(bar_html, "lxml").body
        soup.body.insert(0, bar_tag)

    return str(soup)
