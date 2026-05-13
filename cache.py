"""Two-level diskcache: translated HTML pages + raw text translations."""

import hashlib
import os
from typing import Optional


_cache = None


def _get_cache():
    global _cache
    if _cache is None:
        import diskcache
        cache_dir = os.getenv("CACHE_DIR", "./cache_data")
        _cache = diskcache.Cache(cache_dir, size_limit=2 ** 30)  # 1 GB max
    return _cache


def _key(prefix: str, *parts: str) -> str:
    raw = ":".join(parts)
    return f"{prefix}:{hashlib.sha256(raw.encode()).hexdigest()[:20]}"


# ── Page cache (full translated HTML) ──────────────────────────────────────

def get_page(path: str, lang: str) -> Optional[str]:
    try:
        result = _get_cache().get(_key("page", path, lang))
        return result
    except Exception:
        return None


def set_page(path: str, lang: str, html: str, ttl: Optional[int] = None) -> None:
    ttl = ttl or int(os.getenv("CACHE_PAGE_TTL", 14400))
    try:
        _get_cache().set(_key("page", path, lang), html, expire=ttl)
    except Exception:
        pass


def get_page_age(path: str, lang: str) -> Optional[str]:
    """Return human-readable cache age string, or None if not cached."""
    import time
    try:
        c = _get_cache()
        k = _key("page", path, lang)
        # diskcache stores expire time; we check if key exists + its age
        if k not in c:
            return None
        item = c.peekitem(k, expire_time=True)
        if item is None:
            return None
        _, expire = item
        if expire is None:
            return "постоянно"
        remaining = int(expire - time.time())
        ttl = int(os.getenv("CACHE_PAGE_TTL", 14400))
        age_sec = ttl - remaining
        if age_sec < 60:
            return "только что"
        elif age_sec < 3600:
            return f"{age_sec // 60} мин назад"
        else:
            return f"{age_sec // 3600} ч назад"
    except Exception:
        return None


# ── Stats ───────────────────────────────────────────────────────────────────

def stats() -> dict:
    try:
        c = _get_cache()
        return {
            "entries": len(c),
            "size_mb": round(c.volume() / 1_048_576, 1),
        }
    except Exception:
        return {"entries": 0, "size_mb": 0}


def clear() -> int:
    try:
        c = _get_cache()
        n = len(c)
        c.clear()
        return n
    except Exception:
        return 0
