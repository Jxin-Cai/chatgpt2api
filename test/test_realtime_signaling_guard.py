from services.realtime.signaling import RealtimeSignalingGuard


def test_signaling_guard_applies_identity_rate_limit():
    guard = RealtimeSignalingGuard(max_concurrency=1, rate_per_minute=2, attempt_ttl_seconds=30)

    assert guard.check_rate_limit("user", now=100.0) == 0
    assert guard.check_rate_limit("user", now=101.0) == 0
    assert guard.check_rate_limit("user", now=102.0) > 0
    assert guard.check_rate_limit("other-user", now=102.0) == 0


def test_attempt_chain_is_bound_to_identity_and_tracks_exclusions():
    guard = RealtimeSignalingGuard(max_concurrency=1, rate_per_minute=10, attempt_ttl_seconds=30)
    attempt_id, excluded = guard.open_attempt("user-a", None)
    assert excluded == set()

    guard.record_account(attempt_id, "account-token")
    same_attempt, excluded = guard.open_attempt("user-a", attempt_id)
    other_attempt, other_excluded = guard.open_attempt("user-b", attempt_id)

    assert same_attempt == attempt_id
    assert excluded == {"account-token"}
    assert other_attempt != attempt_id
    assert other_excluded == set()


def test_quota_report_cools_account_for_new_attempts():
    guard = RealtimeSignalingGuard(max_concurrency=1, rate_per_minute=10, attempt_ttl_seconds=30)
    attempt_id, _ = guard.open_attempt("user", None)
    guard.record_account(attempt_id, "exhausted-account")

    assert guard.mark_quota_exhausted("other-user", attempt_id) is None
    assert guard.mark_quota_exhausted("user", attempt_id) == "exhausted-account"

    _, excluded = guard.open_attempt("new-user", None)
    assert "exhausted-account" in excluded
