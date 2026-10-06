"""Speech: faster-whisper for hearing, Kokoro for speaking. Both run locally on the CPU."""
import io
import os
import re
import tempfile
import threading
from pathlib import Path

import numpy as np
import soundfile as sf

KOKORO_DIR = Path(os.environ.get("MENTOR_KOKORO_DIR", Path.home() / ".local" / "share" / "study-mentor" / "tts"))
WHISPER_MODEL = os.environ.get("MENTOR_WHISPER", "base.en")  # small.en is more accurate but about 3x slower on a laptop CPU
DEFAULT_VOICE = os.environ.get("MENTOR_VOICE", "bm_george")

VOICES = {
    "bm_george": "George (British, steady)",
    "bm_lewis": "Lewis (British, warm)",
    "bm_daniel": "Daniel (British, crisp)",
    "bf_emma": "Emma (British, calm)",
    "bf_isabella": "Isabella (British, bright)",
    "am_michael": "Michael (American, direct)",
    "af_heart": "Heart (American, warm)",
}

STT_HINT = (
    "AWS Solutions Architect Professional. Organizations, SCP, Control Tower, Transit Gateway, Direct Connect, PrivateLink, "
    "Route 53, VPC, IAM, S3, EC2, ECS, EKS, Fargate, DynamoDB, Aurora, EventBridge, SQS, SNS, CloudWatch, X-Ray, RTO, RPO. "
    "LSAT, logical reasoning, reading comprehension, necessary, sufficient, conditional, assumption, flaw, conclusion, premise."
)

# ---------- text prep for the voice ----------
_DIGITS = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine"}
_KEEP = {  # all-caps words that should be read as words, not spelled
    "LSAT": "L-sat", "NAT": "nat", "SAP": "S A P", "SRE": "S R E", "AMI": "A M I", "SLA": "S L A", "NACL": "nackle",
    "FIFO": "fy-foh", "SaaS": "sass", "JSON": "jason", "YAML": "yamel", "SQL": "sequel", "ARN": "A R N", "GUI": "gooey",
    "WAF": "waff", "KMS": "K M S", "PDF": "P D F", "OK": "okay", "TTL": "T T L", "CIDR": "cider", "LAWHUB": "law hub",
}
_ALNUM = re.compile(r"\b([A-Z]{1,5})(\d{1,3})\b")        # S3, EC2, SQS2
_CAPS = re.compile(r"\b[A-Z]{2,6}s?\b")


def spoken_text(text: str) -> str:
    t = text.replace("&", " and ").replace("/", " slash ").replace("->", " to ").replace("→", " to ")
    t = re.sub(r"`|\*|_{1,2}|#", "", t)

    def alnum(m):
        letters = " ".join(m.group(1))
        digits = " ".join(_DIGITS[d] for d in m.group(2))
        return f"{letters} {digits}"

    t = _ALNUM.sub(alnum, t)

    def caps(m):
        w = m.group(0)
        base = w[:-1] if w.endswith("s") and len(w) > 2 else w
        if base in _KEEP:
            return _KEEP[base] + ("s" if base != w else "")
        return " ".join(base) + ("s" if base != w else "")

    t = _CAPS.sub(caps, t)
    t = re.sub(r"\bvs\.?(?=\s|$)", "versus", t, flags=re.I)
    t = re.sub(r"\be\.g\.", "for example", t, flags=re.I)
    t = re.sub(r"\bi\.e\.", "that is", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# ---------- TTS ----------
_kokoro = None
_kokoro_lock = threading.Lock()


def _get_kokoro():
    global _kokoro
    with _kokoro_lock:
        if _kokoro is None:
            from kokoro_onnx import Kokoro

            _kokoro = Kokoro(str(KOKORO_DIR / "kokoro-v1.0.onnx"), str(KOKORO_DIR / "voices-v1.0.bin"))
        return _kokoro


def synth(text: str, voice: str | None = None, speed: float = 1.0) -> bytes:
    """Speak `text`; returns WAV bytes."""
    voice = voice if voice in VOICES else DEFAULT_VOICE
    k = _get_kokoro()
    lang = "en-gb" if voice.startswith("b") else "en-us"
    with _kokoro_lock:  # onnx session is not safe to share across threads
        samples, rate = k.create(spoken_text(text), voice=voice, speed=speed, lang=lang)
    buf = io.BytesIO()
    sf.write(buf, samples, rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


# ---------- STT ----------
_whisper = None
_whisper_lock = threading.Lock()


def _get_whisper():
    global _whisper
    with _whisper_lock:
        if _whisper is None:
            from faster_whisper import WhisperModel

            _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", cpu_threads=min(6, os.cpu_count() or 4))
        return _whisper


def _decode(audio_bytes: bytes, suffix: str) -> np.ndarray:
    """Decode any browser audio (webm/ogg/wav) to 16 kHz mono float32 with PyAV."""
    import av

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as f:
        f.write(audio_bytes)
        f.flush()
        chunks = []
        with av.open(f.name) as container:
            resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
            for frame in container.decode(audio=0):
                for out in resampler.resample(frame):
                    chunks.append(out.to_ndarray().reshape(-1))
            for out in resampler.resample(None):
                chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def transcribe(audio_bytes: bytes, suffix: str = ".webm") -> str:
    model = _get_whisper()
    audio = _decode(audio_bytes, suffix)
    if audio.size < 1600:  # under 0.1 s
        return ""
    if True:
        segs, _info = model.transcribe(
            audio,
            language="en",
            beam_size=1,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 350},
            initial_prompt=STT_HINT,
            condition_on_previous_text=False,
        )
        text = " ".join(s.text.strip() for s in segs).strip()
    return text


def warm_up():
    """Load both models once at startup so the first turn is not slow."""
    try:
        _get_kokoro()
    except Exception as e:  # noqa: BLE001
        print("kokoro warm-up failed:", e)
    try:
        _get_whisper()
    except Exception as e:  # noqa: BLE001
        print("whisper warm-up failed:", e)
