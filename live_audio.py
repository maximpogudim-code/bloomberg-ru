"""Bloomberg TV live audio → transcribe EN → translate RU → async callback."""

import asyncio
import os
import re
import shutil
import string
import struct
import tempfile
import time
import wave

_MODEL = None  # singleton WhisperModel, loaded on first use
_STREAM_URL_CACHE: dict = {"url": None, "ts": 0}
_STREAM_URL_TTL = 1800  # refresh HLS URL every 30 min

# ---------------------------------------------------------------------------
# Tool resolution
# ---------------------------------------------------------------------------

def _resolve_ffmpeg() -> str | None:
    """Return path to ffmpeg executable, or None if not found anywhere."""
    # 1. system PATH
    found = shutil.which("ffmpeg")
    if found:
        return found
    # 2. ~/bin/ffmpeg (common local install)
    local = os.path.expanduser("~/bin/ffmpeg")
    if os.path.isfile(local):
        return local
    # 3. imageio_ffmpeg bundled binary (optional dep)
    try:
        import imageio_ffmpeg  # type: ignore
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    return None


_FFMPEG: str | None = _resolve_ffmpeg()


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

# ---------------------------------------------------------------------------
# Audio chunk parameters
# ---------------------------------------------------------------------------

# 7 seconds of 16 kHz mono 16-bit PCM
_CHUNK_SECONDS = 7
_SAMPLE_RATE = 16000
_BYTES_PER_SAMPLE = 2  # s16le
_CHUNK_BYTES = _CHUNK_SECONDS * _SAMPLE_RATE * _BYTES_PER_SAMPLE  # 224 000 bytes

# ---------------------------------------------------------------------------
# Deduplication helpers
# ---------------------------------------------------------------------------

# Whisper hallucination stop-list (lowercase, no punctuation)
_HALLUCINATION_PHRASES = frozenset({
    "thank you",
    "thanks for watching",
    "thanks for watching the video",
    "you",
    "bye",
    "bye bye",
    "please subscribe",
    "subscribe",
    "like and subscribe",
    "i",
    ".",
    "",
    "the",
})


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = text.lower()
    text = text.translate(str.maketrans("", "", string.punctuation))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _is_hallucination(norm: str) -> bool:
    """Return True if the normalised text looks like a Whisper hallucination."""
    if not norm:
        return True
    if len(norm) < 15:
        return True
    words = norm.split()
    if len(words) < 4:
        return True
    if norm in _HALLUCINATION_PHRASES:
        return True
    return False


def _jaccard(a: str, b: str) -> float:
    """Jaccard similarity on word sets."""
    sa = set(a.split())
    sb = set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _strip_prefix_overlap(prev: str, curr: str, min_overlap: int = 3) -> str:
    """
    If the last `min_overlap`+ words of `prev` appear verbatim at the start
    of `curr`, remove that repeated prefix from `curr`.
    """
    prev_words = prev.split()
    curr_words = curr.split()
    # Try progressively shorter suffixes of prev (from longest possible)
    max_check = min(len(prev_words), len(curr_words))
    for length in range(max_check, min_overlap - 1, -1):
        suffix = prev_words[-length:]
        if curr_words[:length] == suffix:
            trimmed = curr_words[length:]
            return " ".join(trimmed)
    return curr

# ---------------------------------------------------------------------------
# Whisper / WAV helpers
# ---------------------------------------------------------------------------


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
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper is not installed; live transcription unavailable"
            ) from exc
        _MODEL = WhisperModel(
            "tiny", device="cpu", compute_type="int8",
            download_root=_whisper_cache_dir(),
        )
    return _MODEL


def _pcm_to_wav(pcm_data: bytes, path: str) -> None:
    """Write raw s16le 16 kHz mono PCM bytes to a proper WAV file."""
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(_BYTES_PER_SAMPLE)
        wf.setframerate(_SAMPLE_RATE)
        wf.writeframes(pcm_data)

# ---------------------------------------------------------------------------
# Stream URL cache
# ---------------------------------------------------------------------------


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
        lines = [ln.strip() for ln in result.stdout.strip().splitlines() if ln.strip()]
        return lines[0] if lines else None

    url = await loop.run_in_executor(None, _fetch)
    if url:
        _STREAM_URL_CACHE["url"] = url
        _STREAM_URL_CACHE["ts"] = now
    return url


def _invalidate_stream_url() -> None:
    _STREAM_URL_CACHE["ts"] = 0

# ---------------------------------------------------------------------------
# Long-lived ffmpeg process
# ---------------------------------------------------------------------------


async def _start_ffmpeg(stream_url: str) -> asyncio.subprocess.Process | None:
    """
    Start a single long-lived ffmpeg process that reads the HLS stream and
    writes raw s16le 16 kHz mono PCM to stdout indefinitely.
    Returns the Process object, or None if ffmpeg is not available.
    """
    if not _FFMPEG or not os.path.isfile(_FFMPEG):
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            _FFMPEG,
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", stream_url,
            "-ar", str(_SAMPLE_RATE),
            "-ac", "1",
            "-f", "s16le",
            "-",  # write PCM to stdout
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return proc
    except Exception:
        return None


async def _read_chunk(proc: asyncio.subprocess.Process) -> bytes | None:
    """
    Read exactly _CHUNK_BYTES from ffmpeg stdout, potentially in multiple
    read() calls (ffmpeg may deliver data in small bursts over HLS).
    Returns None if the process died or stdout was closed before the chunk
    could be filled.
    """
    buf = bytearray()
    remaining = _CHUNK_BYTES
    while remaining > 0:
        try:
            data = await asyncio.wait_for(
                proc.stdout.read(min(remaining, 32768)),
                timeout=_CHUNK_SECONDS * 3,  # generous: 3× real-time
            )
        except asyncio.TimeoutError:
            return None
        if not data:
            # EOF — ffmpeg process finished or stream died
            return None
        buf.extend(data)
        remaining -= len(data)
    return bytes(buf)

# ---------------------------------------------------------------------------
# Transcription / translation
# ---------------------------------------------------------------------------


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

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def live_dubbing_loop(video_id: str, callback) -> None:
    """
    Continuously: get HLS URL → stream PCM via one long-lived ffmpeg process →
    slice into 7-second chunks → transcribe EN → deduplicate → translate RU →
    callback(ru_text).

    Runs until cancelled.  `callback` is an async function receiving a single str.
    Gracefully degrades if faster-whisper, yt-dlp, or ffmpeg are unavailable.
    """
    # Pre-check: faster-whisper must be importable
    try:
        from faster_whisper import WhisperModel  # noqa: F401
    except ImportError:
        return  # silently do nothing

    # Pre-check: ffmpeg must be available
    if not _FFMPEG:
        return  # silently do nothing

    loop = asyncio.get_event_loop()
    consecutive_failures = 0
    ffmpeg_proc: asyncio.subprocess.Process | None = None

    # Deduplication state
    prev_norm: str = ""  # normalised text of last accepted phrase

    try:
        while True:
            try:
                # ----------------------------------------------------------------
                # Ensure we have a running ffmpeg process
                # ----------------------------------------------------------------
                if ffmpeg_proc is None or ffmpeg_proc.returncode is not None:
                    # Process not running — get a (possibly fresh) stream URL
                    stream_url = await _get_stream_url(video_id)
                    if not stream_url:
                        consecutive_failures += 1
                        await asyncio.sleep(min(5 * consecutive_failures, 30))
                        continue

                    ffmpeg_proc = await _start_ffmpeg(stream_url)
                    if ffmpeg_proc is None:
                        # ffmpeg binary vanished mid-run
                        return

                # ----------------------------------------------------------------
                # Read one chunk of PCM
                # ----------------------------------------------------------------
                pcm = await _read_chunk(ffmpeg_proc)
                if pcm is None:
                    # Stream died — kill process, force URL refresh, back off
                    try:
                        ffmpeg_proc.kill()
                    except Exception:
                        pass
                    ffmpeg_proc = None
                    _invalidate_stream_url()
                    consecutive_failures += 1
                    await asyncio.sleep(min(5 * consecutive_failures, 30))
                    continue

                consecutive_failures = 0

                # ----------------------------------------------------------------
                # Write PCM → WAV → transcribe
                # ----------------------------------------------------------------
                tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                tmp.close()
                try:
                    _pcm_to_wav(pcm, tmp.name)
                    en_text = await loop.run_in_executor(None, _transcribe_sync, tmp.name)
                finally:
                    try:
                        os.unlink(tmp.name)
                    except OSError:
                        pass

                if not en_text:
                    continue

                # ----------------------------------------------------------------
                # Deduplication & hallucination filter
                # ----------------------------------------------------------------
                norm = _normalize(en_text)

                # 1. Hallucination filter
                if _is_hallucination(norm):
                    continue

                # 2. Exact duplicate
                if norm == prev_norm:
                    continue

                # 3. Near-duplicate (Jaccard > 0.6)
                if prev_norm and _jaccard(norm, prev_norm) > 0.6:
                    continue

                # 4. Trim verbatim prefix overlap with previous chunk.
                # Compare in normalised space, but slice the ORIGINAL text so
                # case/punctuation reach the translator intact.
                if prev_norm:
                    trimmed_norm = _strip_prefix_overlap(prev_norm, norm)
                    if not trimmed_norm:
                        continue
                    removed = len(norm.split()) - len(trimmed_norm.split())
                    if removed > 0:
                        en_text = " ".join(en_text.split()[removed:])
                        if _is_hallucination(_normalize(en_text)):
                            continue

                # Keep the FULL pre-trim norm as the reference for the next chunk
                prev_norm = norm

                # ----------------------------------------------------------------
                # Translate and deliver
                # ----------------------------------------------------------------
                ru_text = await loop.run_in_executor(None, _translate_sync, en_text)
                await callback(ru_text)

            except asyncio.CancelledError:
                raise
            except Exception:
                consecutive_failures += 1
                await asyncio.sleep(min(5 * consecutive_failures, 30))

    except asyncio.CancelledError:
        pass
    finally:
        # Clean up the long-lived process on exit
        if ffmpeg_proc is not None and ffmpeg_proc.returncode is None:
            try:
                ffmpeg_proc.kill()
                await ffmpeg_proc.wait()
            except Exception:
                pass
