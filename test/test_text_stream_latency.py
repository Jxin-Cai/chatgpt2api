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


def test_follower_receives_each_chunk_before_owner_finishes():
    store = cache()
    source = Mock(return_value=iter([{"text": "first"}, {"text": "second"}]))
    owner = store.get_or_compute_stream("key", source)
    follower = store.get_or_compute_stream("key", source)
    assert next(owner) == {"text": "first"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(next, follower)
        try:
            assert pending.result(timeout=1) == {"text": "first"}
            assert next(owner) == {"text": "second"}
            assert next(follower) == {"text": "second"}
            assert list(owner) == []
            assert list(follower) == []
            source.assert_called_once()
        finally:
            owner.close()  # also release the old implementation's blocked waiter
            try:
                pending.result(timeout=1)
            except BaseException:
                pass
            follower.close()


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
def test_partial_stream_error_is_propagated_and_never_cached(cancel):
    store = cache()
    def broken():
        yield {"text": "partial"}
        raise RuntimeError("upstream failed")
    owner = store.get_or_compute_stream("key", broken)
    assert next(owner) == {"text": "partial"}
    follower = store.get_or_compute_stream("key", broken)
    assert next(follower) == {"text": "partial"}
    if cancel:
        owner.close()
    else:
        with pytest.raises(RuntimeError, match="upstream failed"):
            next(owner)
    with pytest.raises(RuntimeError, match="interrupted" if cancel else "upstream failed"):
        next(follower)
    assert not store._entries
    assert not store._inflight


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
