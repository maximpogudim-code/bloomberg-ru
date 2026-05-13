"""Extract translatable text nodes from Bloomberg HTML and apply translations back."""

import re
from bs4 import BeautifulSoup, NavigableString, Tag

# Tags whose text content we never touch
SKIP_TAGS = {
    "script", "style", "noscript", "svg", "code", "pre",
    "math", "iframe", "head", "meta", "link", "title",
}

# Regex: skip pure numbers, tickers, percentages, dates, URLs
_SKIP_PATTERN = re.compile(
    r"^[\s\d\.,:%\+\-\$€£¥]+$"           # pure numbers/symbols
    r"|^[A-Z]{2,6}$"                        # tickers: AAPL, BTC
    r"|^\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}$"  # dates: 01/15/2024
    r"|^https?://"                           # URLs
    r"|^[\W\s]{0,3}$",                      # whitespace / punctuation only
)


def extract(html: str) -> list[dict]:
    """
    Parse HTML, walk all visible text nodes, tag them with data-bt-id,
    return list of {id, text} for translation.
    Returns modified HTML (with data-bt-id attrs) and the node list.
    """
    soup = BeautifulSoup(html, "lxml")

    nodes: list[dict] = []
    counter = [0]

    def walk(tag: Tag) -> None:
        if not isinstance(tag, Tag):
            return
        if tag.name in SKIP_TAGS:
            return

        for child in list(tag.children):
            if isinstance(child, NavigableString):
                text = str(child).strip()
                if len(text) >= 10 and not _SKIP_PATTERN.match(text):
                    node_id = f"bt{counter[0]}"
                    counter[0] += 1
                    # Wrap the text node in a span with our ID
                    new_tag = soup.new_tag("span", **{"data-bt-id": node_id})
                    new_tag.string = str(child)
                    child.replace_with(new_tag)
                    nodes.append({"id": node_id, "text": text})
            elif isinstance(child, Tag):
                walk(child)

    walk(soup.body or soup)

    return str(soup), nodes


def apply(html: str, translations: dict[str, str]) -> str:
    """
    Given HTML with data-bt-id spans and a {id→translated_text} map,
    replace original text with translations.
    """
    soup = BeautifulSoup(html, "lxml")

    for span in soup.find_all("span", attrs={"data-bt-id": True}):
        node_id = span.get("data-bt-id")
        if node_id in translations:
            span.string = translations[node_id]
        # Unwrap the helper span, keep content
        span.unwrap()

    return str(soup)


def make_batches(nodes: list[dict], max_chars: int = 4000) -> list[list[dict]]:
    """Group nodes into batches by character count for efficient API calls."""
    batches: list[list[dict]] = []
    current: list[dict] = []
    current_len = 0

    for node in nodes:
        node_len = len(node["text"]) + 20  # overhead for id line
        if current_len + node_len > max_chars and current:
            batches.append(current)
            current = []
            current_len = 0
        current.append(node)
        current_len += node_len

    if current:
        batches.append(current)

    return batches
