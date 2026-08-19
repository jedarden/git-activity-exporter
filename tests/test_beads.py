from src import beads


def _ev(repo, hour, kind, actor="system", issue="x"):
    return {"repo": repo, "ts": hour * 3600, "issue_id": issue, "kind": kind, "actor": actor}


def test_bulk_hours_flag_dense_closure_cells_only():
    # The bead-rs migration produced (repo, hour) cells up to 1,966 closures
    # while the busiest genuine hour any repo has recorded is 69.
    events = [_ev("NEEDLE", 100, "closed") for _ in range(400)]
    events += [_ev("NEEDLE", 101, "closed") for _ in range(60)]
    out, bulk_cells = beads.mark_bulk_hours(events, 150)

    assert bulk_cells == {("NEEDLE", 100)}
    assert sum(1 for e in out if e["is_bulk_import"]) == 400
    assert sum(1 for e in out if not e["is_bulk_import"]) == 60


def test_bulk_flag_is_per_repo_not_global():
    # Two repos each below threshold in the same hour must not combine into a
    # false bulk-import flag.
    events = [_ev("a", 100, "closed") for _ in range(100)]
    events += [_ev("b", 100, "closed") for _ in range(100)]
    _, bulk_cells = beads.mark_bulk_hours(events, 150)
    assert bulk_cells == set()


def test_only_closures_can_be_bulk():
    events = [_ev("a", 100, "claimed") for _ in range(400)]
    out, bulk_cells = beads.mark_bulk_hours(events, 150)
    assert bulk_cells == set()
    assert not any(e["is_bulk_import"] for e in out)


def test_bulk_hour_contagion_catches_repos_under_the_per_repo_bar():
    # The real leak: during the bead-rs migration flush, several repos closed
    # 100-141 beads each -- individually under the 150 bar -- in the same
    # hours where thousands of other closures were already flagged. Per-repo
    # flagging alone let those through, and summed at ecosystem scope they
    # produced a 545-closure spike against a next-busiest hour of 81.
    events = [_ev("bulky", 100, "closed", issue=f"b{i}") for i in range(2000)]
    events += [_ev("telegram-claude-bridge", 100, "closed", issue=f"t{i}") for i in range(141)]
    events += [_ev("zai-proxy", 100, "closed", issue=f"z{i}") for i in range(123)]
    out, bulk_cells = beads.mark_bulk_hours(events, 150)

    assert ("telegram-claude-bridge", 100) in bulk_cells
    assert ("zai-proxy", 100) in bulk_cells
    assert all(e["is_bulk_import"] for e in out)


def test_a_busy_hour_without_a_mass_import_is_left_alone():
    # Contagion must not fire on a genuinely busy hour. Several repos each
    # well under the bar, none flagged, so nothing is contaminated.
    events = []
    for repo in ("a", "b", "c", "d"):
        events += [_ev(repo, 200, "closed", issue=f"{repo}{i}") for i in range(60)]
    out, bulk_cells = beads.mark_bulk_hours(events, 150)

    assert bulk_cells == set()
    assert not any(e["is_bulk_import"] for e in out)
