import asyncio
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from services.log_service import LoggedCall
from services.protocol.chat_completion_cache import ChatCompletionCache
from services.protocol import openai_v1_chat_complete as chat


def cache():
    instance = ChatCompletionCache()
    instance._settings = lambda: dict(enabled=True, stream_cache=True, ttl_seconds=60,
                                     max_entries=10, dedupe_inflight=True)
    return instance


def test_slow_consumer_does_not_hold_other_streams_in_worker_pool():
    store = cache()
    source = Mock(side_effect=lambda: iter([{"text": "first"}, {"text": "second"}]))
    slow = store.get_or_compute_stream("key", source)
    assert next(slow) == {"text": "first"}
    fast = store.get_or_compute_stream("key", source)
    # The previous shared stream blocked here until slow requested its next
    # chunk. Enough such waiters starved that owner of a worker permanently.
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(list, fast)
        try:
            assert pending.result(timeout=1) == [{"text": "first"}, {"text": "second"}]
            assert source.call_count == 2
        finally:
            slow.close()
            pending.result(timeout=1)


def test_cached_stream_can_resume_and_finish_on_different_worker_threads():
    store = cache()
    expected = [{"text": "a"}, {"text": "b"}]
    assert list(store.get_or_compute_stream("key", lambda: iter(expected))) == expected
    cached = store.get_or_compute_stream("key", lambda: pytest.fail("cache miss"))
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(next, cached).result(timeout=1) == expected[0]
        assert list(cached) == expected[1:]
    assert not store._inflight, "cache hits must not create orphaned producers"


def test_subscriber_can_suspend_without_blocking_other_cache_keys():
    store = cache()
    owner = store.get_or_compute_stream("key", lambda: iter([{"text": "a"}]))
    next(owner)
    follower = store.get_or_compute_stream("key", lambda: pytest.fail("duplicate producer"))
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(store.get_or_compute_response, "other", lambda: {"ok": True}).result(timeout=1) == {"ok": True}
        assert list(owner) == []
        assert next(follower) == {"text": "a"}
    finally:
        owner.close()
        follower.close()


@pytest.mark.parametrize("cancel", [False, True])
def test_partial_stream_error_is_never_cached_and_retry_is_independent(cancel):
    store = cache()
    def broken():
        yield {"text": "partial"}
        raise RuntimeError("upstream failed")
    owner = store.get_or_compute_stream("key", broken)
    assert next(owner) == {"text": "partial"}
    if cancel:
        owner.close()
    else:
        with pytest.raises(RuntimeError, match="upstream failed"):
            next(owner)
    assert not store._entries
    assert not store._inflight
    assert list(store.get_or_compute_stream("key", lambda: iter([{"text": "retry"}]))) == [{"text": "retry"}]


def test_sse_headers_and_role_arrive_before_upstream_work(monkeypatch):
    upstream = Mock(return_value=iter([("content", "answer")]))
    monkeypatch.setattr(chat, "stream_text_parts", upstream)
    async def run():
        call = LoggedCall({}, "/v1/chat/completions", "gpt-6.1-sol", "test")
        call.log = Mock()
        result = await call.run(lambda: chat.stream_text_chat_completion(object(), [], "gpt-6.1-sol"))
        upstream.assert_not_called()
        assert result.headers["x-accel-buffering"] == "no"
        assert result.headers["cache-control"] == "no-cache, no-transform"
        assert "stream-open" in await anext(result.body_iterator)
        assert '"role": "assistant"' in await anext(result.body_iterator)
        upstream.assert_not_called()
        assert '"answer"' in await anext(result.body_iterator)
        assert "[DONE]" in "".join([chunk async for chunk in result.body_iterator])
    asyncio.run(run())
