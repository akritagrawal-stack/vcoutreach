"""install-mac.sh and packaging/mac/*: static checks plus real dry-run executions.

Written from the installer spec only. The dry-run tests run the real bash script with a throwaway HOME,
a PATH that starts with fake curl/git/sudo/pip programs that record any call, and a cwd that must stay empty.
"""
from __future__ import annotations

import os
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

import pytest

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "install-mac.sh"
UNINSTALL = REPO / "packaging" / "mac" / "uninstall-mac.sh"
MAC_README = REPO / "packaging" / "mac" / "README.md"
TARBALL = "https://github.com/akritagrawal-stack/vcoutreach/archive/refs/heads/{ref}.tar.gz"
DASHES = (chr(0x2014), chr(0x2013))
FAKE_TOOLS = ["curl", "wget", "git", "sudo", "brew", "pip", "pip3", "open", "osascript", "hdiutil", "ssh", "scp", "nc"]

on_mac = pytest.mark.skipif(sys.platform != "darwin", reason="the installer only runs on macOS")


@pytest.fixture
def world(tmp_path: Path):
    """A throwaway HOME, cwd and fake tool directory. Fake tools record a marker file and fail."""
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    fakebin = tmp_path / "fakebin"
    marks = tmp_path / "marks"
    for d in (home, cwd, fakebin, marks):
        d.mkdir(exist_ok=True)
    for name in FAKE_TOOLS:
        tool = fakebin / name
        tool.write_text('#!/bin/sh\necho "$0 $*" >> "%s/%s"\nexit 99\n' % (marks, name))
        tool.chmod(0o755)
    path = os.pathsep.join([str(fakebin), str(Path(sys.executable).parent), "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    env = {"PATH": path, "HOME": str(home), "LC_ALL": "C", "TERM": "dumb"}
    return {"home": home, "cwd": cwd, "fakebin": fakebin, "marks": marks, "env": env, "tmp": tmp_path}


def run(script: Path, args: List[str], w, *, extra_env: Optional[Dict[str, str]] = None, timeout: int = 60):
    env = dict(w["env"])
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(script)] + args, capture_output=True, text=True, env=env, cwd=str(w["cwd"]),
        timeout=timeout, stdin=subprocess.DEVNULL, start_new_session=True,
    )


def tree(root: Path) -> Dict[str, bytes]:
    out: Dict[str, bytes] = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        out[rel] = p.read_bytes() if p.is_file() and not p.is_symlink() else b"<node>"
    return out


def fake_calls(w) -> List[str]:
    return sorted(p.name for p in w["marks"].iterdir())


def shown(path: Path, out: str) -> bool:
    """True when out mentions the whole path, either as plain text or shell-escaped/quoted."""
    text = str(path)
    forms = {text, shlex.quote(text), text.replace(" ", "\\ "), '"%s"' % text, "'%s'" % text}
    return any(f in out for f in forms)


def nonblank(text: str) -> List[str]:
    return [ln for ln in text.splitlines() if ln.strip()]


# ===== static checks on the shipped files ===============================================================================

def test_files_exist():
    assert INSTALL.is_file()
    assert UNINSTALL.is_file()
    assert MAC_README.is_file()


@pytest.mark.parametrize("script", [INSTALL, UNINSTALL], ids=["install", "uninstall"])
def test_scripts_are_valid_bash(script):
    res = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


@pytest.mark.parametrize("script", [INSTALL, UNINSTALL], ids=["install", "uninstall"])
def test_scripts_are_bash_and_executable(script):
    first = script.read_text(encoding="utf-8").splitlines()[0]
    assert re.match(r"#!\s*(/bin/bash|/usr/bin/env bash)\s*$", first), first
    assert script.stat().st_mode & stat.S_IXUSR, "ship the script with the executable bit"


@pytest.mark.parametrize("path", [INSTALL, UNINSTALL, MAC_README], ids=["install", "uninstall", "readme"])
def test_no_em_or_en_dash_anywhere(path):
    text = path.read_text(encoding="utf-8")
    for dash in DASHES:
        assert dash not in text


@pytest.mark.parametrize("script", [INSTALL, UNINSTALL], ids=["install", "uninstall"])
def test_scripts_never_use_sudo(script):
    assert not re.search(r"\bsudo\b", script.read_text(encoding="utf-8"), re.I)


@pytest.mark.parametrize("script", [INSTALL, UNINSTALL], ids=["install", "uninstall"])
def test_scripts_have_unix_line_endings_and_no_http_downloads(script):
    raw = script.read_bytes()
    assert b"\r" not in raw
    assert b"http://" not in raw


def test_installer_does_not_need_git():
    text = INSTALL.read_text(encoding="utf-8")
    assert not re.search(r"(^|[;&|(\s])git\s+(clone|pull|fetch|checkout|init|remote)\b", text, re.M)
    assert not re.search(r"command -v git|which git", text)
    assert "archive/refs/heads/" in text, "download the GitHub tarball instead"


def test_installer_does_not_pipe_downloads_into_a_shell_or_eval():
    text = INSTALL.read_text(encoding="utf-8")
    code = "\n".join(
        ln for ln in text.splitlines() if not re.match(r"\s*(#|say\b|echo\b|printf\b)", ln)
    )
    assert not re.search(r"curl[^\n]*\|\s*(ba)?sh", code)
    assert not re.search(r"\beval\b", code)


def test_mac_readme_is_short():
    lines = MAC_README.read_text(encoding="utf-8").splitlines()
    assert 1 <= len(lines) <= 25, len(lines)
    assert any(ln.strip() for ln in lines)


def test_uninstaller_documents_dry_run_and_paths():
    text = UNINSTALL.read_text(encoding="utf-8")
    for flag in ("--dry-run", "--dir", "--workspace"):
        assert flag in text


# ===== installer: help and bad input ====================================================================================

@on_mac
def test_help_exits_zero_and_lists_every_flag(world):
    res = run(INSTALL, ["--help"], world)
    assert res.returncode == 0
    for flag in ("--dry-run", "--dir", "--workspace", "--ref", "--no-launcher-on-desktop", "--help"):
        assert flag in res.stdout + res.stderr, flag
    assert fake_calls(world) == []
    assert tree(world["home"]) == {} and tree(world["cwd"]) == {}


@on_mac
@pytest.mark.parametrize("flag", ["--bogus", "-z", "--no-such-flag", "dry-run", "--DRY-RUN", "--dryrun"])
def test_unknown_flags_are_a_one_line_error(world, flag):
    res = run(INSTALL, [flag], world)
    assert res.returncode != 0
    lines = nonblank(res.stdout + res.stderr)
    assert len(lines) == 1, lines
    assert flag in lines[0]
    assert fake_calls(world) == []
    assert tree(world["home"]) == {} and tree(world["cwd"]) == {}


@on_mac
def test_an_unknown_flag_after_dry_run_still_fails_and_changes_nothing(world):
    res = run(INSTALL, ["--dry-run", "--bogus"], world)
    assert res.returncode != 0
    assert len(nonblank(res.stdout + res.stderr)) == 1
    assert tree(world["home"]) == {}


@on_mac
@pytest.mark.parametrize("flag", ["--dir", "--workspace", "--ref"])
def test_flag_missing_its_value_is_a_clean_error(world, flag):
    res = run(INSTALL, ["--dry-run", flag], world)
    assert res.returncode != 0
    out = res.stdout + res.stderr
    assert "unbound variable" not in out and "command not found" not in out and "Traceback" not in out
    assert len(nonblank(out)) == 1, out
    assert tree(world["home"]) == {}


@pytest.mark.skipif(sys.platform == "darwin", reason="refusal only observable on a non-macOS host")
def test_refuses_non_macos_with_one_line(world):
    res = run(INSTALL, ["--dry-run"], world)
    assert res.returncode != 0
    assert len(nonblank(res.stdout + res.stderr)) == 1


# ===== installer: dry run =============================================================================================

@on_mac
def test_dry_run_mentions_every_step(world):
    res = run(INSTALL, ["--dry-run"], world)
    assert res.returncode == 0, res.stderr
    out = res.stdout
    assert TARBALL.format(ref="main") in out
    assert "venv" in out
    assert re.search(r"pip\b.*\binstall", out)
    assert "Outreach.command" in out
    assert not re.search(r"\bsudo\b", out)
    assert len(nonblank(out)) >= 6


@on_mac
def test_dry_run_creates_nothing_and_calls_no_external_tool(world):
    res = run(INSTALL, ["--dry-run"], world)
    assert res.returncode == 0, res.stderr
    assert tree(world["home"]) == {}, "dry run must not create files in HOME"
    assert tree(world["cwd"]) == {}
    assert fake_calls(world) == [], "dry run touched: %s" % fake_calls(world)


@on_mac
def test_dry_run_does_not_need_git(world):
    # Fake git exits 99 and records any use; the real one is not on PATH ahead of it.
    res = run(INSTALL, ["--dry-run"], world)
    assert res.returncode == 0
    assert "git" not in fake_calls(world)
    assert not re.search(r"\bgit (clone|pull|fetch)\b", res.stdout)


@on_mac
def test_dry_run_defaults_land_under_home(world):
    res = run(INSTALL, ["--dry-run"], world)
    home = str(world["home"])
    assert home + "/vcoutreach" in res.stdout
    assert home + "/Documents/vcoutreach-workspace" in res.stdout
    assert home + "/Desktop" in res.stdout


@on_mac
def test_no_launcher_on_desktop_drops_the_desktop_copy(world):
    res = run(INSTALL, ["--dry-run", "--no-launcher-on-desktop"], world)
    assert res.returncode == 0
    desktop = str(world["home"] / "Desktop")
    for line in res.stdout.splitlines():
        if desktop in line:
            assert not re.match(r"Would (run|write|preserve)", line), "Desktop is still being written: " + line
    assert "Outreach.command" in res.stdout, "the launcher inside the install folder is still made"


@on_mac
@pytest.mark.parametrize("ref", ["v1.2.3", "feature/x", "my-branch", "release_2"])
def test_ref_changes_the_download_url(world, ref):
    res = run(INSTALL, ["--dry-run", "--ref", ref], world)
    assert res.returncode == 0, res.stderr
    assert TARBALL.format(ref=ref) in res.stdout
    assert TARBALL.format(ref="main") not in res.stdout


@on_mac
def test_paths_with_spaces_are_kept_whole(world):
    d = world["tmp"] / "my apps" / "vc install"
    ws = world["tmp"] / "my docs" / "work space"
    res = run(INSTALL, ["--dry-run", "--dir", str(d), "--workspace", str(ws)], world)
    assert res.returncode == 0, res.stderr
    assert shown(d, res.stdout), res.stdout
    assert shown(ws, res.stdout), res.stdout
    assert not d.exists() and not ws.exists()
    assert fake_calls(world) == []


@on_mac
def test_custom_dir_replaces_the_default(world):
    d = world["tmp"] / "custom"
    res = run(INSTALL, ["--dry-run", "--dir", str(d)], world)
    assert str(d) in res.stdout
    assert str(world["home"] / "vcoutreach") not in res.stdout


@on_mac
def test_dry_run_leaves_an_existing_install_untouched(world):
    d = world["tmp"] / "existing"
    d.mkdir()
    (d / "keep.txt").write_text("precious")
    ws = world["tmp"] / "ws"
    ws.mkdir()
    (ws / "contacts.csv").write_text("id,name,email\n")
    before = (tree(d), tree(ws))
    res = run(INSTALL, ["--dry-run", "--dir", str(d), "--workspace", str(ws)], world)
    assert res.returncode == 0
    assert (tree(d), tree(ws)) == before


@on_mac
def test_dry_run_is_repeatable(world):
    a = run(INSTALL, ["--dry-run"], world).stdout
    b = run(INSTALL, ["--dry-run"], world).stdout
    assert a == b


@on_mac
@pytest.mark.parametrize(
    "evil_args",
    [
        ["--ref", "main;touch PWNED_ref"],
        ["--ref", "$(touch PWNED_ref)"],
        ["--ref", "`touch PWNED_ref`"],
        ["--dir", "x;touch PWNED_ref"],
        ["--dir", "$(touch PWNED_ref)"],
        ["--workspace", "`touch PWNED_ref`"],
        ["--workspace", "a' ; touch PWNED_ref ; '"],
    ],
)
def test_hostile_argument_values_are_never_executed(world, evil_args):
    res = run(INSTALL, ["--dry-run"] + evil_args, world)
    for base in (world["cwd"], world["home"], world["tmp"]):
        assert not list(base.rglob("PWNED_ref")), "argument was executed as shell code"
    assert fake_calls(world) == []
    if res.returncode != 0:
        assert len(nonblank(res.stdout + res.stderr)) == 1


@on_mac
def test_the_generated_launcher_works_with_spaces_in_every_path(world):
    d = world["tmp"] / "app dir" / "vc"
    ws = world["tmp"] / "my work" / "space ws"
    res = run(INSTALL, ["--dry-run", "--dir", str(d), "--workspace", str(ws), "--no-launcher-on-desktop"], world)
    assert res.returncode == 0, res.stderr
    lines = res.stdout.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("#!"))
    end = start
    while end + 1 < len(lines) and not re.match(r"(Would|Dry run|Installing|Open|Update|Remove|To keep)\b", lines[end + 1]):
        end += 1
    launcher_text = "\n".join(lines[start : end + 1]) + "\n"
    # Stand in a fake program for the real venv entry point, then run the launcher for real.
    fake = d / ".venv" / "bin" / "vcoutreach"
    fake.parent.mkdir(parents=True)
    fake.write_text('#!/bin/sh\nfor a in "$@"; do printf "ARG<%s>\\n" "$a"; done\npwd\n')
    fake.chmod(0o755)
    ws.mkdir(parents=True)
    launcher = world["tmp"] / "launcher.command"
    launcher.write_text(launcher_text)
    assert subprocess.run(["bash", "-n", str(launcher)]).returncode == 0
    out = subprocess.run(["bash", str(launcher)], capture_output=True, text=True, cwd="/", timeout=30,
                         env={"PATH": "/usr/bin:/bin", "HOME": str(world["home"])})
    assert out.returncode == 0, out.stderr
    assert "ARG<ui>" in out.stdout
    assert "ARG<--workspace>" in out.stdout
    assert "ARG<%s>" % ws in out.stdout, "the workspace must reach `vcoutreach ui` as one argument"
    assert out.stdout.rstrip().splitlines()[-1] == str(ws), "the launcher should start inside the workspace"


@on_mac
def test_dry_run_says_nothing_was_changed_and_no_network(world):
    out = run(INSTALL, ["--dry-run"], world).stdout.lower()
    assert "dry run" in out
    assert "no files were changed" in out or "nothing" in out


# ===== uninstaller ====================================================================================================

def _fake_install(w, name="vc install", ws_name="my workspace"):
    d = w["tmp"] / name
    d.mkdir()
    (d / "Outreach.command").write_text("#!/bin/bash\n")
    (d / "pyproject.toml").write_text("[project]\nname='vcoutreach'\n")
    ws = w["tmp"] / ws_name
    ws.mkdir()
    (ws / "contacts.csv").write_text("id,name,email\nmine,Mine,mine@example.com\n")
    (ws / ".env").write_text("OPENAI_API_KEY=abc\n")
    return d, ws


@on_mac
def test_uninstall_dry_run_keeps_the_workspace_and_changes_nothing(world):
    d, ws = _fake_install(world)
    before = (tree(d), tree(ws))
    res = run(UNINSTALL, ["--dry-run", "--dir", str(d), "--workspace", str(ws)], world)
    assert res.returncode == 0, res.stderr
    out = res.stdout
    assert shown(d, out) and shown(ws, out), "paths with spaces must appear whole"
    assert re.search(r"\bkeep", out, re.I)
    assert (tree(d), tree(ws)) == before
    assert fake_calls(world) == []
    assert tree(world["home"]) == {}


@on_mac
def test_uninstall_dry_run_with_defaults_touches_nothing(world):
    res = run(UNINSTALL, ["--dry-run"], world)
    assert res.returncode == 0, res.stderr
    assert re.search(r"\bkeep", res.stdout, re.I)
    assert str(world["home"] / "Documents" / "vcoutreach-workspace") in res.stdout
    assert tree(world["home"]) == {} and tree(world["cwd"]) == {}
    assert not re.search(r"\bsudo\b", res.stdout)


@on_mac
def test_uninstall_dry_run_never_plans_to_delete_the_workspace(world):
    d, ws = _fake_install(world, "inst", "ws")
    res = run(UNINSTALL, ["--dry-run", "--dir", str(d), "--workspace", str(ws)], world)
    for line in res.stdout.splitlines():
        if re.search(r"\brm\b|\bremove\b|\bdelete\b", line, re.I) and str(ws) in line:
            assert re.search(r"\b(ask|keep|unless|only if|confirm|never)\b", line, re.I), line


@on_mac
def test_real_uninstall_leaves_the_workspace_by_default(world):
    d, ws = _fake_install(world, "inst", "ws")
    ws_before = tree(ws)
    run(UNINSTALL, ["--dir", str(d), "--workspace", str(ws)], world, timeout=60)
    assert tree(ws) == ws_before, "contacts and keys in the workspace must survive an uninstall"
    assert (ws / ".env").read_text() == "OPENAI_API_KEY=abc\n"


@on_mac
def test_real_uninstall_with_a_workspace_inside_the_install_dir_still_keeps_it(world):
    d, _ = _fake_install(world, "inst", "unused")
    inner = d / "workspace"
    inner.mkdir()
    (inner / "contacts.csv").write_text("id,name,email\nmine,Mine,mine@example.com\n")
    run(UNINSTALL, ["--dir", str(d), "--workspace", str(inner)], world, timeout=60)
    assert (inner / "contacts.csv").is_file(), "the workspace must survive even when it sits inside the install dir"


@on_mac
def test_uninstall_unknown_flag_is_a_one_line_error(world):
    res = run(UNINSTALL, ["--bogus"], world)
    assert res.returncode != 0
    assert len(nonblank(res.stdout + res.stderr)) == 1


@on_mac
@pytest.mark.parametrize("flag", ["--dir", "--workspace"])
def test_uninstall_flag_missing_its_value(world, flag):
    res = run(UNINSTALL, ["--dry-run", flag], world)
    assert res.returncode != 0
    assert "unbound variable" not in res.stdout + res.stderr
    assert len(nonblank(res.stdout + res.stderr)) == 1


@on_mac
@pytest.mark.parametrize("bad", ["/", "HOME", "/usr", "/Users", "/Applications"])
def test_uninstall_does_not_plan_to_remove_system_or_home_dirs(world, bad):
    bad = str(world["home"]) if bad == "HOME" else bad
    res = run(UNINSTALL, ["--dry-run", "--dir", bad], world)
    assert not re.search(r"rm -rf -- %s\s*$" % re.escape(bad), res.stdout, re.M), (bad, res.stdout)
