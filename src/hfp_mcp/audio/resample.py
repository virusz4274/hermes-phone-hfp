"""Streaming PCM conversion; optional NumPy/libsoxr imports stay lazy."""

from __future__ import annotations
from typing import Any

CHANNELS = 1
PCM_WIDTH = 2


class PcmResampler:
    """Streaming, band-limited PCM resampler backed by libsoxr.

    ``soxr.ResampleStream`` retains its filter history between chunks, avoiding
    the aliasing and discontinuities produced by per-chunk nearest-neighbour
    conversion. Odd trailing bytes are retained for the next call.
    """

    def __init__(self, src_rate: int, dst_rate: int, *, quality: str = "MQ") -> None:
        if src_rate <= 0 or dst_rate <= 0:
            raise ValueError("sample rates must be positive")
        self.src_rate = int(src_rate)
        self.dst_rate = int(dst_rate)
        self.quality = quality
        self._tail = b""
        self._stream: Any = None
        if self.src_rate != self.dst_rate:
            try:
                import soxr
            except ImportError as exc:  # pragma: no cover - availability catches it.
                raise RuntimeError("soxr_unavailable") from exc
            self._stream = soxr.ResampleStream(
                self.src_rate,
                self.dst_rate,
                CHANNELS,
                dtype="int16",
                quality=quality,
            )

    def convert(self, pcm: bytes, *, final: bool = False) -> bytes:
        import numpy as np

        data = self._tail + bytes(pcm or b"")
        even_end = len(data) - (len(data) % PCM_WIDTH)
        self._tail = data[even_end:]
        if even_end:
            samples = np.frombuffer(data[:even_end], dtype="<i2").copy()
        else:
            samples = np.empty(0, dtype=np.int16)

        if self._stream is None:
            result = samples
        else:
            result = self._stream.resample_chunk(samples, last=bool(final))
        if final:
            self._tail = b""
        return result.astype("<i2", copy=False).tobytes()
