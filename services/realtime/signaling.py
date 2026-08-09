from __future__ import annotations

import asyncio
import os
import secrets
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
    def __init__(self, status_code: int, detail: str = "", retry_after_seconds: int | None = None):
        super().__init__(f"upstream realtime signaling failed with HTTP {status_code}")
        self.status_code = status_code
        self.detail = detail
        self.retry_after_seconds = retry_after_seconds

    @property
    def is_quota_limited(self) -> bool:
        if self.status_code == 429:
            return True
        detail = self.detail.lower()
        return self.status_code == 403 and any(
            marker in detail
            for marker in (
                "daily limit",
                "rate limit",
                "quota",
                "usage limit",
                "cap_reached",
                "limit reached",
            )
        )


@dataclass
class _AttemptChain:
    identity_key: str
    expires_at: float
    excluded_tokens: set[str] = field(default_factory=set)
    last_token: str | None = None


@dataclass
class _PinnedSession:
    """Opaque browser session binding used for short reconnects.

    The upstream access token deliberately never leaves this process.  The
    client only receives ``session_id`` and can use it while the binding is
    alive and belongs to the same authenticated identity.
    """

    identity_key: str
    access_token: str
    expires_at: float
    conversation_id: str = ""


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
        session_ttl_seconds: int | None = None,
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
        # A pinned session is intentionally much longer-lived than an account
        # rotation attempt: text priming followed by a WebRTC reconnect can
        # span several minutes, but it must not become a permanent token
        # cache.  Two hours matches the browser session's expected lifetime.
        self.session_ttl_seconds = session_ttl_seconds or _positive_int(
            "CHATGPT2API_REALTIME_SESSION_TTL_SECONDS", 2 * 60 * 60
        )
        self.quota_cooldown_seconds = _positive_int(
            "CHATGPT2API_REALTIME_QUOTA_COOLDOWN_SECONDS", 86400
        )
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._lock = threading.Lock()
        self._rate_events: dict[str, deque[float]] = {}
        self._attempts: dict[str, _AttemptChain] = {}
        self._pinned_sessions: dict[str, _PinnedSession] = {}
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
            expired_sessions = [
                key for key, value in self._pinned_sessions.items()
                if value.expires_at <= now
            ]
            for key in expired_sessions:
                self._pinned_sessions.pop(key, None)
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

    def mark_quota_exhausted(
        self,
        identity_key: str,
        attempt_id: str,
        cooldown_seconds: int | None = None,
    ) -> str | None:
        now = time.monotonic()
        with self._lock:
            chain = self._attempts.get(attempt_id)
            if (
                chain is None
                or chain.identity_key != identity_key
                or chain.expires_at <= now
                or not chain.last_token
            ):
                return None
            self._quota_cooldowns[chain.last_token] = now + (
                cooldown_seconds or self.quota_cooldown_seconds
            )
            return chain.last_token

    def get_attempt_token(self, identity_key: str, attempt_id: str) -> str | None:
        """Return the access_token associated with an active attempt, or None."""
        now = time.monotonic()
        with self._lock:
            chain = self._attempts.get(attempt_id)
            if (
                chain is None
                or chain.identity_key != identity_key
                or chain.expires_at <= now
                or not chain.last_token
            ):
                return None
            return chain.last_token

    def pin_session(
        self,
        identity_key: str,
        access_token: str,
        conversation_id: str = "",
        session_id: str | None = None,
    ) -> str:
        """Create or refresh an opaque identity-bound token binding.

        ``session_id`` is only accepted when it already belongs to the same
        identity and token.  This prevents a caller from replacing another
        user's binding while still allowing an in-place refresh on resume.
        """
        if not access_token:
            raise ValueError("access_token is required to pin a realtime session")
        now = time.monotonic()
        with self._lock:
            if session_id:
                current = self._pinned_sessions.get(session_id)
                if (
                    current is None
                    or current.identity_key != identity_key
                    or current.access_token != access_token
                    or current.expires_at <= now
                ):
                    session_id = None
            opaque_id = session_id or secrets.token_urlsafe(32)
            self._pinned_sessions[opaque_id] = _PinnedSession(
                identity_key=identity_key,
                access_token=access_token,
                expires_at=now + self.session_ttl_seconds,
                conversation_id=str(conversation_id or ""),
            )
            return opaque_id

    def get_pinned_session(
        self,
        identity_key: str,
        session_id: str,
        *,
        conversation_id: str = "",
        refresh: bool = False,
    ) -> _PinnedSession | None:
        """Return an identity-bound pinned session without exposing its token.

        ``refresh=True`` is used by SDP resume requests and extends the
        binding for another ``session_ttl_seconds``.  A supplied conversation
        id is remembered for diagnostics/continuity, but is not used as a
        credential and therefore cannot grant access on its own.
        """
        if not session_id:
            return None
        now = time.monotonic()
        with self._lock:
            pinned = self._pinned_sessions.get(session_id)
            if pinned is None or pinned.identity_key != identity_key or pinned.expires_at <= now:
                if pinned is not None and pinned.expires_at <= now:
                    self._pinned_sessions.pop(session_id, None)
                return None
            if refresh:
                pinned.expires_at = now + self.session_ttl_seconds
            if conversation_id:
                pinned.conversation_id = str(conversation_id)
            # Return a copy so callers cannot mutate protected state (and so
            # tests/loggers never need to print the token accidentally).
            return _PinnedSession(
                identity_key=pinned.identity_key,
                access_token=pinned.access_token,
                expires_at=pinned.expires_at,
                conversation_id=pinned.conversation_id,
            )

    def get_pinned_token(
        self,
        identity_key: str,
        session_id: str,
        *,
        conversation_id: str = "",
        refresh: bool = False,
    ) -> str | None:
        """Return the upstream token for an active pinned session internally."""
        pinned = self.get_pinned_session(
            identity_key,
            session_id,
            conversation_id=conversation_id,
            refresh=refresh,
        )
        return pinned.access_token if pinned else None

    def resume_session(
        self,
        identity_key: str,
        session_id: str,
        conversation_id: str = "",
    ) -> str | None:
        """Resume a pinned session and extend its two-hour lease."""
        return self.get_pinned_token(
            identity_key,
            session_id,
            conversation_id=conversation_id,
            refresh=True,
        )

    def cool_account(self, access_token: str, cooldown_seconds: int) -> None:
        if not access_token:
            return
        with self._lock:
            self._quota_cooldowns[access_token] = time.monotonic() + max(1, cooldown_seconds)

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
