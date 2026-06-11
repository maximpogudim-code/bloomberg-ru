"""Bloomberg TV live audio → transcribe EN → translate RU → async callback."""

import asyncio
import os
import shutil
import tempfile
import time

_MODEL = None  # singleton WhisperModel, loaded on first use
_STREAM_URL_CACHE: dict = {"url": None, "ts": 0}
_STREAM_URL_TTL = 1800  # refresh HLS URL every 30 min

# Resolve tool paths at import time
_FFMPEG = shutil.which("ffmpeg") or os.path.expanduser("~/bin/ffmpeg")

def _find_yt_dlp() -> str:
    """Prefer venv binary, then PATH."""
    _here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(_here, ".venv", "bin", "yt-dlp"),
        shutil.which("yt-dlp") or "",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return "yt-dlp"

_YT_DLP = _find_yt_dlp()


def _whisper_cache_dir() -> str:
    """Writable cache dir for the Whisper model — Docker /app if present, else local."""
    docker_path = "/app/cache_data/whisper"
    if os.path.isdir("/app/cache_data"):
        os.makedirs(docker_path, exist_ok=True)
        return docker_path
    local = os.path.expanduser("~/.cache/bloomberg-whisper")
    os.makedirs(local, exist_ok=True)
    return local


def _get_model():
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel
        _MODEL = WhisperModel(
            "tiny", device="cpu", compute_type="int8",
            download_root=_whisper_cache_dir(),
        )
    return _MODEL


async def _get_stream_url(video_id: str) -> str | None:
    now = time.time()
    if _STREAM_URL_CACHE["url"] and _STREAM_URL_CACHE["ts"] + _STREAM_URL_TTL > now:
        return _STREAM_URL_CACHE["url"]

    loop = asyncio.get_event_loop()

    def _fetch():
        import subprocess
        result = subprocess.run(
            [
                _YT_DLP, "--no-warnings", "-f", "bestaudio/worst",
                "--get-url", f"https://www.youtube.com/watch?v={video_id}",
            ],
            capture_output=True, text=True, timeout=40,
        )
        lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
        return lines[0] if lines else None

    url = await loop.run_in_executor(None, _fetch)
    if url:
        _STREAM_URL_CACHE["url"] = url
        _STREAM_URL_CACHE["ts"] = now
    return url


async def _capture_chunk(stream_url: str, duration: int = 3) -> str | None:
    """Capture `duration` seconds of audio from HLS stream → temp WAV. None on failure."""
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    try:
        proc = await asyncio.create_subprocess_exec(
            _FFMPEG, "-y",
            "-i", stream_url,
            "-t", str(duration),
            "-ar", "16000", "-ac", "1", "-f", "wav", tmp.name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=duration + 10)
        except asyncio.TimeoutError:
            proc.kill()
            return None
    except Exception:
        return None

    if os.path.exists(tmp.name) and os.path.getsize(tmp.name) > 8_000:
        return tmp.name
    try:
        os.unlink(tmp.name)
    except OSError:
        pass
    return None


def _transcribe_sync(wav_path: str) -> str:
    model = _get_model()
    segments, _ = model.transcribe(
        wav_path, beam_size=1, language="en", vad_filter=True
    )
    return " ".join(s.text for s in segments).strip()


def _translate_sync(text: str) -> str:
    from deep_translator import GoogleTranslator
    try:
        result = GoogleTranslator(source="en", target="ru").translate(text)
        return result or text
    except Exception:
        return text


async def live_dubbing_loop(video_id: str, callback) -> None:
    """
    Continuously: get HLS URL → capture 10s WAV → transcribe EN → translate RU → callback(ru_text).
    Runs until cancelled. `callback` is an async function receiving a single str.
    """
    loop = asyncio.get_event_loop()
    consecutive_failures = 0

    while True:
        try:
            stream_url = await _get_stream_url(video_id)
            if not stream_url:
                await asyncio.sleep(20)
                consecutive_failures += 1
                continue

            wav = await _capture_chunk(stream_url)
            if not wav:
                # HLS URL likely expired — force refresh next time
                _STREAM_URL_CACHE["ts"] = 0
                consecutive_failures += 1
                await asyncio.sleep(min(5 * consecutive_failures, 30))
                continue

            consecutive_failures = 0

            en_text = await loop.run_in_executor(None, _transcribe_sync, wav)
            try:
                os.unlink(wav)
            except OSError:
                pass

            if en_text and len(en_text) > 5:
                ru_text = await loop.run_in_executor(None, _translate_sync, en_text)
                await callback(ru_text)

        except asyncio.CancelledError:
            return
        except Exception:
            consecutive_failures += 1
            await asyncio.sleep(min(5 * consecutive_failures, 30))
