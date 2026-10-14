"""Repository hygiene and the scrub rules of SPEC sections 3, 14 and 16.

These tests read the repository files themselves, so they skip when the package is not
running from a source checkout (for example a plain pip install).
"""
from __future__ import annotations

import re
import shutil
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


def test_no_secret_shaped_strings_anywhere_in_git_history(root):
    if not is_git_checkout(root):
        pytest.skip("no git history to scan")
    proc = git(root, "log", "--all", "-p", "--no-color", timeout=300)
    if proc.returncode != 0:
        pytest.skip("git log failed")
    hits = [label for label, pattern in SECRET_PATTERNS.items() if re.search(pattern, proc.stdout)]
    assert not hits, "git history contains: " + ", ".join(hits)


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


def test_package_source_has_no_private_paths_third_party_data_ties_or_credential_store_access(root):
    pkg = Path(vcoutreach.__file__).resolve().parent
    names = {label: re.compile(r"\b" + re.escape(word) + r"\b", re.I) for label, word in PRIVATE_DATA_NAMES.items()}
    offenders = []
    for path in pkg.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.suffix.lower() in PACKAGE_FORBIDDEN_SUFFIXES or path.name.endswith(("-wal", "-shm")):
            offenders.append(f"{path.relative_to(root)} (private data file)")
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for label, pattern in {**PACKAGE_FORBIDDEN, **names}.items():
            if pattern.search(text):
                offenders.append(f"{path.relative_to(root)} ({label})")
    assert not offenders, ", ".join(offenders)


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
