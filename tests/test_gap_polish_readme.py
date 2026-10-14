"""SPEC 23.6 and 23.7: stable README facts, checked with whitespace-normalized text and loose wording.

These read the repository files themselves, so they skip when the package is not running from a source checkout.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import vcoutreach

NEGATION = re.compile(r"\b(not|never|don'?t|do not|unrelated|instead of|rather than|avoid|without)\b")
PIP_INSTALL_BARE_NAME = re.compile(r"pip install\s+(?:-\S+\s+)*[\"'\\]*vcoutreach")


@pytest.fixture(scope="module")
def root() -> Path:
    path = Path(vcoutreach.__file__).resolve().parent.parent
    if not (path / "README.md").exists() or not (path / "pyproject.toml").exists():
        pytest.skip("not running from a source checkout")
    return path


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def readme_text(root: Path) -> str:
    return normalize((root / "README.md").read_text(encoding="utf-8"))


def windows(text: str, pattern: str, radius: int = 300):
    for match in re.finditer(pattern, text):
        yield text[max(0, match.start() - radius): match.end() + radius]


def bare_name_install_suggestions(text: str) -> list[str]:
    """Places where `pip install vcoutreach...` is offered as something to run (a nearby warning against it is fine)."""
    lowered = text.lower()
    found = []
    for match in PIP_INSTALL_BARE_NAME.finditer(lowered):
        before = lowered[max(0, match.start() - 160): match.start()]
        if not NEGATION.search(before):
            found.append(text[max(0, match.start() - 40): match.end() + 40])
    return found


# ----- 23.7 real contacts belong in a copy named contacts.csv ----------------------------------------------------------------------------

def test_readme_says_real_contacts_belong_in_a_copy_named_contacts_csv(root):
    lower = readme_text(root).lower()
    assert any(re.search(r"\bcop(y|ied|ies)\b", w) and re.search(r"ignor", w)
               for w in windows(lower, r"(?<![\w.])contacts\.csv", radius=300)), \
        "say that real contacts go in a copy named contacts.csv, which git ignores"


def test_readme_gives_the_reason_the_sample_file_is_not_the_place_for_real_contacts(root):
    lower = readme_text(root).lower()
    assert any(re.search(r"tracked|committed|checked in|version control|under git|in git", w) and re.search(r"(?<![\w.])contacts\.csv", w)
               for w in windows(lower, r"contacts\.sample\.csv", radius=350)), \
        "explain that contacts.sample.csv is tracked by git, so real people must not be typed into it"


# ----- 23.7 a case-only rename of an id ------------------------------------------------------------------------------------------------

def test_readme_says_a_case_only_id_rename_replaces_the_old_file_name(root):
    lower = readme_text(root).lower()
    anchors = r"case[- ]only|only the case|only in case|only by case|differ\w* only (in|by) case|changes? (the |its )?case|change of case|case change"
    assert any(re.search(r"\bids?\b", w) and re.search(r"renam|chang", w) and re.search(r"replac|old (file )?name|overwrit|old file", w)
               for w in windows(lower, anchors, radius=300)), \
        "say that renaming an id by case only replaces the old file name"


def test_readme_has_dropped_the_note_about_stale_files_on_case_sensitive_disks(root):
    lower = readme_text(root).lower()
    assert not any("stale" in w for w in windows(lower, r"case[- ]sensitive", radius=220)), \
        "a case-only rename is handled now, so the old case-sensitive disk caveat should be gone"


# ----- 23.6 the Gmail libraries are installed from the repository folder -----------------------------------------------------------------

def test_readme_says_to_run_the_gmail_requirements_install_from_the_repository_folder(root):
    lower = readme_text(root).lower()
    folder = r"repositor(y|ies)['\s]*(folder|directory|root|checkout)|repo(sitory)?['\s]*(folder|directory|root)|root of (the|your|this) (repo|repository|clone|checkout|project)|cloned (folder|directory|repo)|(project|clone|checkout)['\s]*(folder|directory|root)"
    assert any(re.search(folder, w) for w in windows(lower, r"pip install -r requirements-gmail\.txt", radius=260)), \
        "say that pip install -r requirements-gmail.txt is run from the repository folder"


def test_readme_never_tells_people_to_install_the_bare_name_from_a_package_index(root):
    assert bare_name_install_suggestions(readme_text(root)) == []


def test_no_shipped_text_file_or_module_tells_people_to_install_the_bare_name_from_a_package_index(root):
    package = Path(vcoutreach.__file__).resolve().parent
    candidates = [p for p in root.iterdir() if p.is_file() and p.suffix.lower() in {".md", ".txt", ".toml", ".example", ".cfg"}]
    candidates += [root / ".env.example"]
    candidates += [p for p in package.rglob("*.py")]
    offenders = {}
    for path in candidates:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        found = bare_name_install_suggestions(normalize(text))
        if found:
            offenders[str(path.relative_to(root))] = found
    assert offenders == {}


def test_the_gmail_requirements_file_is_still_in_the_repository_root(root):
    assert (root / "requirements-gmail.txt").is_file()


# ----- first-user README fixes ---------------------------------------------------------------------------------------------------------

def test_readme_tells_people_to_reactivate_the_virtual_environment_in_every_new_terminal(root):
    text = readme_text(root)
    lower = text.lower()
    assert any(". .venv/bin/activate" in w and re.search(r"new terminal|every terminal|each terminal|new shell", w)
               for w in windows(lower, r"command not found", radius=300)), \
        "say that every new terminal needs . .venv/bin/activate, or vcoutreach: command not found appears"


def test_readme_quickstart_copies_the_sample_contacts_to_contacts_csv(root):
    text = readme_text(root)
    clone = text.index("git clone ")
    copy = text.index("cp contacts.sample.csv contacts.csv")
    assert clone < copy, "the copy advice belongs to the quickstart, after the clone command"
    gmail = re.search(r"## Optional: sending with Gmail", text)
    assert gmail is None or copy < gmail.start(), "the copy advice comes before the Gmail section"


def test_readme_gmail_section_says_it_is_not_needed_to_use_the_tool(root):
    text = readme_text(root)
    start = re.search(r"## (?:Optional: )?[Ss]ending with Gmail", text)
    assert start, "a Gmail section heading"
    assert re.search(r"you do not need this|not needed|not required|you don't need this", text[start.start(): start.start() + 500], re.I), \
        "the Gmail section should open by saying it is not needed to use the tool"
