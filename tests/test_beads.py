from src import aggregate, beads


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


def test_bulk_close_threshold_is_strict_at_150_and_151():
    at_bar = [_ev("at-bar", 100, "closed", issue=f"at-{i}") for i in range(150)]
    over_bar = [_ev("over-bar", 101, "closed", issue=f"over-{i}") for i in range(151)]

    out, bulk_cells = beads.mark_bulk_hours(at_bar + over_bar, 150)

    assert bulk_cells == {("over-bar", 101)}
    assert all(not event["is_bulk_import"] for event in out[:150])
    assert all(event["is_bulk_import"] for event in out[150:])


def test_fleet_hour_share_escalates_at_exactly_half():
    flagged = [_ev("flagged", 200, "closed", issue=f"flagged-{i}") for i in range(151)]
    unflagged = [_ev("unflagged", 200, "closed", issue=f"unflagged-{i}") for i in range(149)]

    out, bulk_cells = beads.mark_bulk_hours(flagged + unflagged, 150, bulk_hour_share=0.5)

    assert bulk_cells == {("flagged", 200), ("unflagged", 200)}
    assert all(event["is_bulk_import"] for event in out)


def test_closure_split_preserves_the_pre_split_total():
    events = (
        [_ev("bulk", 300, "closed", issue=f"bulk-{i}") for i in range(151)]
        + [_ev("propagated", 300, "closed", issue=f"propagated-{i}") for i in range(149)]
        + [_ev("ordinary", 301, "closed", issue=f"ordinary-{i}") for i in range(2)]
    )
    pre_split_total = sum(event["kind"] == "closed" for event in events)

    marked, _ = beads.mark_bulk_hours(events, 150)
    rows = aggregate.build_hourly([], marked, {})
    repo_rows = {
        (row["repo"], row["hour_epoch"]): row
        for row in rows
        if row["worker"] is None
    }

    assert repo_rows[("bulk", 300)]["beads_closed_bulk"] == 151
    assert repo_rows[("propagated", 300)]["beads_closed_bulk"] == 149
    assert repo_rows[("ordinary", 301)]["beads_closed"] == 2
    assert sum(
        row["beads_closed"] + row["beads_closed_bulk"]
        for row in repo_rows.values()
    ) == pre_split_total
