"""Local, bounded outgoing openings; synthesis never runs on the call path."""
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
import io
import math
import struct
import wave


@dataclass(frozen=True)
class Opening:
    pcm: bytes
    text: str = ""
    mode: str = "spoken"


@lru_cache(maxsize=3)
def outgoing_opening(mode: str = "spoken") -> Opening | None:
    mode = mode.strip().lower()
    if mode == "off":
        return None
    if mode == "chime":
        # A short, soft two-tone cue with fades to avoid clicks.
        pcm = bytearray()
        for frequency in (660, 880):
            for i in range(1200):
                fade = min(1, i / 160, (1199-i) / 160)
                pcm.extend(struct.pack('<h', round(3000 * fade * math.sin(2*math.pi*frequency*i/8000))))
        return Opening(bytes(pcm), mode="chime")
    if mode != "spoken":
        raise ValueError("HFP_GEMINI_OUTGOING_GREETING must be spoken, chime or off")
    data = files('hfp_mcp.audio').joinpath('assets/outgoing-hello.wav').read_bytes()
    with wave.open(io.BytesIO(data), 'rb') as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 8000, 'NONE'):
            raise ValueError("Cached outgoing greeting must be mono 8 kHz 16-bit PCM WAV")
        if not 0 < audio.getnframes() <= 6*8000:
            raise ValueError("Cached outgoing greeting must be at most six seconds")
        pcm = audio.readframes(audio.getnframes())
        if len(pcm) != audio.getnframes()*2:
            raise ValueError("Cached outgoing greeting is truncated")
    return Opening(pcm, "Hello, this is Hermes, an AI assistant.")


class OpeningInterruption:
    """Consecutive microphone activity cuts local playback; not consent detection."""
    def __init__(self):
        self.loud_ms = 0

    def observe(self, pcm: bytes) -> bool:
        if not pcm or len(pcm) % 2:
            return False
        count = len(pcm)//2
        rms = math.isqrt(sum(s[0]**2 for s in struct.iter_unpack('<h', pcm)) // count)
        self.loud_ms = self.loud_ms + count*1000/8000 if rms >= 400 else 0
        return self.loud_ms >= 80
