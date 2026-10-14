"""SPEC section 22.8, 22.9 and 22.11: console dashes, exit codes of stray errors and of a failing mail transport, negative --limit."""
from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
import types

import pytest

from vcoutreach.types import OutreachError

EM = chr(0x2014)
EN = chr(0x2013)
SEND_ENV = {"SEND_MODE": "test", "TEST_RECIPIENT_EMAIL": "me@mail.test", "SENDER_EMAIL": "sam@lumenlist.example.com"}
GOOGLE_NAMES = ("google", "googleapiclient", "google_auth_oauthlib", "google_auth_httplib2", "httplib2", "oauthlib")


def person(kit, ident, name="Ada Lovelace", **over):
    base = dict(id=ident, name=name, email=f"{ident}@example.com", org="Northwind",
                facts="your essay on how small teams ship weekly")
    base.update(over)
    return kit.csv_row(**base)


def draft_offline(kit, rows, *extra):
    contacts = kit.write_csv(rows, name="people.csv")
    sender = kit.write_sender()
    return kit.cli("draft", "--contacts", contacts.name, "--sender", sender.name, "--offline", *extra)


def assert_no_dashes(res):
    assert EM not in res.out and EM not in res.err, "an em dash was printed"
    assert EN not in res.out and EN not in res.err, "an en dash was printed"


# ----- 22.8 the console never shows an em dash or an en dash ---------------------------------------------------------------------

@pytest.mark.parametrize("dash,name", [(EM, "em"), (EN, "en")], ids=["em-dash", "en-dash"])
def test_an_invalid_email_that_contains_a_dash_is_echoed_with_a_hyphen(kit, dash, name):
    res = draft_offline(kit, [person(kit, "ada", email=f"ada{dash}x.example.com")])  # no at sign, so it is invalid
    assert res.code == 2, res.combined
    assert_no_dashes(res)
    if "ada" in res.combined:
        assert "ada-x.example.com" in res.combined


@pytest.mark.parametrize("dash", [EM, EN], ids=["em-dash", "en-dash"])
def test_an_unknown_provider_name_with_a_dash_is_echoed_with_a_hyphen(kit, dash):
    kit.cli("init")
    res = kit.cli("draft", "--contacts", "contacts.sample.csv", "--sender", "sender.sample.json", "--provider", f"foo{dash}bar")
    assert res.code == 2, res.combined
    assert_no_dashes(res)
    if "foo" in res.combined:
        assert "foo-bar" in res.combined


@pytest.mark.parametrize("dash", [EM, EN], ids=["em-dash", "en-dash"])
def test_a_missing_file_name_with_a_dash_is_echoed_with_a_hyphen(kit, dash):
    kit.write_sender()
    res = kit.cli("draft", "--contacts", f"missing{dash}file.csv", "--sender", "sender.json", "--offline")
    assert res.code == 2, res.combined
    assert_no_dashes(res)


@pytest.mark.parametrize("dash", [EM, EN], ids=["em-dash", "en-dash"])
def test_an_invalid_lane_or_id_with_a_dash_is_not_echoed_with_it(kit, dash):
    lane = draft_offline(kit, [person(kit, "ada", lane=f"first{dash}touch")])
    ident = draft_offline(kit, [person(kit, f"a{dash}b")])
    for res in (lane, ident):
        assert_no_dashes(res)
        assert "Traceback" not in res.combined


def test_ordinary_hyphens_are_left_alone(kit):
    res = draft_offline(kit, [person(kit, "ada", email="ada-x@example.com")])
    assert res.code == 0, res.combined
    assert_no_dashes(res)


# ----- 22.11 a negative --limit is an error ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["-1", "-5"])
def test_a_negative_limit_is_rejected_by_argparse_with_exit_2(kit, value):
    kit.quickstart()
    res = kit.cli("send", "--drafts", "drafts", "--limit", value, env=SEND_ENV)
    assert res.code == 2, res.combined
    assert "must be zero or greater" in res.combined
    assert "Traceback" not in res.combined
    assert not (kit.workdir / ".vcoutreach" / "journal.jsonl").exists()


def test_a_limit_of_zero_is_accepted_and_plans_nothing(kit):
    kit.quickstart()
    res = kit.cli("send", "--drafts", "drafts", "--limit", "0", env=SEND_ENV)
    assert res.code == 0, res.combined
    assert "me@mail.test" not in res.out


# ----- 22.9 exit codes: a failing mail transport is 3, a stray OSError or ValueError is 2 ----------------------------------------

def run_main(*argv):
    from vcoutreach.cli import main

    try:
        result = main(list(argv))
    except SystemExit as exc:
        result = exc.code
    return 0 if result is None else result


class _FakeModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *args, **kwargs: None


class _FakeLoader(importlib.abc.Loader):
    def create_module(self, spec):
        module = _FakeModule(spec.name)
        module.__path__ = []
        return module

    def exec_module(self, module):
        return None


class _FakeGoogleFinder(importlib.abc.MetaPathFinder):
    """Makes every module under the optional Google packages importable (and findable), whatever the check asks for."""

    def find_spec(self, name, path, target=None):
        if name.split(".")[0] in GOOGLE_NAMES:
            return importlib.machinery.ModuleSpec(name, _FakeLoader(), is_package=True)
        return None


@pytest.fixture
def fake_google(monkeypatch):
    before = {n for n in sys.modules if n.split(".")[0] in GOOGLE_NAMES}
    monkeypatch.setattr(sys, "meta_path", [_FakeGoogleFinder()] + list(sys.meta_path))
    yield
    for name in [n for n in sys.modules if n.split(".")[0] in GOOGLE_NAMES and n not in before]:
        del sys.modules[name]


def inject_transport(monkeypatch, behavior):
    import vcoutreach.cli as cli_module
    import vcoutreach.send.gmail as gmail_module

    class StubTransport:
        instances = []

        def __init__(self, settings):
            self.settings = settings
            self.sent = []
            StubTransport.instances.append(self)

        def send(self, message):
            result = behavior(message)
            self.sent.append(message)
            return result

    monkeypatch.setattr(gmail_module, "GmailTransport", StubTransport)
    if hasattr(cli_module, "GmailTransport"):
        monkeypatch.setattr(cli_module, "GmailTransport", StubTransport)
    return StubTransport


def send_env(monkeypatch):
    for name, value in SEND_ENV.items():
        monkeypatch.setenv(name, value)


def test_the_stub_transport_seam_works_when_every_send_succeeds(kit, monkeypatch, capsys, fake_google):
    from vcoutreach.send.gate import SendReceipt

    kit.quickstart()
    send_env(monkeypatch)
    stub = inject_transport(monkeypatch, lambda message: SendReceipt(message_id="<stub@mail.test>"))
    assert run_main("send", "--drafts", "drafts", "--execute") == 0
    assert len(stub.instances) == 1 and len(stub.instances[0].sent) == 4
    assert len(kit.journal_lines()) == 4


def test_an_outreach_error_from_the_mail_transport_during_execute_exits_3(kit, monkeypatch, capsys, fake_google):
    def fail(message):
        raise OutreachError("simulated transport failure")

    kit.quickstart()
    send_env(monkeypatch)
    inject_transport(monkeypatch, fail)
    capsys.readouterr()
    code = run_main("send", "--drafts", "drafts", "--execute")
    captured = capsys.readouterr()
    assert code == 3, captured.out + captured.err
    assert "simulated transport failure" in captured.err + captured.out
    assert "Traceback" not in captured.err + captured.out
    assert kit.journal_lines() == [], "a failed send is not journaled"


@pytest.mark.parametrize("error", [OSError("disk on fire"), PermissionError("no access"), ValueError("odd value"),
                                   UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")],
                         ids=["oserror", "permission", "valueerror", "unicode"])
def test_a_stray_oserror_or_valueerror_exits_2_not_3(kit, monkeypatch, capsys, error):
    import vcoutreach.cli as cli_module
    import vcoutreach.config as config_module

    def boom(*args, **kwargs):
        raise error

    monkeypatch.setattr(config_module, "load_settings", boom)
    if hasattr(cli_module, "load_settings"):
        monkeypatch.setattr(cli_module, "load_settings", boom)
    capsys.readouterr()
    code = run_main("providers")
    captured = capsys.readouterr()
    assert code == 2, captured.out + captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("error", [OSError("disk on fire"), ValueError("odd value")], ids=["oserror", "valueerror"])
def test_a_stray_error_while_reading_the_drafts_exits_2_too(kit, monkeypatch, capsys, error):
    import vcoutreach.cli as cli_module
    import vcoutreach.drafts_io as drafts_module

    def boom(*args, **kwargs):
        raise error

    kit.quickstart()
    send_env(monkeypatch)
    monkeypatch.setattr(drafts_module, "read_drafts", boom)
    if hasattr(cli_module, "read_drafts"):
        monkeypatch.setattr(cli_module, "read_drafts", boom)
    capsys.readouterr()
    code = run_main("send", "--drafts", "drafts")
    captured = capsys.readouterr()
    assert code == 2, captured.out + captured.err


def test_a_draft_file_with_bytes_that_are_not_utf8_never_exits_3(kit):
    kit.quickstart()
    (kit.workdir / "drafts" / "zzz-bad.md").write_bytes(b"to: a@example.com\nsubject: \xff\xfe\n\n\xff body")
    for command in (("send", "--drafts", "drafts"), ("lint", "--drafts", "drafts")):
        res = kit.cli(*command, env=SEND_ENV)
        assert res.code != 3, res.combined
        assert "Traceback" not in res.combined, res.combined
