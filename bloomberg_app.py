"""Bloomberg Live — настольное приложение с переводом на русский в реальном времени."""

import asyncio
from playwright.async_api import async_playwright

START_URL = "https://www.bloomberg.com/live"

_TRANSLATE_JS = """
(function() {
  if (window.__bloombergRuActive) return;
  window.__bloombergRuActive = true;

  const done = new WeakSet();
  let pending = false;
  let debounce;

  const SKIP_TAGS = new Set(['SCRIPT','STYLE','NOSCRIPT','CODE','SVG','IFRAME','INPUT','TEXTAREA']);

  function getNodes() {
    const nodes = [];
    const walker = document.createTreeWalker(
      document.body, NodeFilter.SHOW_TEXT,
      { acceptNode: function(n) {
          const t = n.textContent.trim();
          if (t.length < 6) return NodeFilter.FILTER_SKIP;
          if (done.has(n)) return NodeFilter.FILTER_SKIP;
          const p = n.parentElement;
          if (!p || SKIP_TAGS.has(p.tagName)) return NodeFilter.FILTER_SKIP;
          // Skip numbers-only, URLs, tickers
          if (/^[\\d\\s%$€£+\\-.,:/]+$/.test(t)) return NodeFilter.FILTER_SKIP;
          if (/^[A-Z]{2,5}$/.test(t)) return NodeFilter.FILTER_SKIP;
          return NodeFilter.FILTER_ACCEPT;
      }}
    );
    let n;
    while ((n = walker.nextNode()) && nodes.length < 60) nodes.push(n);
    return nodes;
  }

  async function translatePage() {
    if (pending) return;
    pending = true;
    try {
      const nodes = getNodes();
      if (!nodes.length) { pending = false; return; }
      const texts = nodes.map(function(n) { return n.textContent.trim(); });
      const translated = await window.__translateBatch(texts);
      nodes.forEach(function(n, i) {
        if (translated[i] && translated[i] !== texts[i]) {
          n.textContent = translated[i];
          done.add(n);
        }
      });
    } catch(e) {}
    pending = false;
    if (getNodes().length > 0) setTimeout(translatePage, 2500);
  }

  // First pass after DOM settles
  setTimeout(translatePage, 2000);

  // Watch live content updates
  new MutationObserver(function() {
    clearTimeout(debounce);
    debounce = setTimeout(translatePage, 1200);
  }).observe(document.body, { childList: true, subtree: true });
})();
"""


async def _translate_handler(source, texts: list) -> list:
    """Called from JS as window.__translateBatch(texts). Returns Russian strings."""
    from deep_translator import GoogleTranslator
    loop = asyncio.get_event_loop()

    async def _one(text: str) -> str:
        if not text or len(text) < 6:
            return text
        try:
            result = await loop.run_in_executor(
                None,
                lambda t=text: GoogleTranslator(source="auto", target="ru").translate(t),
            )
            return result or text
        except Exception:
            return text

    return list(await asyncio.gather(*[_one(t) for t in texts]))


async def main():
    print("Запуск Bloomberg Live на русском...")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            args=[
                "--window-size=1440,900",
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            locale="ru-RU",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )

        # Expose Python translation function — available as window.__translateBatch in every page
        await context.expose_binding("__translateBatch", _translate_handler)

        # Inject translation JS on every page load (including navigations)
        await context.add_init_script(_TRANSLATE_JS)

        page = await context.new_page()
        await page.goto(START_URL, wait_until="domcontentloaded", timeout=30000)

        print("Bloomberg RU открыт. Перевод активен. Закройте окно браузера для выхода.")

        # Keep running until the browser window is closed
        try:
            await page.wait_for_event("close", timeout=0)
        except Exception:
            pass
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
