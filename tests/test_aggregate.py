from src import aggregate, families


def _commit(repo, ts, added=10, deleted=5, files=2, bulk=False, raw=None):
    return {
        "sha": f"{repo}{ts}", "repo": repo, "ts": ts, "author_email": "e@x",
        "subject": "s", "bead_id": None,
        "lines_added": added, "lines_deleted": deleted, "files_changed": files,
        "lines_added_raw": raw if raw is not None else added,
        "lines_deleted_raw": deleted, "files_changed_raw": files,
        "is_bulk": bulk,
    }


FAMILY_MAP = {"NEEDLE": "agent-fleet", "FABRIC": "agent-fleet"}


def test_every_tier_sums_from_the_same_grain():
    # The scope hierarchy is only trustworthy if repo -> family -> ecosystem
    # are literally sums of one table. This is the property the panel relies
    # on to compute all three tiers client-side.
    commits = [
        _commit("NEEDLE", 3600 * 100), _commit("NEEDLE", 3600 * 100),
        _commit("FABRIC", 3600 * 100), _commit("vista", 3600 * 100),
    ]
    rows = aggregate.build_hourly(commits, [], FAMILY_MAP)

    by_repo = {r["repo"]: r["commits"] for r in rows}
    assert by_repo == {"NEEDLE": 2, "FABRIC": 1, "vista": 1}
    fleet = sum(r["commits"] for r in rows if r["family"] == "agent-fleet")
    assert fleet == 3
    assert sum(r["commits"] for r in rows) == 4


def test_unmapped_repo_falls_through_to_unassigned():
    rows = aggregate.build_hourly([_commit("brand-new-repo", 3600 * 5)], [], FAMILY_MAP)
    assert rows[0]["family"] == families.UNASSIGNED


def test_bulk_commit_counts_as_a_commit_but_not_as_lines():
    commits = [_commit("NEEDLE", 3600 * 7, added=9_000_000, deleted=0, files=133, bulk=True, raw=9_000_000)]
    rows = aggregate.build_hourly(commits, [], FAMILY_MAP)
    r = rows[0]
    assert r["commits"] == 1
    assert r["bulk_commits"] == 1
    assert r["lines_added"] == 0, "a bulk commit must not reach the trimmed total"
    assert r["lines_added_raw"] == 9_000_000, "raw must stay a true total"


def test_bulk_bead_closures_are_split_out_not_merged():
    events = [
        {"repo": "NEEDLE", "ts": 3600 * 9, "issue_id": "i1", "kind": "closed",
         "actor": "system", "is_bulk_import": True},
        {"repo": "NEEDLE", "ts": 3600 * 9, "issue_id": "i2", "kind": "closed",
         "actor": "system", "is_bulk_import": False},
    ]
    rows = aggregate.build_hourly([], events, FAMILY_MAP)
    assert rows[0]["beads_closed"] == 1
    assert rows[0]["beads_closed_bulk"] == 1


def test_workers_active_counts_distinct_claim_actors():
    events = [
        {"repo": "NEEDLE", "ts": 3600 * 9, "issue_id": "i1", "kind": "claimed", "actor": "w1"},
        {"repo": "NEEDLE", "ts": 3600 * 9, "issue_id": "i2", "kind": "claimed", "actor": "w1"},
        {"repo": "NEEDLE", "ts": 3600 * 9, "issue_id": "i3", "kind": "claimed", "actor": "w2"},
    ]
    rows = aggregate.build_hourly([], events, FAMILY_MAP)
    assert rows[0]["beads_claimed"] == 3
    assert rows[0]["workers_active"] == 2


def test_hour_bucket_is_utc_and_explicitly_zoned():
    rows = aggregate.build_hourly([_commit("NEEDLE", 1786899261)], [], FAMILY_MAP)
    assert rows[0]["hour_utc"].endswith("Z")
    assert rows[0]["hour_utc"] == "2026-08-16T16:00:00Z"  # 16:34:21Z truncated to the hour


def test_total_failure_produces_no_rows_to_publish():
    # Guards the invariant behind main's refuse-to-publish check: when every
    # repo fails there is genuinely nothing, and zeroes written over a good
    # dataset read as a quiet fleet rather than as the outage they are.
    assert aggregate.build_hourly([], [], FAMILY_MAP) == []
