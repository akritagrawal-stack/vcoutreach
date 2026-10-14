"""SPEC section 22.1 to 22.4: backoff ceiling, response cap, loopback proxy bypass.

Loopback servers are the suite's LocalGateway, which binds 127.0.0.1 only.
"""
from __future__ import annotations

import socket

import pytest

from vcoutreach.providers import PublikProvider
from vcoutreach.providers.http import UrllibTransport, send_with_retries
from vcoutreach.types import ProviderResponseError

URL = "https://gateway.test/api/v1/chat/completions"
HEADERS = {"Authorization": "Bearer abc", "Content-Type": "application/json"}
BODY = b'{"hello": "world"}'
RESPONSE_CAP = 8_000_000


# ----- 22.1 the exponential delay doubles to 16 seconds and stops there (before the jitter) ---------------------------------------------

def eight_failures_then_ok(kit, failing):
    return kit.fake_transport(*[failing() for _ in range(8)], kit.response(200, body=b"ok"))


@pytest.mark.parametrize("kind", ["status-503", "status-429", "connection-failure"])
def test_eight_retries_wait_one_two_four_eight_then_sixteen_four_times(kit, kind):
    failing = {
        "status-503": lambda: kit.response(503, body=b"busy"),
        "status-429": lambda: kit.response(429, body=b"slow down"),
        "connection-failure": lambda: kit.failure(),
    }[kind]
    sleeper = kit.sleeper()
    resp = send_with_retries(eight_failures_then_ok(kit, failing), "POST", URL, HEADERS, BODY,
                             timeout=5.0, max_retries=8, sleep=sleeper, rng=kit.rng(0.0))
    assert resp.status == 200
    assert sleeper.calls == [pytest.approx(v) for v in (1, 2, 4, 8, 16, 16, 16, 16)]


def test_the_ceiling_is_applied_before_the_jitter(kit):
    sleeper = kit.sleeper()
    send_with_retries(eight_failures_then_ok(kit, lambda: kit.response(503, body=b"busy")), "POST", URL, HEADERS, BODY,
                      timeout=5.0, max_retries=8, sleep=sleeper, rng=kit.rng(1.0))
    assert sleeper.calls == [pytest.approx(v * 1.1) for v in (1, 2, 4, 8, 16, 16, 16, 16)]


@pytest.mark.parametrize("value,expected", [("1", 1.0), ("7", 7.0), ("30", 30.0), ("31", 30.0), ("100", 30.0), ("99999", 30.0)])
def test_a_numeric_retry_after_is_used_and_capped_at_thirty_seconds(kit, value, expected):
    ft = kit.fake_transport(kit.response(429, headers={"Retry-After": value}, body=b"{}"), kit.response(200, body=b"ok"))
    sleeper = kit.sleeper()
    send_with_retries(ft, "POST", URL, HEADERS, BODY, timeout=5.0, max_retries=2, sleep=sleeper, rng=kit.rng(0.0))
    assert sleeper.calls == [pytest.approx(expected)]


# ----- 22.2 at most 8,000,000 bytes of a response are read -------------------------------------------------------------------------------

@pytest.mark.local_http
def test_a_response_larger_than_the_cap_is_truncated_at_exactly_the_cap(gateway):
    gateway.reply(200, body=b"x" * (RESPONSE_CAP + 1_000_000))
    resp = UrllibTransport().request("GET", gateway.origin + "/big", {}, None, 30.0)
    assert resp.status == 200
    assert len(resp.body) == RESPONSE_CAP


@pytest.mark.local_http
def test_a_response_just_over_the_cap_loses_only_the_excess(gateway):
    gateway.reply(200, body=b"y" * (RESPONSE_CAP + 1))
    assert len(UrllibTransport().request("GET", gateway.origin + "/big", {}, None, 30.0).body) == RESPONSE_CAP


@pytest.mark.local_http
def test_a_response_exactly_at_the_cap_is_read_in_full(gateway):
    gateway.reply(200, body=b"z" * RESPONSE_CAP)
    assert len(UrllibTransport().request("GET", gateway.origin + "/big", {}, None, 30.0).body) == RESPONSE_CAP


@pytest.mark.local_http
def test_a_provider_reports_a_parse_error_when_the_answer_was_cut(gateway, kit):
    padding = "a" * (RESPONSE_CAP + 500_000)
    gateway.reply(200, body='{"choices":[{"message":{"role":"assistant","content":"' + padding + '"}}]}')
    settings = kit.publik_settings(PUBLIK_API_BASE_URL=gateway.base_url, PUBLIK_MAX_RETRIES="0", PUBLIK_TIMEOUT_SECONDS="30")
    with pytest.raises(ProviderResponseError):
        PublikProvider(settings).personalize(kit.request())


# ----- 22.4 loopback hosts are contacted directly, whatever the proxy environment says --------------------------------------------------

def dead_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def set_proxy(monkeypatch, url):
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, url)
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)  # only the transport's own rule may save the request


@pytest.mark.local_http
def test_a_loopback_server_is_reached_directly_even_when_the_proxy_points_at_a_dead_port(gateway, monkeypatch):
    set_proxy(monkeypatch, f"http://127.0.0.1:{dead_port()}")
    gateway.reply(200, body=b"direct")
    resp = UrllibTransport().request("GET", gateway.origin + "/ping", {}, None, 5.0)
    assert resp.status == 200 and resp.body == b"direct"


@pytest.mark.local_http
def test_a_loopback_request_never_touches_a_working_proxy(gateway, monkeypatch):
    proxy = type(gateway)()
    try:
        proxy.reply(200, body=b"from the proxy")
        set_proxy(monkeypatch, proxy.origin)
        gateway.reply(200, body=b"direct")
        resp = UrllibTransport().request("POST", gateway.base_url + "/chat/completions", HEADERS, BODY, 5.0)
        seen_by_proxy = list(proxy.requests)
    finally:
        proxy.stop()
    assert resp.body == b"direct"
    assert seen_by_proxy == [] and len(gateway.requests) == 1


@pytest.mark.local_http
def test_other_hosts_still_go_through_the_proxy(gateway, monkeypatch, network_guard):
    proxy = type(gateway)()
    try:
        proxy.reply(200, body=b"from the proxy")
        set_proxy(monkeypatch, proxy.origin)
        resp = UrllibTransport().request("GET", "http://upstream.test/thing", {}, None, 5.0)
        seen_by_proxy = list(proxy.requests)
    finally:
        proxy.stop()
    assert resp.status == 200 and resp.body == b"from the proxy"
    assert len(seen_by_proxy) == 1 and "upstream.test" in seen_by_proxy[0].path + str(seen_by_proxy[0].headers)
    assert network_guard.attempts == [], "the real host must never be looked up when a proxy is configured"
