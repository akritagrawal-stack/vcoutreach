"""publik API as one provider option. Resolution order: the user's own key >
PUBLIK_API_KEY / PUBLIK_API_BASE_URL env > this app's credential file >
provision on first launch (after the user accepts DISCLOSURE) > the user's
own key again if provisioning fails."""
import json
import os
import platform
import socket
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from vcoutreach import __version__

APP_SLUG = "publikreach"
APP_VERSION = __version__
DEFAULT_BASE_URL = "https://publikhq.com/api/v1"
# Ask for a tier, never a vendor model name.
MODELS = {"fast": "publik-fast", "balanced": "publik-balanced", "smart": "publik-smart"}
# Show this and get a yes BEFORE provision().
DISCLOSURE = (
    "This app can use publik API for its AI features: no key to paste, priced per use in dollars from a publik balance that starts at $0.00. "
    "Linking this computer to a publik account gives $0.05 of free use, once. Your prompts go through publik's servers to the model; you can switch to your own key at any time."
)


def _token_dirs():
    """Folders searched for publik-app-token.txt: the PyInstaller bundle root (sys._MEIPASS),
    this module's folder and every parent, then the executable's folder and parents (a frozen
    .app has no real source tree, so __file__ parents alone are not enough)."""
    seen = []
    roots = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    here = Path(__file__).resolve()
    roots.extend([here.parent, *here.parents])
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        roots.extend([exe.parent, *exe.parents])
    for d in roots:
        if d not in seen:
            seen.append(d)
    return seen


def _app_token() -> str:
    """Public by design (it can only mint capped keys attributed to this app), so
    publik-app-token.txt is committed and read from the bundle root, this module's folder or the
    nearest parent. PUBLIK_APP_TOKEN in the environment overrides it. Empty string if absent."""
    env = os.environ.get("PUBLIK_APP_TOKEN", "").strip()
    if env:
        return env
    for d in _token_dirs():
        f = d / "publik-app-token.txt"
        try:
            if f.is_file():
                return f.read_text().strip()
        except OSError:
            continue
    return ""


def credential_path() -> Path:
    """~/Library/Application Support/publik/apps/<slug>.json, %LOCALAPPDATA%\\publik\\apps\\<slug>.json, ~/.config/publik/apps/<slug>.json"""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "publik" / "apps" / f"{APP_SLUG}.json"


def read_credential():
    """The saved credential dict, or None. PUBLIKREACH_NO_CREDENTIAL_FILE=1 skips the lookup (tests)."""
    if os.environ.get("PUBLIKREACH_NO_CREDENTIAL_FILE") == "1":
        return None
    try:
        c = json.loads(credential_path().read_text())
        return c if str(c.get("key", "")).startswith("pk_") else None
    except (OSError, ValueError):
        return None


def _write_credential(c: dict) -> None:
    p = credential_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(c, f, indent=2)
    os.chmod(p, 0o600)


def resolve_provider(user_key=None):
    """Returns ("own", base_url, key), ("publik", base_url, key) or ("needs_consent", None, None)."""
    if user_key and user_key.get("api_key"):
        return ("own", user_key["base_url"], user_key["api_key"])
    if os.environ.get("PUBLIK_API_KEY"):
        return ("publik", os.environ.get("PUBLIK_API_BASE_URL", DEFAULT_BASE_URL), os.environ["PUBLIK_API_KEY"])
    c = read_credential()
    if c:
        return ("publik", c.get("base_url") or DEFAULT_BASE_URL, c["key"])
    return ("needs_consent", None, None)


def _post(url: str, body: dict):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def provision() -> dict:
    """Call only after the user accepted DISCLOSURE. Raises RuntimeError with a readable message."""
    token = _app_token()
    if not token.startswith("pat_"):
        raise RuntimeError("This build has no publik API app token. Use your own key for now.")
    prior = read_credential() or {}
    install_id = prior.get("install_id") or str(uuid.uuid4())
    os_name = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
    for _ in range(2):
        status, body = _post(f"{DEFAULT_BASE_URL}/installs", {
            "app_token": token, "app_slug": APP_SLUG, "app_version": APP_VERSION, "os": os_name,
            "arch": platform.machine()[:16], "device_name": socket.gethostname()[:120],
            "install_id": install_id, "disclosure_version": 1, "dialects": ["chat_completions"],
        })
        key = body.get("key") or (prior.get("key") if status == 200 else None)
        if status in (200, 201) and key:
            c = {
                "key": key, "base_url": body.get("base_url", DEFAULT_BASE_URL),
                "install_id": body.get("install_id", install_id), "claim_url": body.get("claim_url"),
                "claim_state": body.get("claim_state", "anonymous"),
                "add_credit_url": (body.get("wallet") or {}).get("add_credit_url"),
                "cost_sentence": (body.get("disclosure") or {}).get("cost"),
                "balance_micros": body.get("balance_micros"),
            }
            _write_credential(c)  # the key reaches disk before anything else sees it
            return c
        if status == 200 or (body.get("error") or {}).get("type") == "install_revoked":
            install_id, prior = str(uuid.uuid4()), {}
            continue
        raise RuntimeError((body.get("error") or {}).get("message") or f"publik API could not set up this computer ({status}). Use your own key for now.")
    raise RuntimeError("publik API could not set up this computer. Use your own key for now.")


def self_test() -> str:
    """Builder check, run by the BUILT app: start it with PUBLIK_SELF_TEST=1, print this line, exit.
    It provisions for real; the builder who runs it is the consent."""
    try:
        return f"publik self-test: ok {APP_SLUG} {APP_VERSION} (install {provision()['install_id'][:8]})"
    except Exception as e:  # a readable RuntimeError, or the network
        return f"publik self-test: failed: {e}"


def client(provider):
    """OpenAI-compatible client: pip install openai."""
    from openai import OpenAI
    return OpenAI(base_url=provider[1], api_key=provider[2])


def balance_line(micros, claim_state="anonymous") -> str:
    """First-run card, line 1. Then keep it live from the x-publik-balance header. A new install is $0.00 until it is linked."""
    if micros is None:
        return ""
    dollars = f"${micros / 1_000_000:.2f}"
    return f"{dollars} · link this computer for $0.05 of free use" if micros == 0 and claim_state == "anonymous" else f"{dollars} of publik balance"


def top_up_url_from_402(body) -> tuple:
    """A 402 renders its message plus exactly ONE link: this one."""
    e = (body or {}).get("error") or {}
    return e.get("message", "Not enough publik API balance."), e.get("top_up_url")

def search(provider, q: str, count: int = 10):  # POST /search: web results for q (quote a phrase to find it exactly), one published price per search; (results, message, top_up_url)
    req = urllib.request.Request(f"{provider[1]}/search", data=json.dumps({"q": q, "count": count}).encode(), headers={"content-type": "application/json", "authorization": f"Bearer {provider[2]}"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}").get("results", []), None, None
    except urllib.error.HTTPError as e:
        return ([], *top_up_url_from_402(json.loads(e.read() or b"{}"))) if e.code == 402 else ([], f"publik API answered {e.code}", None)
