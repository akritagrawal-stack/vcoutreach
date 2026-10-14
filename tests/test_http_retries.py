"""providers.http: HttpResponse, send_with_retries and the real UrllibTransport (SPEC section 10)."""
from __future__ import annotations

import dataclasses
import inspect
import random
import time

import pytest

from vcoutreach.providers.http import (
    HttpResponse,
    TransportFailure,
    UrllibTransport,
    send_with_retries,
)

URL = "https://gateway.test/api/v1/chat/completions"
HEADERS = {"Authorization": "Bearer abc", "Content-Type": "application/json"}
BODY = b'{"hello": "world"}'


def go(kit, ft, *, max_retries=3, rng=None, sleeper=None, timeout=12.5, method="POST", body=BODY):
    sleeper = sleeper if sleeper is not None else kit.sleeper()
    result = send_with_retries(
        ft, method, URL, HEADERS, body,
        timeout=timeout, max_retries=max_retries,
        sleep=sleeper, rng=rng if rng is not None else kit.rng(0.0),
    )
    return result, sleeper


def status_only(kit, status, **headers):
    return kit.response(status, headers=headers, body=b"{}")


# ----- the response type ------------------------------------------------------------------------------------

def test_http_response_has_status_headers_body_and_is_frozen():
    assert [f.name for f in dataclasses.fields(HttpResponse)] == ["status", "headers", "body"]
    r = HttpResponse(status=200, headers={"x": "y"}, body=b"ok")
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.status = 500  # type: ignore[misc]


def test_sleep_and_rng_default_to_the_real_functions():
    params = inspect.signature(send_with_retries).parameters
    assert params["sleep"].default is time.sleep
    assert params["rng"].default is random.random
    assert params["timeout"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["max_retries"].kind is inspect.Parameter.KEYWORD_ONLY


# ----- no retry cases --------------------------------------------------------------------------------------------

def test_success_returns_immediately_without_sleeping(kit):
    ft = kit.fake_transport(kit.response(200, body=b"fine"))
    result, sleeper = go(kit, ft)
    assert result.status == 200 and result.body == b"fine"
    assert len(ft.requests) == 1 and sleeper.calls == []


@pytest.mark.parametrize("status", [200, 201, 400, 401, 402, 403, 404, 418, 422, 501])
def test_statuses_outside_the_retry_set_are_returned_after_one_attempt(kit, status):
    ft = kit.fake_transport(status_only(kit, status))
    result, sleeper = go(kit, ft)
    assert result.status == status
    assert len(ft.requests) == 1
    assert sleeper.calls == []


# ----- retry statuses --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_each_retry_status_is_retried_and_a_later_success_is_returned(kit, status):
    ft = kit.fake_transport(status_only(kit, status), kit.response(200, body=b"ok"))
    result, sleeper = go(kit, ft)
    assert result.status == 200 and result.body == b"ok"
    assert len(ft.requests) == 2 and len(sleeper.calls) == 1


def test_429_then_200_waits_one_second_plus_jitter(kit):
    ft = kit.fake_transport(status_only(kit, 429), kit.response(200))
    result, sleeper = go(kit, ft, rng=kit.rng(0.5))
    assert result.status == 200
    assert sleeper.calls == [pytest.approx(1.05)]


def test_503_503_200_backs_off_one_then_two_seconds(kit):
    ft = kit.fake_transport(status_only(kit, 503), status_only(kit, 503), kit.response(200))
    result, sleeper = go(kit, ft, rng=kit.rng(0.0))
    assert result.status == 200
    assert len(ft.requests) == 3
    assert sleeper.calls == [pytest.approx(1.0), pytest.approx(2.0)]


def test_backoff_sequence_is_1_2_4_times_one_plus_a_tenth_of_rng(kit):
    ft = kit.fake_transport(*[status_only(kit, 503) for _ in range(4)])
    result, sleeper = go(kit, ft, max_retries=3, rng=kit.rng(0.0, 0.5, 1.0))
    assert result.status == 503
    assert sleeper.calls == [pytest.approx(1.0), pytest.approx(2.1), pytest.approx(4.4)]


def test_exhausted_retries_return_the_last_response_not_an_exception(kit):
    ft = kit.fake_transport(*[status_only(kit, 429) for _ in range(4)])
    result, sleeper = go(kit, ft, max_retries=3)
    assert result.status == 429
    assert len(ft.requests) == 4, "max_retries counts EXTRA attempts after the first"
    assert len(sleeper.calls) == 3, "no sleep after the final attempt"


def test_max_retries_zero_means_a_single_attempt(kit):
    ft = kit.fake_transport(status_only(kit, 503))
    result, sleeper = go(kit, ft, max_retries=0)
    assert result.status == 503 and len(ft.requests) == 1 and sleeper.calls == []


def test_the_last_response_wins_when_statuses_change_between_attempts(kit):
    ft = kit.fake_transport(status_only(kit, 503), status_only(kit, 429), kit.response(500, body=b"last"))
    result, _ = go(kit, ft, max_retries=2)
    assert result.status == 500 and result.body == b"last"


def test_a_non_retry_status_in_the_middle_stops_the_loop(kit):
    ft = kit.fake_transport(status_only(kit, 503), status_only(kit, 402), kit.response(200))
    result, sleeper = go(kit, ft)
    assert result.status == 402
    assert len(ft.requests) == 2 and len(sleeper.calls) == 1


# ----- Retry-After -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("header,expected", [("7", 7.0), ("1", 1.0), ("29", 29.0), ("30", 30.0)])
def test_numeric_retry_after_is_honoured_exactly_without_jitter(kit, header, expected):
    ft = kit.fake_transport(status_only(kit, 429, **{"retry-after": header}), kit.response(200))
    _, sleeper = go(kit, ft, rng=kit.rng(1.0))
    assert sleeper.calls == [pytest.approx(expected)]


@pytest.mark.parametrize("header", ["31", "120", "99999"])
def test_retry_after_is_capped_at_thirty_seconds(kit, header):
    ft = kit.fake_transport(status_only(kit, 429, **{"retry-after": header}), kit.response(200))
    _, sleeper = go(kit, ft)
    assert sleeper.calls == [pytest.approx(30.0)]


def test_retry_after_zero_means_no_waiting(kit):
    ft = kit.fake_transport(status_only(kit, 429, **{"retry-after": "0"}), kit.response(200))
    result, sleeper = go(kit, ft)
    assert result.status == 200 and sleeper.total == 0


def test_retry_after_also_applies_to_503(kit):
    ft = kit.fake_transport(status_only(kit, 503, **{"retry-after": "3"}), kit.response(200))
    _, sleeper = go(kit, ft)
    assert sleeper.calls == [pytest.approx(3.0)]


@pytest.mark.parametrize("header", ["Wed, 21 Oct 2026 07:28:00 GMT", "soon", "", "ten"])
def test_non_numeric_retry_after_falls_back_to_the_backoff_schedule(kit, header):
    ft = kit.fake_transport(status_only(kit, 429, **{"retry-after": header}), kit.response(200))
    _, sleeper = go(kit, ft, rng=kit.rng(0.0))
    assert sleeper.calls == [pytest.approx(1.0)]


# ----- transport failures ----------------------------------------------------------------------------------------------

def test_transport_failure_is_retried_and_a_later_response_is_returned(kit):
    ft = kit.fake_transport(kit.failure("connection reset"), kit.response(200, body=b"back"))
    result, sleeper = go(kit, ft)
    assert result.body == b"back"
    assert len(ft.requests) == 2 and len(sleeper.calls) == 1


def test_transport_failure_after_every_attempt_is_raised(kit):
    ft = kit.fake_transport(*[kit.failure(f"down {i}") for i in range(4)])
    sleeper = kit.sleeper()
    with pytest.raises(TransportFailure):
        go(kit, ft, max_retries=3, sleeper=sleeper)
    assert len(ft.requests) == 4
    assert len(sleeper.calls) == 3


def test_failures_and_bad_statuses_share_one_retry_budget(kit):
    ft = kit.fake_transport(kit.failure("x"), status_only(kit, 503), kit.response(200))
    result, sleeper = go(kit, ft, rng=kit.rng(0.0))
    assert result.status == 200
    assert sleeper.calls == [pytest.approx(1.0), pytest.approx(2.0)]


def test_unexpected_exceptions_are_not_swallowed_or_retried(kit):
    ft = kit.fake_transport(ValueError("programming error"), kit.response(200))
    with pytest.raises(ValueError):
        go(kit, ft)
    assert len(ft.requests) == 1


# ----- what is sent ------------------------------------------------------------------------------------------------------

def test_every_attempt_sends_the_same_request(kit):
    ft = kit.fake_transport(status_only(kit, 503), kit.failure("x"), kit.response(200))
    go(kit, ft, timeout=7.5)
    assert len(ft.requests) == 3
    for req in ft.requests:
        assert (req.method, req.url, req.body, req.timeout) == ("POST", URL, BODY, 7.5)
        assert req.header("authorization") == "Bearer abc"
        assert req.header("content-type") == "application/json"


def test_get_requests_without_a_body_are_supported(kit):
    ft = kit.fake_transport(kit.response(200))
    result, _ = go(kit, ft, method="GET", body=None)
    assert result.status == 200
    assert ft.requests[0].method == "GET" and ft.requests[0].body is None


# ----- the real transport against a local server -----------------------------------------------------------------------------

@pytest.mark.local_http
@pytest.mark.parametrize("status", [200, 400, 401, 402, 403, 404, 429, 500, 503])
def test_urllib_transport_returns_every_http_status_without_raising(gateway, status):
    gateway.reply(status, body=b'{"error":{"message":"scripted"}}', headers={"X-Custom-Thing": "abc"})
    resp = UrllibTransport().request("POST", gateway.base_url + "/chat/completions", HEADERS, BODY, 5.0)
    assert resp.status == status
    assert resp.body == b'{"error":{"message":"scripted"}}'
    assert resp.headers["x-custom-thing"] == "abc", "response header names must be lower-cased"
    assert all(k == k.lower() for k in resp.headers)


@pytest.mark.local_http
def test_urllib_transport_sends_method_path_headers_and_body(gateway):
    gateway.reply(200, body=b"ok")
    UrllibTransport().request("POST", gateway.base_url + "/chat/completions", HEADERS, BODY, 5.0)
    (req,) = gateway.requests
    assert req.method == "POST" and req.path == "/api/v1/chat/completions"
    assert req.header("authorization") == "Bearer abc"
    assert req.header("content-type") == "application/json"
    assert req.body == BODY


@pytest.mark.local_http
def test_urllib_transport_get_without_a_body(gateway):
    gateway.reply(200, body=b"hello")
    resp = UrllibTransport().request("GET", gateway.origin + "/ping", {}, None, 5.0)
    assert resp.status == 200 and resp.body == b"hello"
    assert gateway.requests[0].method == "GET" and gateway.requests[0].body == b""


@pytest.mark.local_http
def test_urllib_transport_preserves_binary_bodies(gateway):
    payload = bytes(range(256))
    gateway.reply(200, body=payload)
    assert UrllibTransport().request("GET", gateway.origin + "/bin", {}, None, 5.0).body == payload


@pytest.mark.local_http
def test_urllib_transport_connection_refused_raises_transport_failure(gateway):
    url = gateway.origin + "/nothing"
    gateway.stop()
    with pytest.raises(TransportFailure):
        UrllibTransport().request("GET", url, {}, None, 2.0)


@pytest.mark.local_http
def test_urllib_transport_slow_server_raises_transport_failure_at_the_timeout(gateway):
    gateway.reply(200, body=b"too late", delay=3.0)
    started = time.monotonic()
    with pytest.raises(TransportFailure):
        UrllibTransport().request("GET", gateway.origin + "/slow", {}, None, 0.3)
    assert time.monotonic() - started < 2.5, "the timeout was not applied to the read"


@pytest.mark.local_http
def test_send_with_retries_over_the_real_transport_recovers_from_503_then_429(gateway, kit):
    gateway.reply(503, body=b"busy")
    gateway.reply(429, body=b"slow down", headers={"Retry-After": "2"})
    gateway.reply(200, body=b"finally")
    sleeper = kit.sleeper()
    resp = send_with_retries(
        UrllibTransport(), "POST", gateway.base_url + "/chat/completions", HEADERS, BODY,
        timeout=5.0, max_retries=3, sleep=sleeper, rng=kit.rng(0.0),
    )
    assert resp.status == 200 and resp.body == b"finally"
    assert len(gateway.requests) == 3
    assert sleeper.calls == [pytest.approx(1.0), pytest.approx(2.0)]
