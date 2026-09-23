"""The publication protocol, under fault injection.

Every test pins the same two promises: a failed publish leaves the pointer
naming the previous complete cycle, and a pointer-resolved read never
assembles objects from two cycles. FakeS3 is deliberately bare -- it models
put/get/delete/list exactly as boto3 shapes them, so the faults land on the
same calls the real client makes.
"""
import json

import pytest

from src import publish, s3io
from tests.fake_s3 import FakeS3

BUCKET = "dashboard-site"
PREFIX = "git-activity/data"


def payloads(tag):
    """One cycle's payloads, every byte tagged with the cycle so a mixed
    read can never pass for a complete one."""
    return [
        ("hourly.parquet", f"hourly-{tag}".encode(), "application/octet-stream"),
        ("commits.parquet", f"commits-{tag}".encode(), "application/octet-stream"),
        ("bead_events.parquet", f"beads-{tag}".encode(), "application/octet-stream"),
        ("meta.json", json.dumps({"cycle_id": tag}).encode(), "application/json"),
    ]


def do_publish(s3, tag, retention=publish.RETAINED_CYCLES):
    return publish.publish_cycle(
        s3, BUCKET, PREFIX, payloads(tag), cycle_id=f"cyc-{tag}",
        generated_at=f"2026-09-23T18:0{tag}0Z", retention=retention,
    )


def read_via_pointer(s3):
    """The consumer read path: pointer first, then every object it names."""
    raw = s3io.download_bytes(s3, BUCKET, f"{PREFIX}/current.json")
    if raw is None:
        return None, {}
    ptr = json.loads(raw)
    got = {
        name: s3io.download_bytes(s3, BUCKET, f"{PREFIX}/{key}")
        for name, key in ptr["objects"].items()
    }
    return ptr, got


def fixed_keys(s3):
    return {
        name: s3io.download_bytes(s3, BUCKET, f"{PREFIX}/{name}")
        for name in ("hourly.parquet", "commits.parquet", "bead_events.parquet", "meta.json")
    }


def assert_pointer_view_is(tag, s3):
    ptr, got = read_via_pointer(s3)
    assert ptr["cycle_id"] == f"cyc-{tag}"
    assert set(got) == {"hourly.parquet", "commits.parquet", "bead_events.parquet", "meta.json"}
    assert all(b is not None for b in got.values()), "pointer named an object that is missing"
    assert json.loads(got["meta.json"])["cycle_id"] == tag, \
        "pointer and its meta.json name different cycles"


def test_happy_path_stages_mirrors_and_commits():
    s3 = FakeS3()
    do_publish(s3, "A")

    for name, data, _ in payloads("A"):
        staged = s3io.download_bytes(s3, BUCKET, f"{PREFIX}/cycles/cyc-A/{name}")
        assert staged == data
        assert s3io.download_bytes(s3, BUCKET, f"{PREFIX}/{name}") == data
    assert_pointer_view_is("A", s3)


def test_meta_json_is_always_mirrored_last():
    # Even a caller that lists meta first gets the protocol's order: meta is
    # the fixed keys' completion marker.
    s3 = FakeS3()
    reordered = list(reversed(payloads("A")))
    publish.publish_cycle(s3, BUCKET, PREFIX, reordered, cycle_id="cyc-A",
                          generated_at="2026-09-23T18:00:00Z")
    fixed_puts = [k for k in s3.puts
                  if k.startswith(f"{PREFIX}/") and "cycles/" not in k
                  and not k.endswith("/current.json")]
    assert fixed_puts[-1] == f"{PREFIX}/meta.json"
    assert len(fixed_puts) == 4


def test_pointer_document_shape():
    s3 = FakeS3()
    do_publish(s3, "A")
    ptr = json.loads(s3io.download_bytes(s3, BUCKET, f"{PREFIX}/current.json"))
    assert ptr["schema_version"] == publish.POINTER_SCHEMA_VERSION
    assert ptr["generated_at"] == "2026-09-23T18:0A0Z"
    assert ptr["objects"] == {name: f"cycles/cyc-A/{name}" for name, _, _ in payloads("A")}
    for key in ptr["objects"].values():
        assert not key.startswith(PREFIX), "keys are relative to the prefix, not absolute"


def test_cycle_ids_are_sortable_and_unique():
    a = publish.new_cycle_id("2026-09-23T18:55:01Z")
    b = publish.new_cycle_id("2026-09-23T18:55:02Z")
    assert a < b and a != b
    assert publish.new_cycle_id("2026-09-23T18:55:01Z") != a


def test_staging_failure_leaves_previous_cycle_live_everywhere():
    s3 = FakeS3()
    do_publish(s3, "A")

    s3.fail_when(lambda op, key: RuntimeError("stage boom")
                 if op == "put" and "cyc-B" in key and key.endswith("/commits.parquet")
                 else None)
    with pytest.raises(publish.PublicationError):
        do_publish(s3, "B")

    assert_pointer_view_is("A", s3)
    assert fixed_keys(s3) == {name: data for name, data, _ in payloads("A")}


def test_mirror_failure_rolls_fixed_keys_back():
    s3 = FakeS3()
    do_publish(s3, "A")

    # Fail the fixed-key (not staged) put of bead_events: two mirrors have
    # already landed when it fires, which is exactly the half-mirrored set
    # the rollback exists to undo.
    s3.fail_when(lambda op, key: RuntimeError("mirror boom")
                 if op == "put" and key.endswith("/bead_events.parquet")
                 and "cycles/" not in key else None)
    with pytest.raises(publish.PublicationError) as excinfo:
        do_publish(s3, "B")

    assert "mirror" in str(excinfo.value.__cause__)
    assert_pointer_view_is("A", s3)
    assert fixed_keys(s3) == {name: data for name, data, _ in payloads("A")}, \
        "every fixed key must hold the previous cycle again, including the ones already mirrored"


def test_mirror_failure_on_first_run_cleans_the_partial_mirror():
    s3 = FakeS3()
    s3.fail_when(lambda op, key: RuntimeError("mirror boom")
                 if op == "put" and key.endswith("/meta.json")
                 and "cycles/" not in key else None)
    with pytest.raises(publish.PublicationError):
        do_publish(s3, "A")

    # Nothing existed before, so "previous complete dataset" is none: the
    # partial mirror must be gone, not half-left.
    assert fixed_keys(s3) == {name: None for name, _, _ in payloads("A")}
    assert read_via_pointer(s3)[0] is None


def test_pointer_failure_rolls_the_mirror_back():
    s3 = FakeS3()
    do_publish(s3, "A")

    s3.fail_when(lambda op, key: RuntimeError("pointer boom")
                 if op == "put" and key.endswith("/current.json") else None)
    with pytest.raises(publish.PublicationError):
        do_publish(s3, "B")

    # The mirror had already flipped the fixed keys to B; the rollback is
    # what stops the pointer and the fixed keys naming different cycles.
    assert_pointer_view_is("A", s3)
    assert fixed_keys(s3) == {name: data for name, data, _ in payloads("A")}


def test_rollback_failure_does_not_mask_the_original_error():
    s3 = FakeS3()
    do_publish(s3, "A")

    def every_fixed_put_booms(op, key):
        if op == "put" and "cycles/" not in key:
            return RuntimeError("everything on the fixed keys is failing")
        return None

    s3.fail_when(every_fixed_put_booms)
    with pytest.raises(publish.PublicationError) as excinfo:
        do_publish(s3, "B")

    assert "mirror" in str(excinfo.value), \
        "the cycle must be reported as the mirror failure, not the rollback's"
    assert_pointer_view_is("A", s3)  # the pointer at least still holds


def test_prune_keeps_pointer_cycle_and_newest():
    s3 = FakeS3()
    for tag in ("A", "B", "C", "D", "E"):
        do_publish(s3, tag)

    prefixes = s3io.list_prefixes(s3, BUCKET, f"{PREFIX}/cycles/")
    assert prefixes == [f"{PREFIX}/cycles/cyc-{t}/" for t in ("C", "D", "E")]
    assert_pointer_view_is("E", s3)
    assert s3io.list_keys(s3, BUCKET, f"{PREFIX}/cycles/cyc-A/") == []


def test_prune_failure_is_not_fatal():
    s3 = FakeS3()
    do_publish(s3, "A")
    do_publish(s3, "B")
    s3.fail_when(lambda op, key: RuntimeError("delete boom")
                 if op == "delete" else None)
    do_publish(s3, "C")  # must not raise
    assert_pointer_view_is("C", s3)


def test_orphaned_staging_prefix_from_a_failed_cycle_is_swept():
    s3 = FakeS3()
    do_publish(s3, "A")
    s3.fail_when(lambda op, key: RuntimeError("stage boom")
                 if op == "put" and "cyc-B" in key and key.endswith("/commits.parquet")
                 else None)
    with pytest.raises(publish.PublicationError):
        do_publish(s3, "B")
    s3.fail_when(None)

    do_publish(s3, "C")
    do_publish(s3, "D")  # retention 3: {B-orphan, C, D} pushes A out; B goes next
    prefixes = s3io.list_prefixes(s3, BUCKET, f"{PREFIX}/cycles/")
    assert [p.rstrip("/").rsplit("/", 1)[-1] for p in prefixes] == ["cyc-B", "cyc-C", "cyc-D"]
    do_publish(s3, "E")
    prefixes = s3io.list_prefixes(s3, BUCKET, f"{PREFIX}/cycles/")
    assert [p.rstrip("/").rsplit("/", 1)[-1] for p in prefixes] == ["cyc-C", "cyc-D", "cyc-E"]


def test_duplicate_payload_names_are_rejected():
    s3 = FakeS3()
    dupe = payloads("A") + [("meta.json", b"{}", "application/json")]
    with pytest.raises(publish.PublicationError, match="duplicate"):
        publish.publish_cycle(s3, BUCKET, PREFIX, dupe, cycle_id="cyc-A",
                              generated_at="2026-09-23T18:00:00Z")
    assert s3.objects == {}, "a rejected payload set must not upload anything"


@pytest.mark.parametrize("fail_at_put", list(range(9)))
def test_no_fault_injection_point_ever_leaves_a_mixed_view(fail_at_put):
    """The centerpiece: fail the Nth PUT of publishing B over committed A,
    for every N. A consumer resolving through the pointer must see exactly
    cycle A every time -- never a mix, never a missing object. 9 PUTs =
    4 staged + 4 mirrored + 1 pointer."""
    s3 = FakeS3()
    do_publish(s3, "A")

    seen = {"n": 0}

    def fail_nth_put(op, key):
        if op != "put":
            return None
        seen["n"] += 1
        if seen["n"] == fail_at_put + 1:
            return RuntimeError(f"injected failure at put {fail_at_put}")
        return None

    s3.fail_when(fail_nth_put)
    with pytest.raises(publish.PublicationError):
        do_publish(s3, "B")

    assert_pointer_view_is("A", s3)
    assert fixed_keys(s3) == {name: data for name, data, _ in payloads("A")}, \
        "after a failed publish the fixed keys must hold the previous complete cycle too"


@pytest.mark.parametrize("fail_at_put", list(range(9)))
def test_first_ever_publish_failure_leaves_no_partial_state(fail_at_put):
    """Same sweep against an empty prefix: 'previous complete dataset' is
    nothing, so nothing partial may survive a failed first publish. 9 PUTs
    here too: 4 staged + 4 mirrored + 1 pointer."""
    s3 = FakeS3()
    seen = {"n": 0}

    def fail_nth_put(op, key):
        if op != "put":
            return None
        seen["n"] += 1
        if seen["n"] == fail_at_put + 1:
            return RuntimeError(f"injected failure at put {fail_at_put}")
        return None

    s3.fail_when(fail_nth_put)
    with pytest.raises(publish.PublicationError):
        do_publish(s3, "A")

    assert read_via_pointer(s3)[0] is None
    assert all(v is None for v in fixed_keys(s3).values())
