"""SPEC section 22.10: a provider error that aborts draft_all keeps what was finished, writes the index and prunes nothing."""
from __future__ import annotations

import pytest

from vcoutreach.pipeline import draft_all
from vcoutreach.types import InsufficientFacts, ProviderAuthError


def contacts(kit, count):
    return [kit.contact(id=f"c{i}", name="Ada Lovelace", email=f"c{i}@example.com") for i in range(count)]


def scripted(kit, plan):
    """A provider that does what ``plan`` says for each contact id: ok, blocked or abort."""
    def behavior(request):
        action = plan.get(request.contact.id, "ok")
        if action == "abort":
            raise ProviderAuthError("the key was rejected")
        if action == "blocked":
            raise InsufficientFacts("nothing to say")
        return kit.hook(kit.HOOK, provider="stub")

    return kit.stub_provider(behavior)


def index_ids(kit, out):
    return [row["contact_id"] for row in kit.read_index(out)]


def test_the_drafts_finished_before_the_abort_stay_on_disk(kit):
    out = kit.workdir / "drafts"
    with pytest.raises(ProviderAuthError):
        draft_all(contacts(kit, 4), kit.sender(), scripted(kit, {"c2": "abort"}), out)
    assert sorted(p.name for p in out.glob("*.md")) == ["c0.md", "c1.md"]


def test_the_index_is_written_for_the_finished_drafts_only(kit):
    out = kit.workdir / "drafts"
    with pytest.raises(ProviderAuthError):
        draft_all(contacts(kit, 4), kit.sender(), scripted(kit, {"c2": "abort"}), out)
    assert index_ids(kit, out) == ["c0", "c1"]
    assert {row["status"] for row in kit.read_index(out)} == {"ok"}


def test_a_blocked_draft_finished_before_the_abort_is_in_the_index_too(kit):
    out = kit.workdir / "drafts"
    with pytest.raises(ProviderAuthError):
        draft_all(contacts(kit, 3), kit.sender(), scripted(kit, {"c0": "blocked", "c2": "abort"}), out)
    rows = kit.read_index(out)
    assert [(r["contact_id"], r["status"]) for r in rows] == [("c0", "blocked"), ("c1", "ok")]
    assert (out / "blocked" / "c0.md").exists() and (out / "c1.md").exists()


def test_an_abort_on_the_very_first_contact_writes_an_empty_index_or_none_at_all(kit):
    out = kit.workdir / "drafts"
    with pytest.raises(ProviderAuthError):
        draft_all(contacts(kit, 2), kit.sender(), scripted(kit, {"c0": "abort"}), out)
    assert list(out.glob("*.md")) == [] if out.exists() else True
    if (out / "index.jsonl").exists():
        assert index_ids(kit, out) == []


def test_an_abort_prunes_nothing_and_never_calls_on_prune(kit):
    out = kit.workdir / "drafts"
    draft_all(contacts(kit, 4), kit.sender(), scripted(kit, {}), out)
    assert index_ids(kit, out) == ["c0", "c1", "c2", "c3"]
    removed = []
    with pytest.raises(ProviderAuthError):
        draft_all(contacts(kit, 3), kit.sender(), scripted(kit, {"c1": "abort"}), out, on_prune=removed.append)
    assert removed == []
    assert (out / "c3.md").exists(), "c3 left the contacts file, but an aborted run must not delete anything"
    assert (out / "c0.md").exists()
    assert "c0" in index_ids(kit, out)


def test_a_complete_run_still_prunes_as_before(kit):
    out = kit.workdir / "drafts"
    draft_all(contacts(kit, 4), kit.sender(), scripted(kit, {}), out)
    removed = []
    draft_all(contacts(kit, 3), kit.sender(), scripted(kit, {}), out, on_prune=removed.append)
    assert removed == ["c3"] and not (out / "c3.md").exists()
    assert index_ids(kit, out) == ["c0", "c1", "c2"]
