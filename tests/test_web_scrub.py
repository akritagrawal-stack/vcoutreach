"""Scrub scan of the web app, the tracking worker and the web tests themselves (SPEC_V3 section 3).

Plain text reads with pathlib and os.walk. Nothing is imported from the trees under scan, and the owner strings are
built from fragments so that this file does not contain what it searches for.
No dash characters appear in this file.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest

from web_helpers import *  # noqa: F401,F403
from web_helpers import OWNER_NEEDLES, REPO_ROOT

WEB = REPO_ROOT / "vcoutreach" / "web"
WORKER = REPO_ROOT / "tracking_worker"
TESTS = REPO_ROOT / "tests"
NEW_TESTS = [TESTS / name for name in (
    "web_helpers.py", "test_web_security.py", "test_web_send_modes.py", "test_web_crm_contacts.py",
    "test_web_engine_drafts.py", "test_web_settings_keys.py", "test_web_replies_analytics.py",
    "test_web_integrations_offline.py", "test_web_sample.py", "test_web_scrub.py", "test_web_cli_startup.py")]

PLACEHOLDER_DOMAINS = {"example.com", "example.org", "example.net", "domain.tld", "host.tld", "localhost",
                       "yourdomain.com", "your-domain.com", "mail.test"}


def trees() -> List[Path]:
    return [p for p in (WEB, WORKER) if p.exists()]


def walk_files(root: Path) -> Iterator[Path]:
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if not name.endswith((".pyc", ".pyo")):
                yield Path(current) / name


def scanned_files() -> List[Path]:
    out: List[Path] = []
    for tree in trees():
        out.extend(walk_files(tree))
    out.extend(p for p in NEW_TESTS if p.exists())
    return out


def read_text_or_none(path: Path):
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8000]:
        return None
    return data.decode("utf-8", errors="replace")


@pytest.fixture(scope="module", autouse=True)
def _trees_exist():
    if not WEB.is_dir():
        pytest.fail("vcoutreach/web does not exist")


@pytest.mark.parametrize("pair", [OWNER_NEEDLES[i:i + 2] for i in range(0, len(OWNER_NEEDLES), 2)],
                         ids=[f"needles-{i}-{i + 1}" for i in range(0, len(OWNER_NEEDLES), 2)])
def test_no_owner_identity_string_appears_in_the_web_app_the_worker_or_the_web_tests(pair):
    hits: List[str] = []
    for path in scanned_files():
        text = read_text_or_none(path)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            low = line.lower()
            hits.extend(f"{path.relative_to(REPO_ROOT)}:{number}" for needle in pair if needle in low)
    assert not hits, f"{len(hits)} hit(s): " + ", ".join(hits[:15])


FORBIDDEN_NAME = re.compile(
    r"(^\.DS_Store$)|(^\.env$)|(^\.env\.(?!example$))|(\.(db|sqlite|sqlite3|db-wal|db-shm)$)|(-wal$)|(-shm$)"
    r"|(\.pdf$)|(\.pem$)|(\.p12$)|(\.key$)", re.I)
DATA_SUFFIXES = (".json", ".txt", ".dat", ".pkl", ".pickle", ".har", ".cookies", ".sqlite", ".db")
PRIVATE_DATA_NAME = re.compile(r"(cookie|session|storage_state|linkedin_profile)", re.I)


def test_no_database_env_cookie_session_or_deck_file_is_in_the_shipped_trees():
    bad: List[str] = []
    for tree in trees():
        for current, dirs, files in os.walk(tree):
            for d in dirs:
                if d in (".linkedin_profile", "node_modules", ".wrangler"):
                    bad.append(str(Path(current, d).relative_to(REPO_ROOT)))
            for name in files:
                relative = str(Path(current, name).relative_to(REPO_ROOT))
                if FORBIDDEN_NAME.search(name):
                    bad.append(relative)
                elif PRIVATE_DATA_NAME.search(name) and name.lower().endswith(DATA_SUFFIXES):
                    bad.append(relative)
    assert not bad, bad


def test_no_pycache_or_compiled_file_would_be_shipped():
    """Checked against what git would add (tracked or not ignored), because running the tests compiles modules."""
    git = shutil.which("git")
    if git is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    paths = [str(t.relative_to(REPO_ROOT)) for t in trees()]
    proc = subprocess.run([git, "-C", str(REPO_ROOT), "ls-files", "--cached", "--others", "--exclude-standard", "--", *paths],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    offenders = [p for p in proc.stdout.splitlines() if "__pycache__" in p or p.endswith((".pyc", ".pyo", ".DS_Store"))]
    assert not offenders, offenders


def test_no_module_imports_the_old_private_packages():
    pattern = re.compile(r"^\s*(?:from|import)\s+(publik_outreach|scripts|app)(?:\.|\s|$)", re.M)
    offenders = []
    for tree in trees():
        for path in walk_files(tree):
            if path.suffix != ".py":
                continue
            text = read_text_or_none(path) or ""
            for match in pattern.finditer(text):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {match.group(0).strip()}")
            if "publik_outreach" in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)}: mentions publik_outreach")
    assert not offenders, offenders[:15]


def test_no_absolute_home_directory_path_is_left_in_the_shipped_trees():
    pattern = re.compile(r"/Users/[A-Za-z0-9._-]+|/home/[a-z][a-z0-9._-]*/|[A-Za-z]:\\Users\\")
    offenders = []
    for tree in trees():
        for path in walk_files(tree):
            text = read_text_or_none(path)
            if text is None:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")
    assert not offenders, offenders[:15]


def test_no_email_address_outside_the_example_domains_is_in_the_shipped_trees():
    email = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
    offenders = []
    for tree in trees():
        for path in walk_files(tree):
            text = read_text_or_none(path)
            if text is None:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                for match in email.finditer(line):
                    domain = match.group(1).lower()
                    if domain in PLACEHOLDER_DOMAINS or domain.endswith((".example.com", ".example.org", ".example.net")):
                        continue
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {match.group(0)}")
    assert not offenders, offenders[:15]


def test_the_tracking_worker_carries_no_account_ids_database_ids_or_real_worker_hosts():
    if not WORKER.is_dir():
        pytest.fail("tracking_worker/ is missing")
    offenders = []
    for path in walk_files(WORKER):
        text = read_text_or_none(path)
        if text is None:
            continue
        if re.search(r"""account_id\s*[=:]\s*["']?[0-9a-f]{32}""", text, re.I):
            offenders.append(f"{path.name}: account id")
        if re.search(r"""database_id\s*[=:]\s*["']?[0-9a-f]{8}-[0-9a-f]{4}-""", text, re.I):
            offenders.append(f"{path.name}: database id")
        for host in re.findall(r"[A-Za-z0-9<>_-]+\.[A-Za-z0-9<>_-]+\.workers\.dev", text):
            if not re.search(r"(?i)your|example|<|placeholder|subdomain", host):
                offenders.append(f"{path.name}: {host}")
    assert not offenders, offenders


SECRET_PATTERNS = {
    "openai-style key": r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}",
    "google api key": r"AIza[0-9A-Za-z_-]{30,}",
    "google oauth access token": r"ya29\.[0-9A-Za-z_-]{20,}",
    "oauth client secret": r"GOCSPX-[0-9A-Za-z_-]{10,}",
    "private key block": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "github token": r"gh[pousr]_[A-Za-z0-9]{30,}",
}


def test_no_secret_shaped_string_is_in_the_shipped_trees():
    offenders = []
    for tree in trees():
        for path in walk_files(tree):
            text = read_text_or_none(path)
            if text is None:
                continue
            for label, pattern in SECRET_PATTERNS.items():
                if re.search(pattern, text):
                    offenders.append(f"{path.relative_to(REPO_ROOT)} ({label})")
    assert not offenders, offenders
