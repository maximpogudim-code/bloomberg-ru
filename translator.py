"""Batch translation: Claude primary, Groq fallback."""

import json
import os
from dotenv import load_dotenv

load_dotenv()

LANGUAGE_NAMES = {
    "ru": "Russian", "de": "German", "fr": "French",
    "es": "Spanish", "zh": "Chinese (Simplified)",
    "ja": "Japanese", "ar": "Arabic",
}

# Batch translation prompt template — {lang_name} is the only placeholder
_BATCH_SYSTEM_TPL = (
    "You are a professional financial translator specializing in Bloomberg news. "
    "You will receive a JSON array of text nodes. "
    "Translate each 'text' value to {lang_name}. "
    "Rules: preserve numbers, percentages, company tickers (AAPL, BTC), "
    "proper nouns, HTML entities exactly as-is. Use formal register. "
    "Return ONLY a JSON object mapping each 'id' to its translated text. "
    "Example input: [{{\"id\":\"bt0\",\"text\":\"Fed raises rates\"}}] "
    "Example output: {{\"bt0\":\"ФРС повышает ставки\"}}"
)


def _build_system(lang_name: str) -> str:
    return _BATCH_SYSTEM_TPL.format(lang_name=lang_name)


def _build_user_message(nodes: list[dict]) -> str:
    return json.dumps(nodes, ensure_ascii=False)


def _parse_response(text: str) -> dict[str, str]:
    text = text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        import re
        # Try extracting first JSON structure (object or array)
        m = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
        if not m:
            return {}
        parsed = json.loads(m.group())

    # LLMs sometimes return an array [{id, text}] instead of {id: text}
    if isinstance(parsed, list):
        return {item["id"]: item.get("text", item.get("translated", "")) for item in parsed if "id" in item}
    if isinstance(parsed, dict):
        return parsed
    return {}


async def _translate_batch_claude(nodes: list[dict], target_lang: str) -> dict[str, str]:
    import anthropic
    lang_name = LANGUAGE_NAMES.get(target_lang, target_lang)
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8192,
        system=_build_system(lang_name),
        messages=[{"role": "user", "content": _build_user_message(nodes)}],
    )
    return _parse_response(msg.content[0].text)


async def _translate_batch_groq(nodes: list[dict], target_lang: str) -> dict[str, str]:
    import groq as groq_lib
    lang_name = LANGUAGE_NAMES.get(target_lang, target_lang)
    client = groq_lib.Groq(api_key=os.getenv("GROQ_API_KEY"))
    resp = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": _build_system(lang_name)},
            {"role": "user", "content": _build_user_message(nodes)},
        ],
        max_tokens=8192,
        temperature=0.05,
    )
    return _parse_response(resp.choices[0].message.content)


async def _translate_batch_ollama(nodes: list[dict], target_lang: str) -> dict[str, str]:
    """Translate via local Ollama — simple numbered-line format, much faster than JSON chat."""
    import httpx, asyncio
    lang_name = LANGUAGE_NAMES.get(target_lang, target_lang)

    lines = [f"{i+1}. {n['text']}" for i, n in enumerate(nodes)]
    prompt = (
        f"Translate each numbered line to {lang_name}. "
        "Keep numbers. Keep tickers, company names, percentages unchanged. "
        "Output ONLY the translated lines, nothing else.\n\n"
        + "\n".join(lines)
    )

    def _call():
        with httpx.Client(timeout=120) as client:
            resp = client.post(
                "http://localhost:11434/api/generate",
                json={"model": "qwen2.5:7b", "prompt": prompt, "stream": False},
            )
            resp.raise_for_status()
            return resp.json().get("response", "")

    loop = asyncio.get_event_loop()
    raw = await loop.run_in_executor(None, _call)

    result = {}
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        dot = line.find(".")
        if dot > 0 and line[:dot].isdigit():
            idx = int(line[:dot]) - 1
            if 0 <= idx < len(nodes):
                result[nodes[idx]["id"]] = line[dot + 1:].strip()
    return result


async def _translate_batch_google(nodes: list[dict], target_lang: str) -> dict[str, str]:
    """Free Google Translate — parallel async calls, one per node."""
    import asyncio
    from deep_translator import GoogleTranslator

    translator = GoogleTranslator(source="auto", target=target_lang)
    loop = asyncio.get_event_loop()

    async def _one(node: dict) -> tuple[str, str]:
        try:
            t = await loop.run_in_executor(None, translator.translate, node["text"])
            return node["id"], t or node["text"]
        except Exception:
            return node["id"], node["text"]

    pairs = await asyncio.gather(*[_one(n) for n in nodes])
    return dict(pairs)


async def translate_batch(nodes: list[dict], target_lang: str = "ru") -> dict[str, str]:
    """Translate a batch of {id, text} nodes → {id: translated} dict."""
    if not nodes:
        return {}

    # Build the fallback chain, skipping LLM providers whose keys are missing
    chain = []
    if os.getenv("ANTHROPIC_API_KEY"):
        chain.append(_translate_batch_claude)
    if os.getenv("GROQ_API_KEY"):
        chain.append(_translate_batch_groq)
    # Google Translate (free, no key required) is always available
    chain.append(_translate_batch_google)
    # Ollama is optional and local — keep as last resort
    chain.append(_translate_batch_ollama)

    for fn in chain:
        try:
            result = await fn(nodes, target_lang)
            if result:
                return result
        except Exception:
            continue
    return {}


async def translate_all_batches(
    batches: list[list[dict]], target_lang: str = "ru"
) -> dict[str, str]:
    """Translate all batches sequentially and merge results."""
    import asyncio
    all_translations: dict[str, str] = {}
    for batch in batches:
        result = await translate_batch(batch, target_lang)
        all_translations.update(result)
    return all_translations
