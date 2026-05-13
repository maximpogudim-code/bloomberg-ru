"""
Pre-warm cache with Bloomberg's top stories.
Run manually or schedule via cron on the server.
Usage: python warmup.py
"""

import asyncio
import httpx
import os
import sys
from datetime import datetime

BASE_URL = os.getenv("WARMUP_BASE_URL", "http://localhost:8080")
LANG = os.getenv("TARGET_LANG", "ru")

# Bloomberg RSS feeds for latest articles
RSS_FEEDS = [
    "https://feeds.bloomberg.com/markets/news.rss",
    "https://feeds.bloomberg.com/technology/news.rss",
    "https://feeds.bloomberg.com/economics/news.rss",
]


async def get_top_urls(limit: int = 10) -> list[str]:
    """Fetch top article URLs from Bloomberg RSS feeds."""
    import xml.etree.ElementTree as ET

    urls = []
    async with httpx.AsyncClient(timeout=15) as client:
        for feed_url in RSS_FEEDS:
            try:
                resp = await client.get(feed_url)
                root = ET.fromstring(resp.text)
                for item in root.findall(".//item")[:limit // len(RSS_FEEDS) + 1]:
                    link = item.findtext("link", "")
                    if "bloomberg.com" in link:
                        # Extract path from full URL
                        path = link.replace("https://www.bloomberg.com", "").replace(
                            "https://bloomberg.com", ""
                        ).lstrip("/")
                        if path:
                            urls.append(path)
            except Exception as e:
                print(f"  RSS error ({feed_url}): {e}")

    return list(dict.fromkeys(urls))[:limit]  # deduplicate, keep order


async def warm_path(client: httpx.AsyncClient, path: str) -> None:
    """Trigger translation of a single path by requesting it."""
    try:
        # First request triggers background translation
        r1 = await client.get(f"{BASE_URL}/{path}", timeout=5)
        print(f"  → /{path[:60]} [{r1.status_code}] — translating in background...")

        # Wait for translation to complete (poll /api/ready)
        for _ in range(30):  # max 60 seconds
            await asyncio.sleep(2)
            r2 = await client.get(f"{BASE_URL}/api/ready?path={path}", timeout=5)
            if r2.json().get("ready"):
                print(f"  ✓ /{path[:60]} — готово")
                return

        print(f"  ⚠ /{path[:60]} — timeout, продолжаем...")
    except Exception as e:
        print(f"  ✗ /{path[:60]} — ошибка: {e}")


async def main():
    print(f"\n{'='*60}")
    print(f"Bloomberg Proxy Cache Warmup — {datetime.now().strftime('%H:%M %d.%m.%Y')}")
    print(f"Target: {BASE_URL}")
    print(f"{'='*60}\n")

    print("Получаем топ статьи из RSS...")
    urls = await get_top_urls(limit=10)

    if not urls:
        print("Нет статей из RSS. Используем стандартные пути.")
        urls = ["news", "markets", "technology", "economics"]

    print(f"Найдено {len(urls)} статей для предзагрева:\n")

    async with httpx.AsyncClient() as client:
        for i, path in enumerate(urls, 1):
            print(f"[{i}/{len(urls)}]", end=" ")
            await warm_path(client, path)

    print(f"\n{'='*60}")
    print("Предзагрев завершён!")

    # Show cache stats
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(f"{BASE_URL}/api/cache/stats")
            stats = r.json()
            print(f"Кэш: {stats['entries']} страниц, {stats['size_mb']} МБ")
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
