"""Exclusive logical ownership of the single physical SCO bridge."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass

from .contracts import ContractError


MEDIA_OWNERS = frozenset({"hermes_classic", "gemini_live", "mcp_client", "one_shot"})


@dataclass(frozen=True)
class MediaLease:
    call_id: str
    owner: str
    stream_id: str
    generation: int


class MediaLeaseManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._lease: MediaLease | None = None
        self._generation = 0

    def acquire(self, call_id: str, owner: str) -> tuple[MediaLease, bool]:
        if owner not in MEDIA_OWNERS:
            raise ContractError("invalid_argument", f"unsupported audio owner: {owner}")
        if not call_id:
            raise ContractError("invalid_argument", "call_id is required")
        with self._lock:
            if self._lease is not None:
                if self._lease.call_id == call_id and self._lease.owner == owner:
                    return self._lease, True
                raise ContractError(
                    "audio_owner_conflict",
                    f"audio is owned by {self._lease.owner} for call {self._lease.call_id}",
                    retryable=True,
                )
            self._generation += 1
            lease = MediaLease(
                call_id=call_id,
                owner=owner,
                stream_id=f"audio-{self._generation}-{uuid.uuid4().hex[:12]}",
                generation=self._generation,
            )
            self._lease = lease
            return lease, False

    def release(self, stream_id: str | None = None) -> MediaLease | None:
        with self._lock:
            if self._lease is None:
                return None
            if stream_id is not None and self._lease.stream_id != stream_id:
                raise ContractError("stream_expired", "audio stream is not the active lease")
            lease = self._lease
            self._lease = None
            return lease

    def release_if(self, expected: MediaLease) -> MediaLease | None:
        """Release only the exact immutable lease generation supplied."""

        with self._lock:
            if self._lease != expected:
                return None
            self._lease = None
            return expected

    def current(self) -> MediaLease | None:
        with self._lock:
            return self._lease
