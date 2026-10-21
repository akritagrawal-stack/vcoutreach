"""Repository hygiene and the scrub rules of SPEC sections 3, 14 and 16.

These tests read the repository files themselves, so they skip when the package is not
running from a source checkout (for example a plain pip install).
"""
from __future__ import annotations

import random
import re
import shutil
import string
import subprocess
from pathlib import Path

import pytest

import vcoutreach

EM = "\u2014"
EN = "\u2013"
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
             "node_modules", "dist", "build", ".tox", "site-packages"}

SECRET_PATTERNS = {
    "openai-style key": r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}",
    "google api key": r"AIza[0-9A-Za-z_-]{30,}",
    "github token": r"gh[pousr]_[A-Za-z0-9]{30,}",
    "slack token": r"xox[abprs]-[A-Za-z0-9-]{10,}",
    "aws key id": r"AKIA[0-9A-Z]{16}",
    "private key block": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "google oauth access token": r"ya29\.[0-9A-Za-z_-]{20,}",
    "google oauth refresh token": r"1//[0-9A-Za-z_-]{40,}",
    "oauth client secret": r"GOCSPX-[0-9A-Za-z_-]{10,}",
}


@pytest.fixture(scope="module")
def root() -> Path:
    path = Path(vcoutreach.__file__).resolve().parent.parent
    if not (path / "pyproject.toml").exists():
        pytest.skip("not running from a source checkout")
    return path


def readme_text(root: Path) -> str:
    """The README with every run of whitespace (including line breaks from hard wrapping) collapsed to one space."""
    return re.sub(r"\s+", " ", (root / "README.md").read_text(encoding="utf-8"))


def is_git_checkout(root: Path) -> bool:
    return (root / ".git").exists() and shutil.which("git") is not None


def git(root: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def text_files(root: Path, *, include_tests: bool = True):
    for path in root.rglob("*"):
        if not path.is_file() or any(part in SKIP_DIRS or part.endswith(".egg-info") for part in path.relative_to(root).parts):
            continue
        if not include_tests and path.relative_to(root).parts[0] == "tests":
            continue
        try:
            if path.stat().st_size > 2_000_000:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in data:
            continue
        yield path, data.decode("utf-8", errors="ignore")


# ----- no em dash or en dash anywhere (SPEC section 1) ------------------------------------------------------------

def test_no_em_or_en_dash_in_any_text_file_of_the_repository(root):
    offenders = []
    for path, text in text_files(root):
        for number, line in enumerate(text.splitlines(), start=1):
            if EM in line or EN in line:
                offenders.append(f"{path.relative_to(root)}:{number}")
    assert not offenders, "unicode dashes found at: " + ", ".join(offenders[:30])


# ----- scrub rules (SPEC section 16) ----------------------------------------------------------------------------------

def test_no_secret_shaped_strings_in_the_working_tree(root):
    offenders = []
    for path, text in text_files(root, include_tests=False):
        for label, pattern in SECRET_PATTERNS.items():
            if re.search(pattern, text):
                offenders.append(f"{path.relative_to(root)} ({label})")
    assert not offenders, "possible secrets: " + ", ".join(offenders)


# A token value that is plainly a made-up test constant is not a leaked credential. Public history holds one such
# constant in a test file; a real-looking random key must still fail (see the self-tests below).
FAKE_WORDS = ("test", "fake", "example", "dummy", "placeholder", "sample", "secret", "redacted", "changeme",
              "notreal", "not-real", "mock", "demo", "your", "xxxx")
SEQUENTIAL_RUNS = ("0123456789", "1234567890", "123456789", "987654321", "abcdefghij", "abcdef0123", "abcdef012345")


def is_plainly_fake(token: str) -> bool:
    lowered = token.lower()
    if any(word in lowered for word in FAKE_WORDS):
        return True
    if any(run in lowered for run in SEQUENTIAL_RUNS):
        return True
    return bool(re.search(r"(.)\1{7,}", token))


def secret_hits(text: str) -> list[tuple[str, str]]:
    """(label, matched text) for every secret-shaped string whose value is not plainly fake."""
    hits = []
    for label, pattern in SECRET_PATTERNS.items():
        for match in re.finditer(pattern, text):
            token = match.group(0)
            if label == "private key block" or not is_plainly_fake(token):
                hits.append((label, token))
    return hits


def test_no_secret_shaped_strings_anywhere_in_git_history(root):
    if not is_git_checkout(root):
        pytest.skip("no git history to scan")
    proc = git(root, "log", "--all", "-p", "--no-color", timeout=300)
    if proc.returncode != 0:
        pytest.skip("git log failed")
    hits = sorted({label for label, _ in secret_hits(proc.stdout)})
    assert not hits, "git history contains: " + ", ".join(hits)


ALNUM = string.ascii_letters + string.digits


def realistic_key(prefix: str, length: int, seed: int, alphabet: str = ALNUM + "_-") -> str:
    """A random-looking key built at runtime, so this file never contains a secret-shaped literal."""
    rng = random.Random(seed)
    for _ in range(100):
        body = "".join(rng.choice(alphabet) for _ in range(length))
        if not is_plainly_fake(prefix + body):
            return prefix + body
    raise AssertionError("could not build a realistic key")


def realistic_samples() -> dict[str, str]:
    return {
        "openai-style key": realistic_key(_joined("s", "k-"), 48, 1),
        "google api key": realistic_key(_joined("A", "Iza"), 35, 2),
        "github token": realistic_key(_joined("gh", "p_"), 36, 3, ALNUM),
        "slack token": realistic_key(_joined("x", "oxb-"), 40, 4, ALNUM + "-"),
        "aws key id": realistic_key(_joined("AK", "IA"), 16, 5, string.ascii_uppercase + string.digits),
        "google oauth access token": realistic_key(_joined("ya", "29."), 60, 6),
        "google oauth refresh token": realistic_key(_joined("1", "//"), 60, 7),
        "oauth client secret": realistic_key(_joined("GOC", "SPX-"), 28, 8),
    }


def test_a_realistic_random_looking_key_is_never_treated_as_fake():
    for label, key in realistic_samples().items():
        assert not is_plainly_fake(key), label
        assert (label, key) in secret_hits("value = '" + key + "'\n"), label


def test_a_private_key_block_header_is_never_excused():
    header = _joined("-----BEGIN ", "PRIVATE KEY-----")
    assert secret_hits(header + "\nexample\n")


def test_plainly_fake_test_constants_are_ignored():
    fakes = [
        _joined("s", "k-test-SUPER", "SECRET-0123456789abcdef"),
        _joined("s", "k-fake-", "a" * 30),
        _joined("s", "k-", "example", "-key-for-docs-only-0000"),
        _joined("s", "k-", "dummydummydummydummydummy"),
        _joined("s", "k-", "0123456789", "0123456789"),
        _joined("A", "Iza", "x" * 35),
        _joined("gh", "p_", "1" * 36),
    ]
    for fake in fakes:
        assert is_plainly_fake(fake), fake
        assert secret_hits(fake) == [], fake


def test_the_history_scan_fails_for_a_real_shaped_key_and_passes_for_a_fake_one(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    repo = tmp_path / "scratch"
    repo.mkdir()

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
                               "-c", "commit.gpgsign=false", *args], capture_output=True, text=True, encoding="utf-8")

    assert run("init", "-q").returncode == 0
    (repo / "a.py").write_text("SECRET = '" + _joined("s", "k-test-SUPER", "SECRET-0123456789abcdef") + "'\n", encoding="utf-8")
    run("add", "-A")
    assert run("commit", "-q", "-m", "fake").returncode == 0
    assert secret_hits(run("log", "--all", "-p", "--no-color").stdout) == []
    (repo / "b.py").write_text("KEY = '" + realistic_samples()["openai-style key"] + "'\n", encoding="utf-8")
    run("add", "-A")
    assert run("commit", "-q", "-m", "real").returncode == 0
    assert [label for label, _ in secret_hits(run("log", "--all", "-p", "--no-color").stdout)] == ["openai-style key"]


def _joined(*pieces: str) -> str:
    """Build a literal from fragments so a scan pattern never spells out its subject in this public file."""
    return "".join(pieces)


PRIVATE_DATA_NAMES = {
    "third-party data vendor A": _joined("p", "itchb", "ook"),
    "third-party data vendor B": _joined("e", "nr", "ow"),
}

PACKAGE_FORBIDDEN = {
    "private home path": re.compile(r"/Users/|/home/[a-z][\w.-]*/(?:Documents|Library|Desktop|Downloads)"),
    "windows user profile path": re.compile(r"[A-Za-z]:[\\/]+Users[\\/]", re.I),
    "path under a user Library folder": re.compile(r"(?:~|\$HOME|\$\{HOME\}|/Users/[^/\s]+)/Library\b|(?:^|[\s\"'(=])/Library/", re.M),
    "per-user application data folder": re.compile(r"[A]pplication[ _-]?[S]upport|[A]pp[D]ata[\\/]", re.I),
    "OS credential store access": re.compile(_joined(r"\bkey", r"chain\b"), re.I),
    "credential store command line": re.compile(r"\bsecurity\s+(?:find|add|delete)-(?:generic|internet)-password", re.I),
    "embedded database": re.compile(r"\bsqlite3?\b", re.I),
}

PACKAGE_FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".pkl", ".pickle"}


def engine_files(root: Path):
    """Every file of the engine package: vcoutreach/ except the bundled web app in vcoutreach/web/."""
    pkg = Path(vcoutreach.__file__).resolve().parent
    for path in pkg.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.relative_to(pkg).parts[0] == "web":
            continue
        yield path


def test_package_source_has_no_private_paths_third_party_data_ties_or_credential_store_access(root):
    """The strict engine rule: it applies to the engine package only. The bundled web app has its own rules below."""
    names = {label: re.compile(r"\b" + re.escape(word) + r"\b", re.I) for label, word in PRIVATE_DATA_NAMES.items()}
    offenders = []
    for path in engine_files(root):
        if path.suffix.lower() in PACKAGE_FORBIDDEN_SUFFIXES or path.name.endswith(("-wal", "-shm")):
            offenders.append(f"{path.relative_to(root)} (private data file)")
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for label, pattern in {**PACKAGE_FORBIDDEN, **names}.items():
            if pattern.search(text):
                offenders.append(f"{path.relative_to(root)} ({label})")
    assert not offenders, ", ".join(offenders)


# ----- the bundled web app (vcoutreach/web/) and the tracking worker (tracking_worker/) ---------------------------------
# Their features (embedded database, data providers, optional OS credential store, user-supplied keys) are intended.
# What must stay out is anything private: owner paths and identifiers, real emails, raw secrets, vendored session data.

WEB_SCOPES = (("vcoutreach", "web"), ("tracking_worker",))

WEB_FORBIDDEN = {
    "private home path": PACKAGE_FORBIDDEN["private home path"],
    "windows user profile path": PACKAGE_FORBIDDEN["windows user profile path"],
    "per-user application support folder": re.compile(r"[A]pplication[ _-]?[S]upport", re.I),
}

PUBLIC_REPO_URL_PART = "akritagrawal-stack/vcoutreach"
OWNER_IDENTIFIERS = {
    "owner university domain": re.compile(_joined(r"t", r"amu\.edu"), re.I),
    "owner company identifier": re.compile(_joined(r"b", r"esquare"), re.I),
    "owner handle": re.compile(_joined(r"a", r"kritagrawal"), re.I),
    "owner facts file name": re.compile(_joined(r"a", r"krit_facts"), re.I),
}

EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
PLACEHOLDER_EMAIL_DOMAINS = {"example.com", "example.org", "example.net", "firm.com", "domain.com"}
PLACEHOLDER_EMAIL_SUFFIXES = (".example", ".test", ".invalid", ".local", ".localhost")

FLASK_SECRET_LITERALS = [
    re.compile(r"""(?i)\bsecret_key\b['"\]]*\s*(?:=|:)\s*(?:b?['"])[^'"\s]+['"]"""),
    re.compile(r"""(?i)\bFLASK_SECRET_KEY\b['"\]]*\s*(?:=|:)\s*(?:b?['"])[^'"\s]+['"]"""),
    re.compile(r"""(?i)\b(?:environ\.get|getenv|\.get)\(\s*['"]\w*SECRET_KEY['"]\s*,\s*['"][^'"]+['"]"""),
]

VENDORED_DATA_SUFFIXES = PACKAGE_FORBIDDEN_SUFFIXES | {".har", ".cookies", ".pem", ".p12", ".pfx", ".key"}
DECK_SUFFIXES = {".pdf", ".pptx", ".ppt", ".key"}


def real_looking_emails(text: str) -> list[str]:
    found = []
    for match in EMAIL_RE.finditer(text):
        address, domain = match.group(0), match.group(1).lower()
        if "..." in address or domain in PLACEHOLDER_EMAIL_DOMAINS or domain.endswith(PLACEHOLDER_EMAIL_SUFFIXES) \
                or domain.endswith(tuple("." + d for d in PLACEHOLDER_EMAIL_DOMAINS if d.startswith("example."))):
            continue
        found.append(address)
    return found


def owner_identifier_hits(text: str) -> list[str]:
    cleaned = text.replace(PUBLIC_REPO_URL_PART, "")
    return [label for label, pattern in OWNER_IDENTIFIERS.items() if pattern.search(cleaned)]


def hardcoded_flask_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in FLASK_SECRET_LITERALS)


def web_scope_paths(root: Path):
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if not path.is_file() or any(part in SKIP_DIRS or part.endswith(".egg-info") for part in rel.parts):
            continue
        if any(rel.parts[:len(scope)] == scope for scope in WEB_SCOPES):
            yield path


def test_web_app_and_tracking_worker_have_no_private_paths_owner_identifiers_emails_or_flask_secret_literals(root):
    offenders = []
    for path, text in text_files(root):
        rel = path.relative_to(root)
        if not any(rel.parts[:len(scope)] == scope for scope in WEB_SCOPES):
            continue
        for label, pattern in WEB_FORBIDDEN.items():
            if pattern.search(text):
                offenders.append(f"{rel} ({label})")
        for label in owner_identifier_hits(text):
            offenders.append(f"{rel} ({label})")
        for address in real_looking_emails(text):
            offenders.append(f"{rel} (real-looking email {address})")
        if hardcoded_flask_secret(text):
            offenders.append(f"{rel} (hardcoded Flask secret literal)")
    assert not offenders, ", ".join(offenders)


def test_web_app_and_tracking_worker_contain_no_raw_secrets(root):
    offenders = []
    for path, text in text_files(root):
        rel = path.relative_to(root)
        if not any(rel.parts[:len(scope)] == scope for scope in WEB_SCOPES):
            continue
        for label, _token in secret_hits(text):
            offenders.append(f"{rel} ({label})")
    assert not offenders, ", ".join(offenders)


def test_web_app_and_tracking_worker_ship_no_session_cookie_database_or_env_files(root):
    offenders = []
    for path in web_scope_paths(root):
        name, suffix = path.name.lower(), path.suffix.lower()
        rel = path.relative_to(root)
        if suffix in VENDORED_DATA_SUFFIXES or name.endswith(("-wal", "-shm")):
            offenders.append(f"{rel} (database, key or cookie file)")
        elif name == ".env" or (name.startswith(".env.") and name != ".env.example") or name == ".ds_store":
            offenders.append(f"{rel} (environment or finder file)")
        elif suffix in {".json", ".txt", ".cookies", ".dat"} and re.search(r"cookie|session|storage_state|storagestate", name):
            offenders.append(f"{rel} (session or cookie data)")
    assert not offenders, ", ".join(offenders)


def test_no_deck_pdf_or_presentation_file_is_shipped_anywhere_in_the_repository(root):
    # The bundled redacted example deck is intentional; it is the only deck file allowed.
    allowed = {"vcoutreach/data/examples/Publik_Pitch_Deck_Example.pdf"}
    offenders = []
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if not path.is_file() or any(part in SKIP_DIRS or part.endswith(".egg-info") for part in rel.parts):
            continue
        if str(rel) in allowed:
            continue
        if path.suffix.lower() in DECK_SUFFIXES or "deck_assets" in rel.parts:
            offenders.append(str(rel))
    assert not offenders, ", ".join(offenders)


def test_the_web_rules_catch_what_they_are_meant_to_catch_and_allow_what_they_should():
    owner_domain = _joined("t", "amu.edu")
    owner_company = _joined("Be", "Square")
    owner_handle = _joined("akrit", "agrawal")
    assert owner_identifier_hits("mail me at x@" + owner_domain)
    assert owner_identifier_hits("verify." + owner_company.lower() + ".app")
    assert owner_identifier_hits("github.com/" + owner_handle + "22")
    assert owner_identifier_hits("see https://github.com/" + owner_handle + "-stack/vcoutreach and " + owner_handle)
    assert not owner_identifier_hits("git clone https://github.com/" + owner_handle + "-stack/vcoutreach.git")
    assert real_looking_emails("write to jane.doe@" + _joined("big", "firm.vc") + " today")
    assert real_looking_emails("someone@gmail.com")
    assert not real_looking_emails("a@example.com b@mail.example.org c@host.test d@localhost.local first@firm.com")
    assert not real_looking_emails("<CABEbgnC...@mail.gmail.com>")
    assert hardcoded_flask_secret("app.secret_key = 'dev-key'")
    assert hardcoded_flask_secret('app.config["SECRET_KEY"] = "abc123"')
    assert hardcoded_flask_secret("FLASK_SECRET_KEY = 'changeme'")
    assert hardcoded_flask_secret("os.environ.get('FLASK_SECRET_KEY', 'fallback')")
    assert not hardcoded_flask_secret("app.secret_key = workspace.env()['FLASK_SECRET_KEY']")
    assert not hardcoded_flask_secret("'FLASK_SECRET_KEY': ''")
    assert not hardcoded_flask_secret("app.secret_key = secrets.token_hex(32)")
    home = _joined("/Us", "ers/someone/x")
    assert WEB_FORBIDDEN["private home path"].search(home)
    assert WEB_FORBIDDEN["per-user application support folder"].search(_joined("Application ", "Support/Publik"))


def test_package_data_files_are_only_templates_and_examples(root):
    data = Path(vcoutreach.__file__).resolve().parent / "data"
    if not data.is_dir():
        pytest.skip("no package data directory")
    unexpected = []
    for path in data.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(data).parts
        if rel[0] not in {"templates", "examples", "__init__.py"}:
            unexpected.append("/".join(rel))
    assert not unexpected, "package data must hold only templates and examples: " + ", ".join(unexpected)


def test_gitignore_keeps_private_files_out_but_lets_the_examples_in(root):
    gi = root / ".gitignore"
    assert gi.exists()
    if is_git_checkout(root):
        def ignored(rel: str) -> bool:
            return git(root, "check-ignore", "-q", "--no-index", rel).returncode == 0

        for private in (".env", "drafts/ada.md", "drafts/blocked/ada.md", "drafts/index.jsonl",
                        ".vcoutreach/journal.jsonl", "token.json", "client_secret.json",
                        "contacts.csv", "contacts-real.csv", "contacts_investors.csv"):
            assert ignored(private), f"{private} must be git-ignored"
        for shipped in (".env.example", "contacts.sample.csv", "sender.sample.json",
                        "vcoutreach/data/examples/contacts.sample.csv",
                        "vcoutreach/data/examples/sender.sample.json",
                        "vcoutreach/data/examples/env.example"):
            assert not ignored(shipped), f"{shipped} must NOT be git-ignored"
    else:
        text = gi.read_text(encoding="utf-8")
        for needle in (".env", "drafts/", ".vcoutreach/", "token", "client_secret", "contacts"):
            assert needle in text


def gitignore_rules(root: Path) -> list[tuple[int, str]]:
    """(line number, rule) for every non-blank, non-comment .gitignore line."""
    rules = []
    for number, line in enumerate((root / ".gitignore").read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            rules.append((number, stripped))
    return rules


def test_gitignore_negation_for_the_sample_contacts_comes_after_the_contacts_ignore_rule(root):
    """SPEC 18.9: git applies the last matching rule, so an un-ignore placed first would do nothing."""
    rules = gitignore_rules(root)
    ignore_lines = [n for n, rule in rules if not rule.startswith("!") and "contacts" in rule and "*" in rule and rule.endswith(".csv")]
    negation_lines = [n for n, rule in rules if rule.startswith("!") and "contacts.sample.csv" in rule]
    assert ignore_lines, "the contacts*.csv ignore rule must still be there"
    assert negation_lines, "contacts.sample.csv must be explicitly un-ignored"
    assert min(negation_lines) > max(ignore_lines), "the negation must come after the ignore rule it overrides"


def test_gitignore_sample_contacts_are_un_ignored_at_the_root_and_in_the_package_data(root):
    if not is_git_checkout(root):
        pytest.skip("not a git checkout")
    for shipped in ("contacts.sample.csv", "vcoutreach/data/examples/contacts.sample.csv"):
        # exit 1 means "not ignored"; a matching "!" negation rule also counts as not ignored
        result = git(root, "check-ignore", "-q", "--no-index", shipped)
        assert result.returncode == 1, f"{shipped} must not be ignored (exit {result.returncode})"
    for private in ("contacts.csv", "contacts-real.csv", "contacts_2026.csv", "contacts.sample.backup.csv"):
        assert git(root, "check-ignore", "-q", "--no-index", private).returncode == 0, f"{private} must stay ignored"


LEGACY_DATA_RULES = ["data/private/", "data/backups/", "data/imports/", "data/logs/", "data/*.db", "data/*.db-*"]


def test_gitignore_has_no_leftover_legacy_data_rules(root):
    """SPEC 19.10: rules that guarded a private data folder of the original tool do not belong in the public repo."""
    rules = [rule for _, rule in gitignore_rules(root)]
    leftovers = [legacy for legacy in LEGACY_DATA_RULES if legacy in rules or "/" + legacy in rules]
    assert not leftovers, leftovers


def test_gitignore_still_covers_the_real_private_files_after_the_cleanup(root):
    rules = [rule for _, rule in gitignore_rules(root)]
    joined = "\n".join(rules)
    for needle in (".env", "drafts/", ".vcoutreach/", "token", "client_secret", "contacts"):
        assert needle in joined, f"{needle} must still be ignored"


def test_no_private_artifact_is_tracked_by_git(root):
    if not is_git_checkout(root):
        pytest.skip("not a git checkout")
    tracked = git(root, "ls-files").stdout.splitlines()
    bad = []
    for name in tracked:
        base = Path(name).name
        if base == ".env" or re.search(r"\.(db|sqlite3?)$", base) or re.match(r"(token|client_secret).*\.json$", base):
            bad.append(name)
        elif name.startswith(("drafts/", ".vcoutreach/")):
            bad.append(name)
        elif re.match(r"contacts.*\.csv$", base) and base != "contacts.sample.csv":
            bad.append(name)
    assert not bad, bad


# ----- packaging and docs (SPEC sections 3 and 14) -----------------------------------------------------------------------

def test_license_is_mit(root):
    text = (root / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in text and "Permission is hereby granted" in text


def test_pyproject_declares_python_310_the_package_the_entry_point_and_no_runtime_dependencies(root):
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r"requires-python\s*=\s*\"\s*>=\s*3\.10", text)
    assert re.search(r"name\s*=\s*\"vcoutreach\"", text)
    assert "vcoutreach.cli:main" in text
    try:
        import tomllib
    except ImportError:
        return
    data = tomllib.loads(text)
    assert data["project"].get("dependencies", []) == [], "the core must have zero third-party runtime dependencies"
    assert data["project"]["scripts"]["vcoutreach"] == "vcoutreach.cli:main"


def test_requirements_files(root):
    gmail = (root / "requirements-gmail.txt").read_text(encoding="utf-8")
    assert "google-auth-oauthlib" in gmail and "google-api-python-client" in gmail
    core = (root / "requirements.txt").read_text(encoding="utf-8")
    active = [ln.strip() for ln in core.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    for line in active:
        assert not re.match(r"(google|requests|httpx|openai|anthropic|python-dotenv|pyyaml)", line, re.I), line


def test_root_env_example_is_a_copy_of_the_packaged_one(root):
    packaged = (Path(vcoutreach.__file__).resolve().parent / "data" / "examples" / "env.example").read_bytes()
    assert (root / ".env.example").read_bytes() == packaged


def test_readme_covers_the_quickstart_and_states_the_cost_plainly(root):
    text = readme_text(root)
    lower = text.lower()
    for needle in ("python3 -m venv", "pip install -e .", "vcoutreach init", "vcoutreach draft",
                   "--offline", "contacts.sample.csv", "sender.sample.json", "PUBLIK_API_KEY",
                   "https://publikhq.com/dashboard/api"):
        assert needle in text, f"README is missing {needle!r}"
    assert "money" in lower, "the README must say plainly that Publik calls cost real money"
    assert "free" in lower and "offline" in lower


def test_readme_documents_the_send_safeguards(root):
    text = readme_text(root)
    for needle in ("SEND_MODE", "TEST_RECIPIENT_EMAIL", "CONFIRM_LIVE_SEND", "--execute"):
        assert needle in text, f"README is missing {needle!r}"


def windows(text: str, pattern: str, radius: int = 350):
    for match in re.finditer(pattern, text):
        yield text[max(0, match.start() - radius): match.end() + radius]


def test_readme_admits_that_grounding_and_false_parallel_checks_are_heuristics(root):
    """SPEC 18.7: no overselling of the safety checks."""
    lower = readme_text(root).lower()
    assert "heuristic" in lower
    assert any(("grounding" in w and ("false-parallel" in w or "false parallel" in w))
               for w in windows(lower, "heuristic")), "name the checks that are heuristics"
    assert re.search(r"(read|review|check)[^.\n]{0,40}every (draft|email|message)", lower), \
        "tell the user a human must read every draft before sending"


def test_readme_warns_that_a_network_retry_can_repeat_a_metered_request(root):
    lower = readme_text(root).lower()
    assert any(re.search(r"metered|repeat|twice|duplicate|charged again|more than once", w)
               for w in windows(lower, r"retr(?:y|ies)")), \
        "document that retries can repeat a paid request after an ambiguous failure"


def test_readme_explains_that_limit_counts_planned_sends_after_skips(root):
    lower = readme_text(root).lower()
    assert any(("skip" in w and "plan" in w) for w in windows(lower, r"--limit|`limit`")), \
        "explain that --limit counts planned sends after skips"


def has_number(text: str, number: int) -> bool:
    return re.search(rf"(?<![0-9]){number}(?![0-9])", text) is not None


def test_readme_first_step_checks_the_python_version_and_upgrades_pip(root):
    text = readme_text(root)
    assert "python3 --version" in text
    assert "python3 -m pip install -U pip" in text
    assert text.index("python3 --version") < text.index("python3 -m venv"), "the version check comes first"
    window = text[max(0, text.index("python3 --version") - 700): text.index("python3 --version") + 900]
    assert "3.10" in window, "say which Python version is needed"
    assert "3.9" in window and "macos" in window.lower(), "warn that the macOS system Python is too old"


def test_readme_clones_into_an_explicit_folder(root):
    text = readme_text(root)
    assert re.search(r"git clone \S+ vcoutreach\b", text), "git clone <url> vcoutreach"
    assert "cd vcoutreach" in text
    assert text.index("git clone") < text.index("python3 -m venv")


def test_readme_documents_the_first_name_column_and_how_facts_should_read(root):
    text = readme_text(root)
    lower = text.lower()
    assert any("column" in w for w in windows(lower, r"first_name")), "document the optional first_name column"
    assert "noun phrase" in lower
    assert re.search(r"your (?:\d{4} )?essay on", lower), "show a fact that reads as a noun phrase"
    assert re.search(r"i came across .{1,40}, and i found it interesting", lower), "show the offline template sentence"
    assert any("first fact" in w for w in windows(lower, r"i came across")) or "first fact" in lower


def test_readme_documents_the_space_pipe_space_separator_for_facts(root):
    lower = readme_text(root).lower()
    assert any(" | " in w and re.search(r"separat|pipe|delimit", w) for w in windows(lower, r"facts", radius=300))


def test_readme_documents_follow_up_rows(root):
    text = readme_text(root)
    lower = text.lower()
    assert "thread_subject" in text and "in_reply_to" in text
    assert re.search(r"<[^<>\s]*@[^<>\s]*>", text), "show what a Message-ID in angle brackets looks like"
    assert "following up in case you missed my previous email" in lower or "opener" in lower


def test_readme_says_lint_takes_a_sender_file(root):
    lower = readme_text(root).lower()
    assert any("--sender" in w for w in windows(lower, r"lint --drafts|vcoutreach lint", radius=250))


def test_readme_explains_how_to_come_back_to_the_virtual_environment(root):
    text = readme_text(root)
    lower = text.lower()
    assert any("activate" in w for w in windows(lower, r"new terminal|new shell|next time|come back|re-activate|reactivate"))
    assert "Activate.ps1" in text and "powershell" in lower, "the Windows PowerShell activation line"


def test_readme_has_an_exit_code_table(root):
    lower = readme_text(root).lower()
    good = [w for w in windows(lower, r"exit code", radius=900)
            if all(has_number(w, n) for n in (0, 1, 2, 3)) and "provider" in w and "lint" in w]
    assert good, "document exit codes 0, 1, 2 and 3 (ok, lint failures, usage or file errors, provider error)"


def test_readme_says_the_test_recipient_is_only_needed_to_execute(root):
    lower = readme_text(root).lower()
    assert any("--execute" in w and re.search(r"\bonly\b|plan", w) for w in windows(lower, r"test_recipient_email", radius=300))


def test_readme_quickstart_ends_by_showing_the_first_sample_draft(root):
    from vcoutreach.contacts import load_contacts

    text = readme_text(root)
    sample = Path(vcoutreach.__file__).resolve().parent / "data" / "examples" / "contacts.sample.csv"
    first_id = load_contacts(sample)[0].id
    command = f"cat drafts/{first_id}.md"
    assert command in text, f"the README should end the quickstart with {command}"
    assert text.index(command) > text.index("--offline"), "the cat comes after the draft command"
    lower = text.lower()
    assert "index.jsonl" in lower and re.search(r"machine[- ]readable", lower)


# ----- README, round 2 of the first-user review (SPEC 20.11) ---------------------------------------------------------

CLONE_URL = "https://github.com/akritagrawal-stack/vcoutreach.git"


def test_readme_quickstart_clones_the_real_repository_url_and_has_no_placeholder(root):
    text = readme_text(root)
    assert f"git clone {CLONE_URL} vcoutreach" in text
    assert "<repo-url>" not in text


def test_readme_explains_that_init_skips_files_that_already_exist(root):
    lower = readme_text(root).lower()
    assert any("init" in w and "exist" in w and ".env.example" in w for w in windows(lower, r"skipped", radius=450)), \
        "say that init prints skipped for files that already exist, such as the .env.example a clone ships"


def test_readme_says_a_follow_up_without_a_message_id_is_rejected_at_draft_time(root):
    lower = readme_text(root).lower()
    assert any(re.search(r"reject|error|refus|fail|stop", w) and "draft" in w for w in windows(lower, r"in_reply_to", radius=300))


def test_readme_says_missing_thread_only_protects_hand_edited_drafts(root):
    lower = readme_text(root).lower()
    assert "missing_thread" in lower
    by_hand = r"hand[- ]?(edit|written|made)|edited (by hand|manually)|edit(ed)? them (by hand|manually)|manually[- ]edited"
    assert any(re.search(by_hand, w) and re.search(r"\bonly\b", w) for w in windows(lower, r"missing_thread", radius=300))


def test_readme_documents_the_id_rules(root):
    lower = readme_text(root).lower()
    assert any(re.search(r"unique|duplicate", w) and re.search(r"slug|letters|file ?name|characters|safe", w)
               for w in windows(lower, r"\bid\b", radius=450)), "say how ids are chosen, what they may contain and that they are unique"


def test_readme_tells_a_zero_byte_csv_apart_from_a_header_only_csv(root):
    text = readme_text(root)
    lower = text.lower()
    zero = r"zero[- ]byte|empty file|completely empty"
    header_only = r"header[- ]only|only a header|just a header|header but no (data )?rows|header with no (data )?rows"
    assert any(re.search(zero, w) and re.search(header_only, w) for w in windows(lower, r"header", radius=400))
    # the two exact messages a user will see, rather than the surrounding prose
    assert "is empty: expected a header row" in text
    assert "no contacts found in" in text


def test_readme_explains_that_stale_drafts_are_cleaned_up_from_the_same_out_directory(root):
    lower = readme_text(root).lower()
    assert any("index.jsonl" in w and "--out" in w and re.search(r"delet|remov", w) for w in windows(lower, r"stale", radius=450))


def test_readme_says_execute_checks_the_safeguards_before_the_google_libraries(root):
    lower = readme_text(root).lower()
    assert any(re.search(r"safeguard|gate", w) and re.search(r"before|first", w) for w in windows(lower, r"google librar|gmail librar|requirements-gmail", radius=350))


def test_readme_says_errors_with_several_problems_print_one_problem_per_line(root):
    lower = readme_text(root).lower()
    assert any(re.search(r"one problem per line|each problem on (its own|a) line|one line per problem|per line", w)
               for w in windows(lower, r"problem", radius=250))


def test_readme_no_longer_sends_people_to_lint_to_explain_draft_warnings(root):
    lower = readme_text(root).lower()
    assert not re.search(r"run vcoutreach lint --drafts \S+ for details", lower), "that was the old warnings note"
    assert "after 1 retries" not in lower


def test_community_files_exist(root):
    for name in ("CONTRIBUTING.md", "SECURITY.md", "THIRD_PARTY_NOTICES.md", "LICENSE", ".gitignore"):
        assert (root / name).is_file(), name


# ----- SPEC 21.8: README and .env.example facts from the third review -----------------------------------------------------------

def test_readme_says_the_openai_and_anthropic_models_have_no_default_and_shows_the_error(root):
    text = readme_text(root)
    lower = text.lower()
    for variable in ("OPENAI_MODEL", "ANTHROPIC_MODEL"):
        assert any(re.search(r"no default|no built-in default|required|must be set|not set|\bnone\b|unset|you choose|your own model", w)
                   for w in windows(lower, variable.lower(), radius=250)), f"{variable} must be described as having no default"
    assert "OPENAI_MODEL is not set" in text, "show the error a user sees when the model is missing"
    assert "gpt-4o-mini" not in lower, "the stale default model is gone"
    assert not re.search(r"defaults? (to|is|are) [`'\"(]*(gpt-|claude-)", lower)
    assert not re.search(r"(gpt-|claude-)[a-z0-9.\-]*[`'\")]* (is|as) the default", lower)


def test_root_env_example_marks_the_model_lines_as_required_with_a_key(root):
    text = (root / ".env.example").read_text(encoding="utf-8")
    for variable in ("OPENAI_MODEL", "ANTHROPIC_MODEL"):
        assert variable in text, f"{variable} must be in .env.example"
        flat = re.sub(r"\s+", " ", text).lower()
        assert any(re.search(r"required|must|need|mandatory", w) for w in windows(flat, variable.lower(), radius=300)), \
            f"say that {variable} is required when its key is set"


def test_readme_explains_what_the_wrote_line_counts(root):
    text = readme_text(root)
    lower = text.lower()
    assert any("blocked" in w and "index.jsonl" in w for w in windows(lower, r"\bwrote\b", radius=300)), \
        "explain the wrote line: drafts, blocked drafts and the index"
    assert not re.search(r"\(\s*(\d+|<n>|n)\s+files\s*\)", lower), "the old '(<n> files)' wording is gone"


def test_readme_says_ids_are_unique_ignoring_case(root):
    lower = readme_text(root).lower()
    pattern = r"ignor\w* case|differ only (in|by) case|case[- ]insensitiv|regardless of (their )?case|upper[- ]?case and lower[- ]?case"
    assert any(re.search(pattern, w) and re.search(r"unique|duplicate", w) for w in windows(lower, r"\bids?\b", radius=350)), \
        "say that ids must be unique even when they differ only by case"


def test_readme_says_limit_zero_plans_nothing(root):
    lower = readme_text(root).lower()
    assert any(re.search(r"(?<![0-9])0(?![0-9])|zero", w) and re.search(r"nothing|no (drafts|messages|sends)|none", w)
               for w in windows(lower, r"--limit", radius=300)), "document that --limit 0 plans nothing"


def test_readme_says_the_vcoutreach_folder_is_git_ignored_and_what_gitignore_covers(root):
    lower = readme_text(root).lower()
    assert any(re.search(r"git-?ignore", w) for w in windows(lower, r"\.vcoutreach", radius=300)), \
        "say that .vcoutreach/ (journal and lock file) is git-ignored"
    assert any("contacts" in w and "csv" in w for w in windows(lower, r"\.gitignore|git-ignored|gitignored", radius=300)), \
        "say which contact files .gitignore covers"


def test_readme_mentions_suffixes_after_a_comma_next_to_the_first_name_column(root):
    lower = readme_text(root).lower()
    assert any(re.search(r"phd|jr", w) for w in windows(lower, r"first_name", radius=450)), \
        "say that a suffix such as PhD or Jr after a comma is handled, and use first_name for unusual names"


@pytest.mark.parametrize("private", [".vcoutreach/journal.jsonl", ".vcoutreach/journal.lock", ".vcoutreach/send.lock",
                                     ".vcoutreach/journal.jsonl.lock"])
def test_the_journal_and_its_lock_file_are_git_ignored(root, private):
    if not is_git_checkout(root):
        pytest.skip("not a git checkout")
    assert git(root, "check-ignore", "-q", "--no-index", private).returncode == 0, f"{private} must be git-ignored"
