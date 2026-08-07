from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


def _positive_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


class SignalingBusyError(RuntimeError):
    pass


class UpstreamSignalingError(RuntimeError):
    def __init__(self, status_code: int, detail: str = ""):
        super().__init__(f"upstream realtime signaling failed with HTTP {status_code}")
        self.status_code = status_code
        self.detail = detail


@dataclass
class _AttemptChain:
    identity_key: str
    expires_at: float
    excluded_tokens: set[str] = field(default_factory=set)
    last_token: str | None = None


class RealtimeSignalingGuard:
    """In-process protection for the short SDP signaling critical path.

    Media does not pass through this process, so this class deliberately limits
    signaling requests rather than pretending to track active WebRTC sessions.
    """

    def __init__(
        self,
        *,
        max_concurrency: int | None = None,
        rate_per_minute: int | None = None,
        attempt_ttl_seconds: int | None = None,
    ) -> None:
        self.max_concurrency = max_concurrency or _positive_int(
            "CHATGPT2API_REALTIME_SIGNALING_CONCURRENCY", 8
        )
        self.rate_per_minute = rate_per_minute or _positive_int(
            "CHATGPT2API_REALTIME_SIGNALING_RATE_PER_MINUTE", 20
        )
        self.attempt_ttl_seconds = attempt_ttl_seconds or _positive_int(
            "CHATGPT2API_REALTIME_ATTEMPT_TTL_SECONDS", 300
        )
        self.quota_cooldown_seconds = _positive_int(
            "CHATGPT2API_REALTIME_QUOTA_COOLDOWN_SECONDS", 3600
        )
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._lock = threading.Lock()
        self._rate_events: dict[str, deque[float]] = {}
        self._attempts: dict[str, _AttemptChain] = {}
        self._quota_cooldowns: dict[str, float] = {}

    def check_rate_limit(self, identity_key: str, now: float | None = None) -> int:
        current = time.monotonic() if now is None else now
        cutoff = current - 60.0
        with self._lock:
            stale_identities = [
                key for key, queued in self._rate_events.items()
                if not queued or queued[-1] <= cutoff
            ]
            for key in stale_identities:
                self._rate_events.pop(key, None)
            events = self._rate_events.setdefault(identity_key, deque())
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.rate_per_minute:
                return max(1, int(60.0 - (current - events[0])))
            events.append(current)
        return 0

    def open_attempt(self, identity_key: str, attempt_id: str | None) -> tuple[str, set[str]]:
        now = time.monotonic()
        with self._lock:
            expired = [key for key, value in self._attempts.items() if value.expires_at <= now]
            for key in expired:
                self._attempts.pop(key, None)
            expired_cooldowns = [token for token, expires_at in self._quota_cooldowns.items() if expires_at <= now]
            for token in expired_cooldowns:
                self._quota_cooldowns.pop(token, None)

            chain = self._attempts.get(attempt_id or "")
            if chain is None or chain.identity_key != identity_key:
                attempt_id = uuid.uuid4().hex
                chain = _AttemptChain(
                    identity_key=identity_key,
                    expires_at=now + self.attempt_ttl_seconds,
                )
                self._attempts[attempt_id] = chain
            else:
                chain.expires_at = now + self.attempt_ttl_seconds
            return attempt_id, set(chain.excluded_tokens) | set(self._quota_cooldowns)

    def record_account(self, attempt_id: str, access_token: str) -> None:
        with self._lock:
            chain = self._attempts.get(attempt_id)
            if chain is not None:
                chain.excluded_tokens.add(access_token)
                chain.last_token = access_token

    def mark_quota_exhausted(self, identity_key: str, attempt_id: str) -> bool:
        now = time.monotonic()
        with self._lock:
            chain = self._attempts.get(attempt_id)
            if (
                chain is None
                or chain.identity_key != identity_key
                or chain.expires_at <= now
                or not chain.last_token
            ):
                return False
            self._quota_cooldowns[chain.last_token] = now + self.quota_cooldown_seconds
            return True

    @asynccontextmanager
    async def signaling_slot(self):
        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=0.25)
        except asyncio.TimeoutError as exc:
            raise SignalingBusyError("realtime signaling is busy") from exc
        try:
            yield
        finally:
            self._semaphore.release()


realtime_signaling_guard = RealtimeSignalingGuard()
