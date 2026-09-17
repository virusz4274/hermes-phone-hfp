"""Hermes speech-provider shim. All PCM is mono s16le, 8 kHz."""

import json
import subprocess
import tempfile
import wave
from pathlib import Path


def transcribe_pcm(pcm: bytes) -> str:
    from tools.transcription_tools import transcribe_audio

    with tempfile.TemporaryDirectory(prefix="hfp-stt-") as directory:
        path = Path(directory) / "speech.wav"
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(pcm)
        result = transcribe_audio(str(path))
    if not result.get("success"):
        raise RuntimeError("Hermes transcription failed")
    return str(result.get("transcript") or "")


def synthesize_pcm(text: str) -> bytes:
    from tools.tts_tool import text_to_speech_tool

    result = text_to_speech_tool(text=text)
    result = json.loads(result) if isinstance(result, str) else result
    if not result.get("success"):
        raise RuntimeError("Hermes speech synthesis failed")
    output = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            result["file_path"],
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-ac",
            "1",
            "-ar",
            "8000",
            "-",
        ],
        capture_output=True,
        check=True,
        timeout=60,
    )
    return output.stdout
