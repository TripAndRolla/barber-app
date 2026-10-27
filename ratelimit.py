"""Blocco dei tentativi di accesso ripetuti (login admin e login clienti)."""
from __future__ import annotations

import threading
import time

MAX_ATTEMPTS, LOCK_SECONDS = 5, 300

_failed: dict[str, list[float]] = {}
_lock = threading.Lock()


def too_many_attempts(bucket: str, ip: str) -> bool:
    key = f"{bucket}:{ip}"
    with _lock:
        recent = [t for t in _failed.get(key, []) if time.time() - t < LOCK_SECONDS]
        _failed[key] = recent
        return len(recent) >= MAX_ATTEMPTS


def record_failure(bucket: str, ip: str) -> None:
    with _lock:
        _failed.setdefault(f"{bucket}:{ip}", []).append(time.time())


def reset() -> None:
    with _lock:
        _failed.clear()
