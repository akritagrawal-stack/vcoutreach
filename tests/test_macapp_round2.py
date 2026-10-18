"""Round two contracts, exercised without reading either implementation source.

The numeric test prefixes map directly to the supplied behavior specification.
Only loopback servers are permitted by the repository network guard.
"""

import builtins
import importlib
import io
import json
import re
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace
from urllib.parse import urlsplit

import pytest

REPO = Path(__file__).resolve().parents[1]
APP_DIR = REPO / "packaging" / "mac" / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import runtime
from vcoutreach.web import create_app
from vcoutreach.web.gmail import api as gmail_api
from vcoutreach.web.gmail import setup as gmail_setup

pytestmark = pytest.mark.local_http
BASE = "http://127.0.0.1:7654"
CSRF = "round-two-csrf"


def good_client(secret="round-two-client-secret"):
    return json.dumps({"installed": {
        "client_id": "1234567890-abcdef.apps.googleusercontent.com",
        "client_secret": secret,
        "project_id": "demo",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }}).encode()


@pytest.fixture
def workspace(tmp_path):
    return runtime.bootstrap_workspace(tmp_path / "ws")


class GmailHarness:
    def __init__(self, workspace):
        self.workspace = workspace
        self.app = create_app(workspace.root)
        self.app.config.update(PORT=7654, TESTING=False)
        self.setup = self.app.extensions["gmail_setup"]
        self.client = self.app.test_client()
        response = self.client.get(
            "/?token=" + self.app.config["ACCESS_TOKEN"], base_url=BASE)
        assert response.status_code == 302
        with self.client.session_transaction(base_url=BASE) as session:
            self.csrf = session["csrf"]
        self.credentials = workspace.root / "data" / ".gmail_credentials.json"
        self.token = workspace.root / "data" / ".gmail_token.json"

    def post(self, route, *, headers=None, **kwargs):
        actual = {"X-CSRF-Token": self.csrf}
        actual.update({"Origin": BASE} if headers is None else headers)
        return self.client.post("/api/gmail/" + route, base_url=BASE,
                                headers=actual, **kwargs)

    def upload(self, raw, **kwargs):
        return self.post("client", data={"oauth_client": (io.BytesIO(raw), "client.json")},
                         **kwargs)

    def status(self):
        response = self.client.get("/api/gmail/status", base_url=BASE)
        assert response.status_code == 200
        result = response.get_json()
        assert type(result["running"]) is bool and type(result["failed"]) is bool
        return result

    def snapshot(self):
        return ({p.name: p.read_bytes() for p in (self.credentials, self.token)
                 if p.exists()}, self.status())


@pytest.fixture
def gmail(workspace):
    return GmailHarness(workspace)


class Consent:
    """Real threads execute the real setup worker around a gated OAuth function."""
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.threads = []
        self.calls = []
        self.fail = False

    def flow(self):
        self.calls.append(threading.current_thread())
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("test consent gate timed out")
        if self.fail:
            raise RuntimeError("private OAuth failure")
        return "local@example.test"

    def thread(self, *args, **kwargs):
        thread = threading.Thread(*args, **kwargs)
        self.threads.append(thread)
        return thread

    def begin(self, gmail):
        response = gmail.post("connect")
        assert response.status_code == 202, response.get_json()
        assert self.entered.wait(2), "consent thread did not enter the OAuth seam"
        return response

    def finish(self):
        self.release.set()
        for thread in self.threads:
            if thread.ident is not None:
                thread.join(2)
                assert not thread.is_alive(), "consent thread failed to finish"


@pytest.fixture
def consent(monkeypatch):
    control = Consent()
    monkeypatch.setattr(gmail_api, "run_oauth_flow", control.flow)
    # There is no consent opener in dir(gmail_setup). Its thread is the seam.
    monkeypatch.setattr(gmail_setup, "threading", SimpleNamespace(
        Thread=control.thread, Lock=threading.Lock))
    yield control
    control.finish()


def fail_connect(gmail, consent):
    gmail.setup.import_client(good_client())
    consent.fail = True
    consent.begin(gmail)
    consent.finish()
    assert gmail.status()["failed"] is True


def test_01_http_replacement_deletes_old_token(gmail):
    gmail.setup.import_client(good_client())
    gmail.token.write_text('{"refresh_token":"old"}')
    response = gmail.upload(good_client("replacement-secret"))
    assert response.status_code == 200 and response.get_json()["ok"] is True
    assert not gmail.token.exists()


@pytest.mark.parametrize("extra", [0, 1], ids=["exact-limit", "limit-plus-one"])
def test_02_copy_oauth_client_size_boundary(workspace, tmp_path, extra):
    raw = good_client()
    path = tmp_path / "client.json"
    path.write_bytes(raw + b" " * (gmail_setup.MAX_CLIENT_BYTES + extra - len(raw)))
    if extra:
        with pytest.raises(ValueError):
            gmail_setup.copy_oauth_client(path, workspace)
    else:
        gmail_setup.copy_oauth_client(path, workspace)
        assert json.loads((workspace.root / "data" / ".gmail_credentials.json").read_bytes()) == json.loads(raw)


@pytest.mark.parametrize("extra", [0, 1], ids=["exact-limit", "limit-plus-one"])
def test_02_import_file_size_boundary(gmail, tmp_path, extra):
    raw = good_client()
    path = tmp_path / "client.json"
    path.write_bytes(raw + b" " * (gmail_setup.MAX_CLIENT_BYTES + extra - len(raw)))
    if extra:
        with pytest.raises(ValueError):
            gmail.setup.import_file(path)
    else:
        gmail.setup.import_file(path)
        assert json.loads(gmail.credentials.read_bytes()) == json.loads(raw)


class ReadSpy:
    def __init__(self, handle, calls):
        self.handle, self.calls = handle, calls

    def read(self, size=-1):
        result = self.handle.read(size)
        self.calls.append((size, len(result)))
        return result

    def __enter__(self):
        self.handle.__enter__()
        return self

    def __exit__(self, *args):
        return self.handle.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.handle, name)


def test_03_sparse_import_rejected_with_bounded_read(gmail, tmp_path, monkeypatch):
    path = tmp_path / "sparse.json"
    with path.open("wb") as handle:
        handle.write(good_client())
        handle.truncate(50 * 1024 * 1024)
    calls = []

    def wrap(original):
        def opened(file, *args, **kwargs):
            handle = original(file, *args, **kwargs)
            return ReadSpy(handle, calls) if file == path or file == str(path) else handle
        return opened

    monkeypatch.setattr(builtins, "open", wrap(builtins.open))
    monkeypatch.setattr(io, "open", wrap(io.open))
    with pytest.raises(ValueError):
        gmail.setup.import_file(path)
    assert calls, "the file read spy did not observe a bounded read"
    assert all(0 <= size <= gmail_setup.MAX_CLIENT_BYTES + 1 for size, _ in calls)
    assert sum(count for _, count in calls) <= gmail_setup.MAX_CLIENT_BYTES + 1
    assert not gmail.credentials.exists()


def test_04_running_import_refused_preserves_bytes(gmail, consent):
    gmail.setup.import_client(good_client())
    before = gmail.credentials.read_bytes()
    consent.begin(gmail)
    with pytest.raises((ValueError, RuntimeError)):
        gmail.setup.import_client(good_client("changed-secret"))
    assert gmail.credentials.read_bytes() == before


def test_05_successful_import_clears_failed(gmail, consent):
    fail_connect(gmail, consent)
    gmail.setup.import_client(good_client("new-secret"))
    assert gmail.status()["failed"] is False


def test_06_running_disconnect_409_preserves_both_files(gmail, consent):
    gmail.setup.import_client(good_client())
    gmail.token.write_text('{"refresh_token":"keep"}')
    before = gmail.snapshot()[0]
    consent.begin(gmail)
    assert gmail.post("disconnect").status_code == 409
    assert gmail.snapshot()[0] == before


def test_07_disconnect_deletes_token(gmail):
    gmail.setup.import_client(good_client())
    gmail.token.write_text("token")
    assert gmail.post("disconnect").status_code == 200
    assert not gmail.token.exists()


def test_07_disconnect_deletes_credentials(gmail):
    gmail.setup.import_client(good_client())
    assert gmail.credentials.exists()
    assert gmail.post("disconnect").status_code == 200
    assert not gmail.credentials.exists()


def test_07_disconnect_clears_failed(gmail, consent):
    fail_connect(gmail, consent)
    assert gmail.post("disconnect").status_code == 200
    assert gmail.status()["failed"] is False


def test_08_two_connects_start_one_thread(gmail, consent):
    gmail.setup.import_client(good_client())
    consent.begin(gmail)
    gmail.post("connect")
    assert len(consent.threads) == 1
    assert len(consent.calls) == 1


def test_09_tampered_client_connect_400_no_flow(gmail, consent):
    gmail.setup.import_client(good_client())
    data = json.loads(gmail.credentials.read_bytes())
    data["installed"]["token_uri"] = "https://evil.example/token"
    gmail.credentials.write_text(json.dumps(data))
    assert gmail.post("connect").status_code == 400
    assert consent.threads == [] and consent.calls == []


def test_10_connect_immediate_status_running_not_failed(gmail, consent):
    gmail.setup.import_client(good_client())
    consent.begin(gmail)
    state = gmail.status()
    assert state["running"] is True and state["failed"] is False


def test_10_retry_clears_failed_while_running(gmail, consent):
    fail_connect(gmail, consent)
    consent.release.clear()
    consent.entered.clear()
    consent.fail = False
    consent.begin(gmail)
    state = gmail.status()
    assert state["running"] is True and state["failed"] is False


def test_11_consent_exception_sets_failed_and_stops(gmail, consent):
    fail_connect(gmail, consent)
    state = gmail.status()
    assert state["failed"] is True and state["running"] is False


@pytest.mark.parametrize("fail", [False, True], ids=["success", "failure"])
def test_12_finished_flow_can_connect_again(gmail, consent, fail):
    gmail.setup.import_client(good_client())
    consent.fail = fail
    consent.begin(gmail)
    consent.finish()
    assert gmail.status()["running"] is False
    consent.entered.clear()
    response = gmail.post("connect")
    assert response.status_code == 202
    assert consent.entered.wait(2)
    consent.finish()
    assert len(consent.calls) == 2


class BrokenThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        raise RuntimeError("cannot start consent thread")


def test_13_thread_start_error_propagates(gmail, consent, monkeypatch):
    gmail.setup.import_client(good_client())
    monkeypatch.setattr(gmail_setup.threading, "Thread", BrokenThread)
    response = gmail.post("connect")
    assert response.status_code in (400, 500)
    assert response.get_json()["ok"] is False


def test_13_thread_start_error_resets_running_and_allows_retry(gmail, consent, monkeypatch):
    gmail.setup.import_client(good_client())
    monkeypatch.setattr(gmail_setup.threading, "Thread", BrokenThread)
    gmail.post("connect")
    assert gmail.status()["running"] is False
    monkeypatch.setattr(gmail_setup.threading, "Thread", consent.thread)
    consent.begin(gmail)


@pytest.mark.parametrize("headers,accepted", [
    ({"Origin": "http://other.example:7654"}, False),
    ({"Origin": "http://127.0.0.1:7655"}, False),
    ({"Origin": "https://127.0.0.1:7654"}, False),
    ({"Origin": BASE}, True),
    ({"Origin": "http://evil.example:7654", "Referer": BASE + "/settings"}, False),
    ({}, False),
    ({"Referer": BASE + "/settings?tab=gmail"}, True),
    ({"Referer": "http://other.example:7654/settings"}, False),
], ids=["other-host", "other-port", "other-scheme", "same-origin", "evil-origin-wins",
        "missing-both", "same-referer", "other-referer"])
@pytest.mark.parametrize("route", ["client", "connect", "disconnect"])
def test_14_same_origin_guard_all_mutations(gmail, consent, headers, accepted, route):
    gmail.setup.import_client(good_client())
    gmail.token.write_text("keep-token")
    before = gmail.snapshot()
    response = (gmail.upload(good_client("replacement"), headers=headers)
                if route == "client" else gmail.post(route, headers=headers))
    if accepted:
        assert response.status_code == (202 if route == "connect" else 200)
    else:
        assert response.status_code == 400
        assert gmail.snapshot() == before
        assert consent.threads == []


def test_15_upload_missing_file_400_not_ok(gmail):
    response = gmail.post("client")
    assert response.status_code == 400 and response.get_json()["ok"] is False


def test_15_upload_invalid_file_400_not_ok(gmail):
    response = gmail.upload(b"invalid JSON")
    assert response.status_code == 400 and response.get_json()["ok"] is False
    assert not gmail.credentials.exists()


def test_15_huge_upload_rejected_with_bounded_read(gmail, tmp_path, monkeypatch):
    from flask import has_request_context
    from werkzeug.datastructures import FileStorage

    path = tmp_path / "huge-upload.json"
    with path.open("wb") as handle:
        handle.write(good_client())
        handle.truncate(50 * 1024 * 1024)
    calls = []
    def read(storage, size=-1):
        result = storage.stream.read(size)
        if has_request_context():
            calls.append((size, len(result)))
        return result

    # Werkzeug streams the multipart body into a spool before route-level reads.
    monkeypatch.setattr(FileStorage, "read", read, raising=False)
    with path.open("rb") as handle:
        response = gmail.post("client", data={"oauth_client": (handle, "huge.json")})
    assert response.status_code in (400, 413)
    assert calls or response.status_code == 413, "route read spy did not observe a read"
    assert all(0 <= size <= gmail_setup.MAX_CLIENT_BYTES + 1 for size, _ in calls)
    assert sum(count for _, count in calls) <= gmail_setup.MAX_CLIENT_BYTES + 1
    assert not gmail.credentials.exists()


def test_16_valid_client_connect_202_running(gmail, consent):
    gmail.setup.import_client(good_client())
    response = consent.begin(gmail)
    assert response.get_json()["running"] is True


@pytest.mark.parametrize("raw", [None, b"not valid"], ids=["missing", "invalid"])
def test_16_missing_or_invalid_client_connect_400(gmail, consent, raw):
    if raw is not None:
        gmail.credentials.write_bytes(raw)
    assert gmail.post("connect").status_code == 400
    assert consent.threads == []


def test_17_disconnect_oserror_500_not_ok(gmail, monkeypatch):
    gmail.setup.import_client(good_client())
    gmail.token.write_text("keep")
    original = Path.unlink

    def broken(path, *args, **kwargs):
        if path in (gmail.token, gmail.credentials):
            raise OSError("cannot remove file")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", broken)
    response = gmail.post("disconnect")
    assert response.status_code == 500 and response.get_json()["ok"] is False


def test_18_status_failed_after_flow(gmail, consent):
    fail_connect(gmail, consent)
    assert gmail.status()["failed"] is True


def test_18_status_running_during_flow(gmail, consent):
    gmail.setup.import_client(good_client())
    consent.begin(gmail)
    assert gmail.status()["running"] is True


@pytest.fixture
def app_main(monkeypatch):
    # Fakes are installed through sys.modules even if a platform package exists.
    webview = ModuleType("webview")
    webview.FileDialog = SimpleNamespace(OPEN=object())
    monkeypatch.setitem(sys.modules, "webview", webview)
    return importlib.import_module("app_main")


@pytest.fixture
def native(app_main, gmail):
    calls = []

    class Window:
        url = BASE + "/settings"
        result = ["/chosen/first.json", "/chosen/second.json"]
        during_dialog = None

        def get_current_url(self):
            return self.url

        def create_file_dialog(self, kind, **kwargs):
            calls.append((kind, kwargs))
            if self.during_dialog:
                self.during_dialog()
            return self.result

    window = Window()
    api = app_main.NativeApi(gmail.app, BASE, CSRF)
    api._window = window
    return SimpleNamespace(api=api, window=window, dialogs=calls, gmail=gmail)


@pytest.mark.parametrize("path", ["/", "/crm", "/replies", "/settings/", "/setup/child"])
def test_19_unauthorized_page_refuses_dialog(native, path):
    native.window.url = BASE + path
    assert native.api.choose_oauth_client(CSRF)["ok"] is False
    assert native.dialogs == []


@pytest.mark.parametrize("path", ["/settings", "/knowledge", "/setup"])
def test_19_exact_allowed_page_opens_dialog(native, monkeypatch, path):
    imported = []
    monkeypatch.setattr(native.gmail.setup, "import_file", imported.append)
    native.window.url = BASE + path
    assert native.api.choose_oauth_client(CSRF)["ok"] is True
    assert len(native.dialogs) == 1 and len(imported) == 1


@pytest.mark.parametrize("origin", ["http://other.example:7654", "http://127.0.0.1:7655"],
                         ids=["host", "port"])
def test_20_other_origin_refuses_dialog(native, origin):
    native.window.url = origin + "/settings"
    assert native.api.choose_oauth_client(CSRF)["ok"] is False
    assert native.dialogs == []


@pytest.mark.parametrize("csrf", ["wrong", None, 7, ""], ids=["wrong", "none", "int", "empty"])
def test_21_bad_csrf_refuses_dialog(native, csrf):
    assert native.api.choose_oauth_client(csrf)["ok"] is False
    assert native.dialogs == []


def test_21_correct_csrf_accepted(native, monkeypatch):
    monkeypatch.setattr(native.gmail.setup, "import_file", lambda path: None)
    assert native.api.choose_oauth_client(CSRF)["ok"] is True
    assert len(native.dialogs) == 1


def test_22_navigation_during_dialog_prevents_import(native, monkeypatch):
    imported = []
    monkeypatch.setattr(native.gmail.setup, "import_file", imported.append)
    native.window.during_dialog = lambda: setattr(native.window, "url", BASE + "/crm")
    assert native.api.choose_oauth_client(CSRF)["ok"] is False
    assert len(native.dialogs) == 1 and imported == []


@pytest.mark.parametrize("result", [None, [], ()], ids=["none", "empty-list", "empty-tuple"])
def test_23_cancelled_dialog_imports_nothing(native, monkeypatch, result):
    imported = []
    monkeypatch.setattr(native.gmail.setup, "import_file", imported.append)
    native.window.result = result
    response = native.api.choose_oauth_client(CSRF)
    assert response["ok"] is False and response["cancelled"] is True
    assert imported == []


def test_24_selection_imports_first_path_only(native, monkeypatch, tmp_path):
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    first.write_bytes(good_client())
    second.write_bytes(good_client("other-client"))
    imported = []
    original = native.gmail.setup.import_file

    def record(path):
        imported.append(Path(path))
        return original(path)

    monkeypatch.setattr(native.gmail.setup, "import_file", record)
    native.window.result = [str(first), str(second)]
    assert native.api.choose_oauth_client(CSRF)["ok"] is True
    assert imported == [first]
    assert json.loads(native.gmail.credentials.read_bytes()) == json.loads(first.read_bytes())


def test_25_import_error_is_generic(native, monkeypatch):
    marker = "private-import-failure-marker"
    private = marker + ": " + native.window.result[0]

    def fail(path):
        raise ValueError(private)

    monkeypatch.setattr(native.gmail.setup, "import_file", fail)
    result = native.api.choose_oauth_client(CSRF)
    assert result["ok"] is False and result.get("error")
    assert marker not in json.dumps(result)
    assert private not in json.dumps(result)
    assert native.window.result[0] not in json.dumps(result)


def test_26_dialog_open_single_json_only(native, monkeypatch):
    monkeypatch.setattr(native.gmail.setup, "import_file", lambda path: None)
    native.api.choose_oauth_client(CSRF)
    kind, kwargs = native.dialogs[0]
    assert kind is sys.modules["webview"].FileDialog.OPEN
    assert kwargs["allow_multiple"] is False
    types = kwargs["file_types"]
    assert isinstance(types, tuple) and types
    assert all("json" in value.lower() and "*.json" in value.lower() for value in types)


def test_27_both_js_methods_use_original_run_once(app_main):
    original_calls, evaluate_calls = [], []

    def original(script):
        original_calls.append(script)
        return "original-result"

    window = SimpleNamespace(run_js=original, evaluate_js=evaluate_calls.append)
    app_main.configure_csp_bridge(window)
    for method, script in [(window.run_js, "first();"), (window.evaluate_js, "second();")]:
        before = len(original_calls)
        method(script)
        assert len(original_calls) == before + 1
        assert original_calls[-1] == script + "\n" + app_main.CSP_BRIDGE
        assert original_calls[-1].count(app_main.CSP_BRIDGE) == 1
    assert evaluate_calls == []


def test_28_bridge_exposes_only_choose_oauth_client(app_main):
    bridge = app_main.CSP_BRIDGE
    assert set(re.findall(r"bridge\.api\.(\w+)\s*=", bridge)) == {"choose_oauth_client"}
    assert set(re.findall(r"_returnValuesCallbacks\.(\w+)\s*=", bridge)) == {"choose_oauth_client"}


def execute_bridge(app_main, csrf):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable: JavaScript execution only")
    script = """
const calls = [];
const window = {pywebview: {
  api: {}, _returnValuesCallbacks: {},
  _checkValue: function(name, resolve, reject, id) { resolve('ok'); },
  _jsApiCallback: function(name, args, id) { calls.push([name, args, id]); }
}};
""" + app_main.CSP_BRIDGE + "\n" + """
window.pywebview._createApi();
window.pywebview.api.choose_oauth_client(JSON.parse(process.argv[1]));
console.log(JSON.stringify(calls));
"""
    result = subprocess.run([node, "-e", script, json.dumps(csrf)],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_28_bridge_invokes_native_callback(app_main):
    calls = execute_bridge(app_main, CSRF)
    assert len(calls) == 1
    name, args, ident = calls[0]
    assert name == "choose_oauth_client" and args == [CSRF]
    assert isinstance(ident, str) and ident


@pytest.mark.parametrize("csrf", ["argument-csrf-one", "argument-csrf-two"])
def test_29_bridge_passes_actual_csrf_argument(app_main, csrf):
    assert execute_bridge(app_main, csrf)[0][1] == [csrf]


def test_29_bridge_forwards_parameter_text(app_main):
    text = app_main.CSP_BRIDGE
    parameter = re.search(r"bridge\.api\.choose_oauth_client\s*=\s*function\s*\(\s*(\w+)\s*\)", text)
    assert parameter, "bridge must accept a CSRF argument"
    assert re.search(r"_jsApiCallback\(\s*['\"]choose_oauth_client['\"]\s*,\s*\[\s*"
                     + re.escape(parameter.group(1)) + r"\s*\]\s*,", text)


def assert_lock_free(root):
    probe = runtime.SingleInstanceLock(root / "data" / ".app.lock")
    try:
        assert probe.acquire(), "main left its workspace lock held"
    finally:
        probe.release()


@pytest.fixture
def alerts(monkeypatch):
    shown = []

    class Alert:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            self.message = ""
            self.information = ""
            return self

        def setMessageText_(self, text):
            self.message = text

        def setInformativeText_(self, text):
            self.information = text

        def runModal(self):
            shown.append(self.message + " " + self.information)

    module = ModuleType("AppKit")
    module.NSAlert = Alert
    monkeypatch.setitem(sys.modules, "AppKit", module)
    return shown


def test_30_second_instance_returns_zero_without_launch(app_main, workspace, monkeypatch):
    calls = []
    monkeypatch.setattr(app_main, "launch", calls.append)
    holder = runtime.SingleInstanceLock(workspace.root / "data" / ".app.lock")
    assert holder.acquire()
    try:
        assert app_main.main(["--workspace", str(workspace.root)]) == 0
        assert calls == []
    finally:
        holder.release()


@pytest.mark.parametrize("raises", [False, True], ids=["normal", "launch-error"])
def test_31_main_releases_lock(app_main, workspace, monkeypatch, alerts, raises):
    calls = []

    def launch(ws):
        calls.append(ws)
        if raises:
            raise RuntimeError("injected GUI failure")

    monkeypatch.setattr(app_main, "launch", launch)
    assert app_main.main(["--workspace", str(workspace.root)]) == (1 if raises else 0)
    assert len(calls) == 1
    assert_lock_free(workspace.root)


@pytest.mark.parametrize("exists", [False, True], ids=["new-data", "existing-data"])
def test_32_main_creates_or_reuses_data_directory(app_main, tmp_path, monkeypatch, exists):
    root = tmp_path / "workspace"
    if exists:
        (root / "data").mkdir(parents=True)
        (root / "data" / "keep").write_text("unchanged")
    calls = []
    monkeypatch.setattr(app_main, "launch", calls.append)
    assert app_main.main(["--workspace", str(root)]) == 0
    assert (root / "data").is_dir() and len(calls) == 1
    if exists:
        assert (root / "data" / "keep").read_text() == "unchanged"


@pytest.mark.parametrize("explicit", [False, True], ids=["default", "explicit"])
def test_33_workspace_selection_and_bootstrap_result(app_main, tmp_path, monkeypatch, explicit):
    home = tmp_path / "home-for-main"
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "chosen" if explicit else home / "Documents" / "Publikreach"
    bootstraps, launches = [], []
    original = app_main.bootstrap_workspace

    def bootstrap(selected=None):
        ws = original(selected)
        bootstraps.append((Path(selected) if selected is not None else Path.home() / "Documents" / "Publikreach", ws))
        return ws

    monkeypatch.setattr(app_main, "bootstrap_workspace", bootstrap)
    monkeypatch.setattr(app_main, "launch", launches.append)
    assert app_main.main(["--workspace", str(root)] if explicit else []) == 0
    assert len(bootstraps) == 1 and bootstraps[0][0] == root
    assert len(launches) == 1 and launches[0] is bootstraps[0][1]
    assert launches[0].root == root


@pytest.mark.parametrize("explicit", [False, True], ids=["temporary", "explicit"])
def test_34_self_test_failure_headless_and_workspace(app_main, tmp_path, monkeypatch, capsys, alerts, explicit):
    paths = []
    root = tmp_path / "explicit-self-test"

    def fail(path):
        paths.append(Path(path))
        if not explicit:
            assert Path(path).is_dir()
        raise RuntimeError("private self-test failure")

    monkeypatch.setattr(app_main, "self_test", fail)
    argv = ["--self-test"] + (["--workspace", str(root)] if explicit else [])
    assert app_main.main(argv) == 1
    assert "self-test failed" in capsys.readouterr().err.lower()
    assert alerts == [] and len(paths) == 1
    if explicit:
        assert paths == [root]
    else:
        assert not paths[0].exists()


def test_35_gui_failure_generic_stderr_and_alert(app_main, tmp_path, monkeypatch, capsys, alerts):
    root = tmp_path / "sensitive-workspace-name"
    secret = "private exception marker"

    def fail(ws):
        raise RuntimeError(secret)

    monkeypatch.setattr(app_main, "launch", fail)
    argv = ["--workspace", str(root)]
    assert app_main.main(argv) == 1
    error = capsys.readouterr().err
    assert "Publikreach could not start" in error
    assert str(root) not in error and secret not in error and "--workspace" not in error
    assert len(alerts) == 1 and "Publikreach could not start" in alerts[0]


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
def test_36_baseexception_propagates_and_releases_lock(app_main, workspace, monkeypatch, exception):
    def interrupt(ws):
        raise exception()

    monkeypatch.setattr(app_main, "launch", interrupt)
    with pytest.raises(exception):
        app_main.main(["--workspace", str(workspace.root)])
    assert_lock_free(workspace.root)


def test_37_self_test_pass_once_prints_passed(app_main, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(app_main, "self_test", calls.append)
    assert app_main.main(["--self-test"]) == 0
    assert len(calls) == 1
    assert "passed" in capsys.readouterr().out.lower()


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self, *args):
        for handler in list(self.handlers):
            handler(*args)


class Events:
    def __init__(self):
        self.before_load = Event()
        self.loaded = Event()
        self.closed = Event()
        self.shown = Event()


@pytest.fixture
def launch_env(app_main, workspace, monkeypatch):
    """Real Flask exchange and runtime thread with an in-process listener seam.

    The separate real-port test checks socket cleanup where binding is permitted.
    """
    state = SimpleNamespace(windows=[], starts=[], cookies=[], cookie_urls=[],
                            installed=[], servers=[], stops=[], exchanges=[],
                            cookie_count=1, http_only=True, before_start=None,
                            start_returned=False)
    original_start = runtime.start_server
    original_exchange = runtime.exchange_token

    class InProcessListener:
        server_port = 7654

        def __init__(self, *args, **kwargs):
            self.done = threading.Event()
            self.closed = False

        def serve_forever(self):
            self.done.wait(5)

        def shutdown(self):
            self.done.set()

        def server_close(self):
            self.closed = True

    state.server_factory = InProcessListener

    def start_server(*args, **kwargs):
        kwargs["server_factory"] = state.server_factory
        server = original_start(*args, **kwargs)
        stop = server.stop

        def stopped():
            state.stops.append(server)
            return stop()

        server.stop = stopped
        state.servers.append(server)
        return server

    def exchange(*args, **kwargs):
        cookie = original_exchange(*args, **kwargs)
        state.exchanges.append(cookie)
        return cookie

    monkeypatch.setattr(app_main, "start_server", start_server)
    monkeypatch.setattr(app_main, "exchange_token", exchange)

    class Cookie:
        def isHTTPOnly(self):
            return state.http_only

    class NSHTTPCookie:
        @staticmethod
        def cookiesWithResponseHeaderFields_forURL_(headers, url):
            state.cookies.append(headers)
            state.cookie_urls.append(url)
            return [Cookie() for _ in range(state.cookie_count)]

    foundation = ModuleType("Foundation")
    foundation.NSHTTPCookie = NSHTTPCookie
    foundation.NSURL = SimpleNamespace(URLWithString_=lambda url: url)
    monkeypatch.setitem(sys.modules, "Foundation", foundation)

    def call_after(callback, *args, **kwargs):
        return callback(*args, **kwargs)

    helper = ModuleType("PyObjCTools.AppHelper")
    helper.callAfter = call_after
    package = ModuleType("PyObjCTools")
    package.AppHelper = helper
    monkeypatch.setitem(sys.modules, "PyObjCTools", package)
    monkeypatch.setitem(sys.modules, "PyObjCTools.AppHelper", helper)

    class CookieStore:
        def setCookie_completionHandler_(self, cookie, callback):
            state.installed.append(cookie)
            if callback:
                callback()

    store = CookieStore()
    native_webview = SimpleNamespace(configuration=lambda: SimpleNamespace(
        websiteDataStore=lambda: SimpleNamespace(httpCookieStore=lambda: store)))

    class Window:
        def __init__(self, url):
            self.events = Events()
            self.url = url
            self.destroyed = False
            self.scripts = []
            self.run_js = self.scripts.append
            self.original_run_js = self.run_js
            self.evaluate_js = self.scripts.append
            self.native = SimpleNamespace(contentView=lambda: native_webview)

        def get_current_url(self):
            return self.url

        def load_url(self, url):
            self.url = url

        def destroy(self):
            self.destroyed = True

    webview = sys.modules["webview"]
    webview.settings = {}

    def create_window(title, url=None, **kwargs):
        window = Window(url)
        state.windows.append((title, url, kwargs, window))
        return window

    def start(func=None, *args, **kwargs):
        state.starts.append(kwargs)
        if state.before_start:
            state.before_start()
        if func:
            func(*args)
        for _, _, _, window in state.windows:
            window.events.before_load.fire()
            window.events.loaded.fire()
        state.start_returned = True

    webview.create_window = create_window
    webview.start = start
    state.webview = webview
    state.workspace = workspace
    yield state
    for server in state.servers:
        server.stop()


def assert_server_stopped(state):
    assert state.servers and state.stops
    for server in state.servers:
        assert server in state.stops
        assert not server.thread.is_alive()
        assert server.server.closed


def test_38_launch_configuration_and_native_api(app_main, launch_env):
    state = launch_env
    app_main.launch(state.workspace)
    assert state.webview.settings["ALLOW_FILE_URLS"] is False
    assert len(state.starts) == 1
    assert {key: state.starts[0][key] for key in ("debug", "private_mode", "gui")} == {
        "debug": False, "private_mode": True, "gui": "cocoa"}
    title, url, kwargs, window = state.windows[0]
    assert title == "Publikreach"
    api = kwargs["js_api"]
    assert isinstance(api, app_main.NativeApi) and api._window is window
    # Behavioral CSRF check avoids depending on the attribute's private spelling.
    window.url = state.servers[0].base_url + "/settings"
    dialogs = []
    window.create_file_dialog = lambda *args, **kwargs: dialogs.append(True) or None
    api.choose_oauth_client(state.exchanges[0].csrf)
    assert dialogs == [True]
    dialogs.clear()
    api.choose_oauth_client("wrong-csrf")
    assert dialogs == []


def test_39_native_cookie_has_protections_name_value_and_root_url(app_main, launch_env):
    state = launch_env
    app_main.launch(state.workspace)
    assert len(state.cookies) == 1
    header = state.cookies[0]["Set-Cookie"]
    cookie = state.exchanges[0]
    for text in ("HttpOnly", "SameSite=Strict", "Path=/", cookie.name + "=" + cookie.value):
        assert text in header
    assert state.cookie_urls == [state.servers[0].base_url + "/"]


@pytest.mark.parametrize("http_only,count", [(False, 1), (True, 0), (True, 2)],
                         ids=["not-httponly", "empty", "multiple"])
def test_40_invalid_native_cookie_destroys_window_and_stops_server(app_main, launch_env, http_only, count):
    state = launch_env
    state.http_only, state.cookie_count = http_only, count
    with pytest.raises(RuntimeError):
        app_main.launch(state.workspace)
    assert state.start_returned, "cookie failure must surface after webview.start returns"
    assert state.windows[0][3].destroyed is True
    assert_server_stopped(state)


def test_40_successful_launch_also_stops_server(app_main, launch_env):
    app_main.launch(launch_env.workspace)
    assert_server_stopped(launch_env)


def test_40_cookie_failure_releases_real_port(app_main, launch_env):
    import errno
    from werkzeug.serving import make_server

    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", 0))
        except OSError as exc:
            if exc.errno in (errno.EPERM, errno.EACCES):
                pytest.skip("sandbox forbids loopback bind; real-port cleanup unverified")
            raise
    state = launch_env
    state.server_factory = make_server
    state.http_only = False
    with pytest.raises(RuntimeError):
        app_main.launch(state.workspace)
    assert state.stops and state.windows[0][3].destroyed
    for server in state.servers:
        assert not server.thread.is_alive()
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", urlsplit(server.base_url).port))


def test_41_closed_event_is_server_stop(app_main, launch_env):
    state = launch_env

    def close_during_start():
        window = state.windows[0][3]
        assert state.servers[0].stop in window.events.closed.handlers
        window.events.closed.fire()
        assert_server_stopped(state)

    state.before_start = close_during_start
    app_main.launch(state.workspace)


def test_42_csp_wrapper_installed_before_start(app_main, launch_env):
    state = launch_env
    checked = []

    def check():
        window = state.windows[0][3]
        assert window.run_js is not window.original_run_js
        window.run_js("before_start();")
        assert window.scripts[-1] == "before_start();\n" + app_main.CSP_BRIDGE
        checked.append(True)

    state.before_start = check
    app_main.launch(state.workspace)
    assert checked == [True]
