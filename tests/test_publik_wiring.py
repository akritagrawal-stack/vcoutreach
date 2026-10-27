"""publik API wiring: key resolution, provisioning, self-test, 402 top-up, web routes. No network."""
from __future__ import annotations

import json
import os
import stat

import pytest

from web_helpers import *  # noqa: F401,F403

from vcoutreach import publik_api
from vcoutreach.providers.publik import PublikProvider
from vcoutreach.types import ProviderConfigError, ProviderCreditError

SECRET = "pk_wiretest_SECRETVALUE0123456789"
OWN = "fakekey_OWNKEY0123456789"
ENVKEY = "fakekey_ENVKEY0123456789"
TOKEN = "pat_faketokenfortests"


def _install_response(**over):
    body = {
        "key": SECRET, "base_url": "https://publikhq.com/api/v1", "install_id": "abcdef12-0000-0000-0000-000000000000",
        "claim_url": "https://publikhq.com/claim/xyz", "claim_state": "anonymous",
        "wallet": {"add_credit_url": "https://publikhq.com/add-credit"},
        "disclosure": {"cost": "Costs are priced per use."}, "balance_micros": 0,
    }
    body.update(over)
    return 201, body


@pytest.fixture
def pk_home(tmp_path, monkeypatch):
    home = tmp_path / "pkhome"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData"))
    monkeypatch.delenv("PUBLIKREACH_NO_CREDENTIAL_FILE", raising=False)
    monkeypatch.setattr(publik_api.Path, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture
def fake_post(monkeypatch):
    calls = []

    def install(response=None):
        response = response or _install_response()

        def _post(url, body):
            calls.append((url, body))
            return response

        monkeypatch.setattr(publik_api, "_post", _post)
        return calls

    return install


@pytest.fixture
def with_token(monkeypatch):
    monkeypatch.setenv("PUBLIK_APP_TOKEN", TOKEN)


def _save_cred(**over):
    c = {"key": SECRET, "base_url": "https://credfile.example.test/api/v1", "claim_state": "anonymous"}
    c.update(over)
    publik_api._write_credential(c)


def _provider_key_and_url(settings):
    p = PublikProvider(settings)
    return p._api_key, p._url


def test_resolution_order_settings_then_env_then_credential_file(kit, pk_home, monkeypatch):
    _save_cred()
    # 1. settings key (user's own) wins over env and the file
    monkeypatch.setenv("PUBLIK_API_KEY", ENVKEY)
    s = kit.settings(PUBLIK_API_KEY=OWN)
    assert _provider_key_and_url(s)[0] == OWN
    # 2. env key wins over the file
    bare = kit.settings()
    assert _provider_key_and_url(bare)[0] == ENVKEY
    # 3. file is used with its own base_url
    monkeypatch.delenv("PUBLIK_API_KEY")
    key, url = _provider_key_and_url(kit.settings())
    assert key == SECRET and url == "https://credfile.example.test/api/v1/chat/completions"


def test_nothing_resolves_keeps_error_and_mentions_settings(kit, pk_home):
    with pytest.raises(ProviderConfigError) as err:
        PublikProvider(kit.settings())
    text = str(err.value)
    assert "PUBLIK_API_KEY is not set" in text and "Settings page" in text and "no key" in text


def test_credential_file_lookup_is_skippable(kit, pk_home, monkeypatch):
    _save_cred()
    monkeypatch.setenv("PUBLIKREACH_NO_CREDENTIAL_FILE", "1")
    assert publik_api.read_credential() is None
    with pytest.raises(ProviderConfigError):
        PublikProvider(kit.settings())


def test_provision_writes_0600_credential(pk_home, fake_post, with_token):
    calls = fake_post()
    c = publik_api.provision()
    assert c["key"] == SECRET
    path = publik_api.credential_path()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(path.read_text())["key"] == SECRET
    url, body = calls[0]
    assert url.endswith("/installs") and body["app_slug"] == "publikreach" and body["app_token"] == TOKEN
    assert body["app_version"] == publik_api.APP_VERSION


def test_missing_token_is_a_readable_error(pk_home, monkeypatch, fake_post):
    monkeypatch.delenv("PUBLIK_APP_TOKEN", raising=False)
    monkeypatch.setattr(publik_api, "_token_dirs", lambda: [])
    calls = fake_post()
    with pytest.raises(RuntimeError, match="no publik API app token"):
        publik_api.provision()
    assert calls == []


def test_token_file_found_in_meipass(tmp_path, monkeypatch):
    monkeypatch.delenv("PUBLIK_APP_TOKEN", raising=False)
    (tmp_path / "publik-app-token.txt").write_text(TOKEN + "\n")
    monkeypatch.setattr("sys._MEIPASS", str(tmp_path), raising=False)
    assert publik_api._app_token() == TOKEN


def test_self_test_line_formats(pk_home, fake_post, with_token, monkeypatch):
    fake_post()
    line = publik_api.self_test()
    assert line == f"publik self-test: ok publikreach {publik_api.APP_VERSION} (install abcdef12)"
    assert SECRET not in line
    monkeypatch.setattr(publik_api, "_post", lambda u, b: (500, {"error": {"message": "boom"}}))
    assert publik_api.self_test() == "publik self-test: failed: boom"


def test_self_test_env_hook_in_cli_exits_before_anything(pk_home, fake_post, with_token, monkeypatch, capsys):
    fake_post()
    monkeypatch.setenv("PUBLIK_SELF_TEST", "1")
    from vcoutreach import cli

    with pytest.raises(SystemExit) as ex:
        cli.main(["--help"])
    assert ex.value.code == 0
    assert capsys.readouterr().out.startswith("publik self-test: ok publikreach")


def test_balance_line_and_402_helper():
    assert publik_api.balance_line(0) == "$0.00 · link this computer for $0.05 of free use"
    assert publik_api.balance_line(1_500_000, "claimed") == "$1.50 of publik balance"
    assert publik_api.balance_line(None) == ""
    msg, url = publik_api.top_up_url_from_402({"error": {"message": "Low", "top_up_url": "https://publikhq.com/top"}})
    assert (msg, url) == ("Low", "https://publikhq.com/top")
    assert publik_api.top_up_url_from_402({}) == ("Not enough publik API balance.", None)


def test_provider_402_carries_top_up_url(kit, pk_home):
    ft = kit.fake_transport(kit.error_response(402, "Out of credit", top_up_url="https://publikhq.com/top"))
    provider = PublikProvider(kit.publik_settings(), transport=ft, sleep=kit.sleeper(), rng=kit.rng(0.0))
    with pytest.raises(ProviderCreditError) as err:
        provider.personalize(kit.request())
    assert err.value.top_up_url == "https://publikhq.com/top"


# ---- web routes -----------------------------------------------------------------------------------------------

def _no_secret(*responses):
    for r in responses:
        assert SECRET.encode() not in r.data


def test_status_route_before_and_after_provision(web, pk_home, fake_post, with_token):
    before = web.get("/api/publik/status")
    assert before.status_code == 200
    assert before.get_json() == {"configured": False, "balance_line": "", "claim_url": "", "claim_state": "",
                                 "add_credit_url": "", "cost_sentence": ""}
    fake_post()
    done = web.post("/api/publik/provision", headers={"Accept": "application/json"})
    assert done.status_code == 200
    d = done.get_json()
    assert d["ok"] is True and d["claim_url"] == "https://publikhq.com/claim/xyz"
    assert d["balance_line"].startswith("$0.00") and d["cost_sentence"] == "Costs are priced per use."
    after = web.get("/api/publik/status")
    assert after.get_json()["configured"] is True
    assert "key" not in after.get_json() and "key" not in d
    page = web.get("/settings")
    assert "Set up publik API" in page.get_data(as_text=True)
    assert "Sign in with publik" in page.get_data(as_text=True)
    _no_secret(before, done, after, page, web.get("/setup/ai"))


def test_claimed_state_shows_add_plan_button(web, pk_home):
    _save_cred(claim_state="claimed", balance_micros=2_000_000, add_credit_url="https://publikhq.com/add-credit")
    html = web.get("/settings").get_data(as_text=True)
    assert "Add a plan or pack" in html and "https://publikhq.com/add-credit" in html
    assert "$2.00 of publik balance" in html
    _no_secret(web.get("/api/publik/status"))


def test_provision_failure_shows_readable_message_and_own_key_hint(web, pk_home, monkeypatch):
    monkeypatch.delenv("PUBLIK_APP_TOKEN", raising=False)
    monkeypatch.setattr(publik_api, "_token_dirs", lambda: [])
    r = web.post("/api/publik/provision", headers={"Accept": "application/json"})
    d = r.get_json()
    assert d["ok"] is False and "no publik API app token" in d["message"] and "own" in d["hint"]
    assert web.get("/api/publik/status").get_json()["configured"] is False


def test_provision_requires_csrf(web, pk_home):
    assert web.post("/api/publik/provision", csrf=False).status_code != 200


def test_probe_402_returns_one_top_up_url(web, pk_home, monkeypatch):
    from vcoutreach.web.server import mcp_routes

    monkeypatch.setattr(mcp_routes, "_run_probe", lambda key: {
        "ok": False, "message": mcp_routes.PROBE_CREDIT, "top_up_url": "https://publikhq.com/top"})
    _save_cred()
    d = web.post("/api/publik/test").get_json()
    assert d["ok"] is False and d["top_up_url"] == "https://publikhq.com/top"
    assert mcp_routes._safe_top_up("javascript:alert(1)") == ""
    assert mcp_routes._safe_top_up("http://insecure.example") == ""
