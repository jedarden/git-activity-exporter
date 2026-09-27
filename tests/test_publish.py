"""The publication protocol, under fault injection.

Every test pins the same two promises: a failed publish leaves the pointer
naming the previous complete cycle, and a pointer-resolved read never
assembles objects from two cycles. FakeS3 is deliberately bare -- it models
put/get/delete/list exactly as boto3 shapes them, so the faults land on the
same calls the real client makes.
"""
import json

import pytest
from botocore.exceptions import ClientError

from src import publish, s3io
from tests.fake_s3 import FakeS3

BUCKET = "dashboard-site"
PREFIX = "git-activity/data"
_CYCLES = {
    tag: (
        f"2026-09-23T18:{index:02d}:00Z",
        f"20260923T18{index:02d}00Z-{index:07x}a",
    )
    for index, tag in enumerate("ABCDE", start=0)
}


def identity(tag):
    return _CYCLES[tag]


def cycle_id(tag):
    return identity(tag)[1]


def payloads(tag):
    """One cycle's payloads, every byte tagged with the cycle so a mixed
    read can never pass for a complete one."""
    generated_at, cycle = identity(tag)
    return [
        ("hourly.parquet", f"hourly-{tag}".encode(), "application/octet-stream"),
        ("commits.parquet", f"commits-{tag}".encode(), "application/octet-stream"),
        ("bead_events.parquet", f"beads-{tag}".encode(), "application/octet-stream"),
        ("meta.json", json.dumps({
            "cycle_id": cycle,
            "generated_at": generated_at,
        }).encode(), "application/json"),
    ]


def do_publish(s3, tag, retention=publish.RETAINED_CYCLES):
    generated_at, cycle = identity(tag)
    return publish.publish_cycle(
        s3, BUCKET, PREFIX, payloads(tag), cycle_id=cycle,
        generated_at=generated_at, retention=retention,
    )


def transient_s3_error(code="InternalError", status=500):
    return ClientError(
        {
            "Error": {"Code": code, "Message": "temporary S3 failure"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "PutObject",
    )


def put_key(prefix, name):
    return f"{PREFIX}/{prefix}/{name}"


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


class LegacySnapshotRejected(RuntimeError):
    """The legacy fixed-key reader could not prove a whole-cycle snapshot."""


def read_legacy_fixed_snapshot(s3, max_attempts=3):
    """Model the documented legacy consumer read contract.

    The root objects are only returned after they match both the pointer's
    metadata and every immutable object named by that pointer. This catches
    the data-before-meta window that a cycle-id-only check cannot see.
    """
    names = ("hourly.parquet", "commits.parquet", "bead_events.parquet", "meta.json")
    diagnostics = []
    pointer_key = f"{PREFIX}/current.json"

    for attempt in range(1, max_attempts + 1):
        pointer_cycle = None
        pointer_after_cycle = None
        meta_cycle = None
        try:
            pointer_raw = s3io.download_bytes(s3, BUCKET, pointer_key)
            if pointer_raw is None:
                raise LegacySnapshotRejected("current.json is missing")
            pointer = json.loads(pointer_raw)
            publish.validate_pointer_document(pointer)
            pointer_cycle = pointer["cycle_id"]

            fixed = {
                name: s3io.download_bytes(s3, BUCKET, f"{PREFIX}/{name}")
                for name in names
            }
            missing = [name for name, body in fixed.items() if body is None]
            if missing:
                raise LegacySnapshotRejected(f"missing fixed keys: {missing}")

            meta = json.loads(fixed["meta.json"])
            meta_cycle = meta.get("cycle_id")
            reasons = []
            if (
                meta.get("cycle_id") != pointer["cycle_id"]
                or meta.get("generated_at") != pointer["generated_at"]
            ):
                reasons.append(
                    "meta cycle "
                    f"{meta.get('cycle_id')} != pointer cycle {pointer['cycle_id']}"
                )

            mismatched = []
            for name in names:
                immutable = s3io.download_bytes(
                    s3, BUCKET, f"{PREFIX}/{pointer['objects'][name]}"
                )
                if immutable is None or fixed[name] != immutable:
                    mismatched.append(name)
            if mismatched:
                reasons.append(
                    f"fixed-key mismatch for pointer cycle {pointer['cycle_id']}: {mismatched}"
                )

            pointer_after = s3io.download_bytes(s3, BUCKET, pointer_key)
            if pointer_after != pointer_raw:
                try:
                    pointer_after_cycle = json.loads(pointer_after).get("cycle_id")
                except (AttributeError, TypeError, ValueError):
                    pointer_after_cycle = None
                reasons.append(
                    "current.json changed during the read: "
                    f"before {pointer_cycle}, after {pointer_after_cycle}"
                )

            if reasons:
                raise LegacySnapshotRejected("; ".join(reasons))
            return fixed
        except (
            LegacySnapshotRejected,
            KeyError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            publish.PublicationError,
            s3io.S3OperationError,
        ) as error:
            diagnostics.append(
                f"prefix={PREFIX} attempt={attempt} "
                f"pointer_before={pointer_cycle} pointer_after={pointer_after_cycle} "
                f"meta_cycle={meta_cycle}: {error}"
            )

    raise LegacySnapshotRejected("; ".join(diagnostics))


@pytest.fixture(autouse=True)
def reset_prune_health():
    publish.reset_prune_health()
    yield
    publish.reset_prune_health()


def assert_pointer_view_is(tag, s3):
    ptr, got = read_via_pointer(s3)
    assert ptr["cycle_id"] == cycle_id(tag)
    assert set(got) == {"hourly.parquet", "commits.parquet", "bead_events.parquet", "meta.json"}
    assert all(b is not None for b in got.values()), "pointer named an object that is missing"
    assert json.loads(got["meta.json"])["cycle_id"] == cycle_id(tag), \
        "pointer and its meta.json name different cycles"


def test_happy_path_stages_mirrors_and_commits():
    s3 = FakeS3()
    do_publish(s3, "A")

    for name, data, _ in payloads("A"):
        staged = s3io.download_bytes(
            s3, BUCKET, f"{PREFIX}/cycles/{cycle_id('A')}/{name}"
        )
        assert staged == data
        assert s3io.download_bytes(s3, BUCKET, f"{PREFIX}/{name}") == data
    assert_pointer_view_is("A", s3)


def test_publication_sets_content_type_and_cache_control_metadata():
    s3 = FakeS3()
    do_publish(s3, "A")

    for name, _, content_type in payloads("A"):
        cycle_key = f"{PREFIX}/cycles/{cycle_id('A')}/{name}"
        fixed_key = f"{PREFIX}/{name}"

        assert s3.head_object(Bucket=BUCKET, Key=cycle_key) == {
            "ContentLength": len(s3.objects[cycle_key][0]),
            "ContentType": content_type,
            "CacheControl": "public, max-age=31536000, immutable",
        }
        assert s3.head_object(Bucket=BUCKET, Key=fixed_key) == {
            "ContentLength": len(s3.objects[fixed_key][0]),
            "ContentType": content_type,
            "CacheControl": "no-cache, max-age=0, must-revalidate",
        }

    pointer_key = f"{PREFIX}/current.json"
    pointer_metadata = s3.head_object(Bucket=BUCKET, Key=pointer_key)
    assert pointer_metadata["ContentType"] == "application/json"
    assert pointer_metadata["CacheControl"] == "no-cache, max-age=0, must-revalidate"


def test_meta_json_is_always_mirrored_last():
    # Even a caller that lists meta first gets the protocol's order: meta is
    # the fixed keys' completion marker.
    s3 = FakeS3()
    reordered = list(reversed(payloads("A")))
    generated_at, cycle = identity("A")
    publish.publish_cycle(s3, BUCKET, PREFIX, reordered, cycle_id=cycle,
                          generated_at=generated_at)
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
    assert ptr["generated_at"] == identity("A")[0]
    assert ptr["objects"] == {
        name: f"cycles/{cycle_id('A')}/{name}" for name, _, _ in payloads("A")
    }
    for key in ptr["objects"].values():
        assert not key.startswith(PREFIX), "keys are relative to the prefix, not absolute"


def _stored_pointer(document):
    s3 = FakeS3()
    s3.objects[f"{PREFIX}/current.json"] = (
        json.dumps(document).encode(), "application/json"
    )
    return s3


def _valid_pointer_document():
    generated_at, cycle = identity("A")
    return json.loads(publish.pointer_bytes(
        cycle, generated_at, [name for name, _, _ in payloads("A")]
    ))


@pytest.mark.parametrize("missing", publish.POINTER_REQUIRED_FIELDS)
def test_pointer_reader_rejects_each_missing_required_field(missing):
    document = _valid_pointer_document()
    del document[missing]
    s3 = _stored_pointer(document)

    state = publish._read_pointer_state(s3, BUCKET, f"{PREFIX}/current.json")

    assert state.present is True
    assert state.document is None
    assert f"pointer missing required field(s): {missing}" == state.invalid_reason


def test_pointer_reader_accepts_additive_optional_fields():
    document = _valid_pointer_document()
    document["producer"] = {"release": "0.1.37"}
    s3 = _stored_pointer(document)

    state = publish._read_pointer_state(s3, BUCKET, f"{PREFIX}/current.json")

    assert state.document == document


@pytest.mark.parametrize("schema_version", [0, 2, "1", True, None])
def test_pointer_reader_rejects_unknown_or_non_integer_schema_versions(schema_version):
    document = _valid_pointer_document()
    document["schema_version"] = schema_version
    s3 = _stored_pointer(document)

    state = publish._read_pointer_state(s3, BUCKET, f"{PREFIX}/current.json")

    assert state.present is True
    assert state.document is None
    assert "unsupported pointer schema_version" in state.invalid_reason


def test_unusable_pointer_is_not_dereferenced_for_recovery():
    document = _valid_pointer_document()
    document["schema_version"] = 2
    s3 = _stored_pointer(document)
    named_object = put_key(f"cycles/{cycle_id('A')}", "hourly.parquet")
    s3.objects[named_object] = (b"must not be fetched", "application/octet-stream")

    assert publish.recover_publication(
        s3, BUCKET, PREFIX, expected_names=publish.DEFAULT_FIXED_NAMES
    ) is None
    assert ("get", named_object) not in s3.calls


def test_absent_pointer_bootstraps_without_pruning_existing_cycles():
    s3 = FakeS3()
    orphan = f"{PREFIX}/cycles/{cycle_id('A')}/diagnostic.txt"
    s3.objects[orphan] = (b"keep until a valid pointer exists", "text/plain")

    do_publish(s3, "B")

    assert_pointer_view_is("B", s3)
    assert s3.objects[orphan][0] == b"keep until a valid pointer exists"


@pytest.mark.parametrize(
    "pointer_document",
    [
        b"not-json",
        json.dumps({"schema_version": publish.POINTER_SCHEMA_VERSION + 1}).encode(),
    ],
    ids=("malformed", "unsupported-schema"),
)
def test_unusable_pointer_bootstraps_and_skips_destructive_pruning(pointer_document):
    s3 = FakeS3()
    pointer_key = f"{PREFIX}/current.json"
    s3.objects[pointer_key] = (pointer_document, "application/json")
    preserved = f"{PREFIX}/cycles/{cycle_id('A')}/operator-note"
    s3.objects[preserved] = (b"preserve", "text/plain")

    do_publish(s3, "B")

    assert_pointer_view_is("B", s3)
    assert s3.objects[preserved][0] == b"preserve"


def test_pointer_outside_configured_prefix_is_never_fetched_or_pruned():
    s3 = FakeS3()
    pointer = json.loads(publish.pointer_bytes(
        cycle_id("A"), identity("A")[0], [name for name, _, _ in payloads("A")]
    ))
    pointer["objects"]["hourly.parquet"] = "../outside/hourly.parquet"
    s3.objects[f"{PREFIX}/current.json"] = (
        json.dumps(pointer).encode(), "application/json"
    )
    outside = f"{PREFIX}/../outside/hourly.parquet"
    s3.objects[outside] = (b"must not be read", "application/octet-stream")
    preserved = f"{PREFIX}/cycles/{cycle_id('A')}/operator-note"
    s3.objects[preserved] = (b"preserve", "text/plain")

    do_publish(s3, "B")

    assert_pointer_view_is("B", s3)
    assert s3.objects[outside][0] == b"must not be read"
    assert s3.objects[preserved][0] == b"preserve"
    assert ("get", outside) not in s3.calls


def test_unusable_pointer_is_retained_until_a_complete_stage_exists():
    s3 = FakeS3()
    pointer_key = f"{PREFIX}/current.json"
    invalid_pointer = b"{broken"
    s3.objects[pointer_key] = (invalid_pointer, "application/json")
    s3.fail_when(
        lambda op, key: RuntimeError("stage boom")
        if op == "put"
        and key == put_key(f"cycles/{cycle_id('B')}", "meta.json")
        else None
    )

    with pytest.raises(publish.PublicationError):
        do_publish(s3, "B")

    assert s3.objects[pointer_key][0] == invalid_pointer
    assert s3io.list_keys(s3, BUCKET, f"{PREFIX}/cycles/{cycle_id('B')}/")


def test_missing_pointer_object_is_not_used_as_rollback_source():
    s3 = FakeS3()
    do_publish(s3, "A")
    del s3.objects[put_key(f"cycles/{cycle_id('A')}", "hourly.parquet")]
    preserved = put_key(f"cycles/{cycle_id('A')}", "commits.parquet")

    do_publish(s3, "B")

    assert_pointer_view_is("B", s3)
    assert s3.objects[preserved][0] == b"commits-A"
    assert s3io.list_keys(s3, BUCKET, f"{PREFIX}/cycles/{cycle_id('A')}/")


def test_pointer_missing_configured_object_name_is_not_used_as_rollback_source():
    s3 = FakeS3()
    do_publish(s3, "A")
    pointer_key = f"{PREFIX}/current.json"
    pointer = json.loads(s3.objects[pointer_key][0])
    del pointer["objects"]["bead_events.parquet"]
    s3.objects[pointer_key] = (json.dumps(pointer).encode(), "application/json")
    preserved = put_key(f"cycles/{cycle_id('A')}", "commits.parquet")

    do_publish(s3, "B")

    assert_pointer_view_is("B", s3)
    assert s3.objects[preserved][0] == b"commits-A"


def test_pointer_meta_mismatch_is_not_used_as_rollback_source():
    s3 = FakeS3()
    do_publish(s3, "A")
    meta_key = put_key(f"cycles/{cycle_id('A')}", "meta.json")
    replacement_meta = next(data for name, data, _ in payloads("B") if name == "meta.json")
    s3.objects[meta_key] = (replacement_meta, "application/json")

    do_publish(s3, "B")

    assert_pointer_view_is("B", s3)
    assert s3.objects[meta_key][0] == replacement_meta


def test_legacy_fixed_reader_accepts_only_a_complete_pointer_cycle():
    s3 = FakeS3()
    do_publish(s3, "A")

    assert read_legacy_fixed_snapshot(s3) == fixed_keys(s3)


@pytest.mark.parametrize("mixed_state", ("new-data-old-meta", "old-data-new-meta"))
def test_legacy_fixed_reader_never_accepts_a_mixed_snapshot(mixed_state):
    """A marker comparison alone must not make a mixed root look complete."""
    s3 = FakeS3()
    do_publish(s3, "A")
    fixed_a = fixed_keys(s3)
    pointer_a = s3.objects[f"{PREFIX}/current.json"]

    do_publish(s3, "B")
    fixed_b = fixed_keys(s3)
    # The immutable A cycle remains available and current.json is restored to
    # the authority a legacy reader observed before B's mirror began.
    s3.objects[f"{PREFIX}/current.json"] = pointer_a

    if mixed_state == "new-data-old-meta":
        mixed = dict(fixed_b)
        mixed["meta.json"] = fixed_a["meta.json"]
    else:
        mixed = dict(fixed_a)
        mixed["meta.json"] = fixed_b["meta.json"]
    for name, body in mixed.items():
        key = f"{PREFIX}/{name}"
        s3.objects[key] = (body, s3.objects[key][1])

    with pytest.raises(LegacySnapshotRejected, match="fixed-key mismatch|meta cycle") as error:
        read_legacy_fixed_snapshot(s3)

    assert "attempt=3" in str(error.value)
    assert cycle_id("A") in str(error.value)


def test_cycle_id_format_names_generated_at(monkeypatch):
    class FixedUUID:
        hex = "0123456789abcdef0123456789abcdef"

    monkeypatch.setattr(publish.uuid, "uuid4", lambda: FixedUUID())
    generated_at = "2026-09-23T18:55:01Z"
    cycle = publish.new_cycle_id(generated_at)

    assert cycle == "20260923T185501Z-01234567"
    publish.validate_cycle_id(cycle, generated_at)


def test_cycle_ids_sort_by_timestamp_and_same_second_suffix(monkeypatch):
    generated_at = "2026-09-23T18:55:01Z"
    values = iter(("0000000a", "0000000b", "0000000c"))
    monkeypatch.setattr(
        publish.uuid, "uuid4",
        lambda: type("UUID", (), {"hex": next(values)})(),
    )

    first = publish.new_cycle_id(generated_at)
    second = publish.new_cycle_id(generated_at)

    assert first == "20260923T185501Z-0000000a"
    assert second == "20260923T185501Z-0000000b"
    assert first < second
    assert first != publish.new_cycle_id("2026-09-23T18:55:02Z")


@pytest.mark.parametrize(
    "generated_at",
    [
        "2026-09-23T18:55:01",
        "2026-02-30T18:55:01Z",
        "2026-09-23T18:55:01+00:00",
    ],
)
def test_cycle_id_rejects_noncanonical_generated_at(generated_at):
    with pytest.raises(publish.PublicationError, match="generated_at"):
        publish.new_cycle_id(generated_at)


@pytest.mark.parametrize(
    "cycle,generated_at",
    [
        ("20260923T185501Z-0123456", "2026-09-23T18:55:01Z"),
        ("20260923T185501Z-0123456G", "2026-09-23T18:55:01Z"),
        ("20260924T185501Z-01234567", "2026-09-23T18:55:01Z"),
    ],
)
def test_cycle_id_validation_rejects_malformed_or_mismatched_ids(cycle, generated_at):
    with pytest.raises(publish.PublicationError):
        publish.validate_cycle_id(cycle, generated_at)


def test_existing_cycle_id_is_a_collision_before_any_write():
    s3 = FakeS3()
    do_publish(s3, "A")
    puts = list(s3.puts)
    objects = dict(s3.objects)
    pointer = s3.objects[f"{PREFIX}/current.json"]
    fixed = fixed_keys(s3)

    generated_at, cycle = identity("A")
    with pytest.raises(publish.PublicationError, match="collision"):
        publish.publish_cycle(
            s3, BUCKET, PREFIX, payloads("B"), cycle_id=cycle,
            generated_at=generated_at,
        )

    assert s3.puts == puts
    assert s3.objects == objects
    assert s3.objects[f"{PREFIX}/current.json"] == pointer
    assert fixed_keys(s3) == fixed
    assert_pointer_view_is("A", s3)


def test_staged_prefix_collision_preserves_pointer_and_fixed_keys():
    s3 = FakeS3()
    do_publish(s3, "A")
    orphan_key = f"{PREFIX}/cycles/{cycle_id('B')}/in-flight"
    s3.objects[orphan_key] = (b"abandoned", "application/octet-stream")
    before = dict(s3.objects)
    before_puts = list(s3.puts)

    with pytest.raises(publish.PublicationError, match="collision"):
        do_publish(s3, "B")

    assert s3.objects == before
    assert s3.puts == before_puts
    assert_pointer_view_is("A", s3)
    assert fixed_keys(s3) == {name: data for name, data, _ in payloads("A")}


def test_staging_failure_leaves_previous_cycle_live_everywhere():
    s3 = FakeS3()
    do_publish(s3, "A")

    s3.fail_when(lambda op, key: RuntimeError("stage boom")
                 if op == "put" and cycle_id("B") in key and key.endswith("/commits.parquet")
                 else None)
    with pytest.raises(publish.PublicationError):
        do_publish(s3, "B")

    assert_pointer_view_is("A", s3)
    assert fixed_keys(s3) == {name: data for name, data, _ in payloads("A")}


def test_transient_staging_put_retries_then_publishes(monkeypatch):
    s3 = FakeS3()
    do_publish(s3, "A")
    attempts = {"count": 0}

    def fail_twice(op, key):
        if op == "put" and key == f"{PREFIX}/cycles/{cycle_id('B')}/hourly.parquet":
            attempts["count"] += 1
            if attempts["count"] < 3:
                return transient_s3_error()
        return None

    sleeps = []
    monkeypatch.setattr(s3io.retry.time, "sleep", sleeps.append)
    s3.fail_when(fail_twice)

    do_publish(s3, "B")

    assert attempts["count"] == 3
    assert sleeps == [1, 2]
    assert_pointer_view_is("B", s3)


def test_ambiguous_staged_put_is_verified_without_overwrite():
    s3 = FakeS3()
    do_publish(s3, "A")
    attempts = {"n": 0}

    def lose_staged_response(op, key):
        if op == "put" and key == put_key(f"cycles/{cycle_id('B')}", "hourly.parquet"):
            attempts["n"] += 1
            return transient_s3_error()
        return None

    s3.fail_after_put_when(lose_staged_response)
    do_publish(s3, "B")

    assert attempts["n"] == 1
    assert s3.puts.count(put_key(f"cycles/{cycle_id('B')}", "hourly.parquet")) == 1
    assert_pointer_view_is("B", s3)


def test_ambiguous_fixed_key_put_retries_same_mutable_value():
    s3 = FakeS3()
    do_publish(s3, "A")
    attempts = {"n": 0}

    def lose_mirror_response(op, key):
        if op == "put" and key == f"{PREFIX}/hourly.parquet":
            attempts["n"] += 1
            if attempts["n"] == 1:
                return transient_s3_error()
        return None

    s3.fail_after_put_when(lose_mirror_response)
    do_publish(s3, "B")

    assert attempts["n"] == 2
    assert_pointer_view_is("B", s3)
    assert fixed_keys(s3) == {name: data for name, data, _ in payloads("B")}


def test_ambiguous_pointer_put_retries_same_commit():
    s3 = FakeS3()
    do_publish(s3, "A")
    attempts = {"n": 0}

    def lose_pointer_response(op, key):
        if op == "put" and key == f"{PREFIX}/current.json":
            attempts["n"] += 1
            if attempts["n"] == 1:
                return transient_s3_error()
        return None

    s3.fail_after_put_when(lose_pointer_response)
    do_publish(s3, "B")

    assert attempts["n"] == 2
    assert_pointer_view_is("B", s3)


def test_restart_after_staging_termination_keeps_previous_pointer_and_orphan_recoverable():
    s3 = FakeS3()

    do_publish(s3, "A")
    s3.fail_after_put_when(
        lambda op, key: KeyboardInterrupt()
        if op == "put"
        and key == put_key(f"cycles/{cycle_id('B')}", "hourly.parquet")
        else None
    )
    with pytest.raises(KeyboardInterrupt):
        do_publish(s3, "B")

    s3.fail_after_put_when(None)
    assert_pointer_view_is("A", s3)
    staged_key = put_key(f"cycles/{cycle_id('B')}", "hourly.parquet")
    assert s3.objects[staged_key][0] == b"hourly-B"

    do_publish(s3, "C")
    assert_pointer_view_is("C", s3)
    assert fixed_keys(s3) == {name: data for name, data, _ in payloads("C")}
    assert s3.objects[staged_key][0] == b"hourly-B", \
        "the orphaned immutable stage must not be overwritten during restart"

    # Later cycles can prune the incomplete orphan; it never blocks recovery
    # and it is never treated as a committed cycle.
    do_publish(s3, "D")
    do_publish(s3, "E")
    assert s3io.list_keys(
        s3, BUCKET, f"{PREFIX}/cycles/{cycle_id('B')}/"
    ) == []


def test_restart_after_fixed_mirror_termination_repairs_from_previous_pointer():
    s3 = FakeS3()
    do_publish(s3, "A")
    s3.fail_after_put_when(
        lambda op, key: KeyboardInterrupt()
        if op == "put" and key == f"{PREFIX}/hourly.parquet" else None
    )
    with pytest.raises(KeyboardInterrupt):
        do_publish(s3, "B")

    assert_pointer_view_is("A", s3)
    assert fixed_keys(s3)["hourly.parquet"] == b"hourly-B"
    s3.fail_after_put_when(None)

    # This is the restarted pod's first cycle. Recovery happens before it
    # stages C, so a legacy fixed-key reader is repaired from A, not from the
    # partially mirrored B keys.
    do_publish(s3, "C")
    assert_pointer_view_is("C", s3)
    assert fixed_keys(s3) == {name: data for name, data, _ in payloads("C")}


def test_restart_after_pointer_termination_keeps_previous_pointer_authoritative():
    s3 = FakeS3()
    do_publish(s3, "A")
    s3.fail_when(
        lambda op, key: KeyboardInterrupt()
        if op == "put" and key == f"{PREFIX}/current.json" else None
    )
    with pytest.raises(KeyboardInterrupt):
        do_publish(s3, "B")

    assert_pointer_view_is("A", s3)
    assert fixed_keys(s3) == {name: data for name, data, _ in payloads("B")}
    s3.fail_when(None)

    do_publish(s3, "C")
    assert_pointer_view_is("C", s3)
    assert fixed_keys(s3) == {name: data for name, data, _ in payloads("C")}


def test_restart_after_ambiguous_pointer_commit_repairs_to_committed_pointer():
    s3 = FakeS3()
    do_publish(s3, "A")
    s3.fail_after_put_when(
        lambda op, key: KeyboardInterrupt()
        if op == "put" and key == f"{PREFIX}/current.json" else None
    )
    with pytest.raises(KeyboardInterrupt):
        do_publish(s3, "B")

    # The response was lost after S3 committed. B, not the old A pointer, is
    # authoritative; restart reconciles fixed keys from B before continuing.
    assert_pointer_view_is("B", s3)
    assert fixed_keys(s3) == {name: data for name, data, _ in payloads("B")}
    s3.fail_after_put_when(None)

    do_publish(s3, "C")
    assert_pointer_view_is("C", s3)
    assert fixed_keys(s3) == {name: data for name, data, _ in payloads("C")}


def test_exhausted_transient_pointer_put_rolls_fixed_keys_back(monkeypatch):
    s3 = FakeS3()
    do_publish(s3, "A")
    attempts = {"count": 0}

    def fail_pointer(op, key):
        if op == "put" and key == f"{PREFIX}/current.json":
            attempts["count"] += 1
            return transient_s3_error()
        return None

    monkeypatch.setattr(s3io.retry.time, "sleep", lambda _seconds: None)
    s3.fail_when(fail_pointer)

    with pytest.raises(publish.PublicationError, match="pointer write failed"):
        do_publish(s3, "B")

    assert attempts["count"] == s3io.retry.MAX_ATTEMPTS
    assert_pointer_view_is("A", s3)
    assert fixed_keys(s3) == {name: data for name, data, _ in payloads("A")}


@pytest.mark.parametrize(
    "failed_name",
    ("hourly.parquet", "commits.parquet", "bead_events.parquet", "meta.json"),
)
def test_mirror_failure_rolls_fixed_keys_back(failed_name):
    s3 = FakeS3()
    do_publish(s3, "A")
    before_pointer = s3.objects[f"{PREFIX}/current.json"]

    s3.fail_when(lambda op, key: RuntimeError("mirror boom")
                 if op == "put" and key == f"{PREFIX}/{failed_name}" else None)
    with pytest.raises(publish.PublicationError) as excinfo:
        do_publish(s3, "B")

    assert "mirror" in str(excinfo.value.__cause__)
    assert_pointer_view_is("A", s3)
    assert s3.objects[f"{PREFIX}/current.json"] == before_pointer
    assert fixed_keys(s3) == {name: data for name, data, _ in payloads("A")}


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
    before_pointer = s3.objects[f"{PREFIX}/current.json"]

    s3.fail_when(lambda op, key: RuntimeError("pointer boom")
                 if op == "put" and key.endswith("/current.json") else None)
    with pytest.raises(publish.PublicationError):
        do_publish(s3, "B")

    # The mirror had already flipped the fixed keys to B; the rollback is
    # what stops the pointer and the fixed keys naming different cycles.
    assert_pointer_view_is("A", s3)
    assert s3.objects[f"{PREFIX}/current.json"] == before_pointer
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
    assert prefixes == [f"{PREFIX}/cycles/{cycle_id(t)}/" for t in ("C", "D", "E")]
    assert_pointer_view_is("E", s3)
    assert s3io.list_keys(s3, BUCKET, f"{PREFIX}/cycles/{cycle_id('A')}/") == []


def test_prune_uses_valid_id_order_and_ignores_foreign_prefixes():
    s3 = FakeS3()
    base = f"{PREFIX}/cycles/"
    current = "20260923T185500Z-00000000"
    same_second_high = "20260923T185500Z-ffffffff"
    same_second_low = "20260923T185500Z-00000001"
    older = "20260923T185459Z-ffffffff"
    foreign = "operator-note"
    for cycle in (current, same_second_high, same_second_low, older, foreign):
        s3.objects[f"{base}{cycle}/meta.json"] = (cycle.encode(), "application/json")

    publish._prune(s3, BUCKET, PREFIX, keep=current, retention=3)

    assert s3io.list_keys(s3, BUCKET, f"{base}{older}/") == []
    assert s3io.list_keys(s3, BUCKET, f"{base}{current}/")
    assert s3io.list_keys(s3, BUCKET, f"{base}{same_second_high}/")
    assert s3io.list_keys(s3, BUCKET, f"{base}{same_second_low}/")
    assert s3io.list_keys(s3, BUCKET, f"{base}{foreign}/")


def test_prune_failure_is_not_fatal(caplog):
    s3 = FakeS3()
    do_publish(s3, "A")
    do_publish(s3, "B")
    s3.fail_when(lambda op, key: RuntimeError("delete boom")
                 if op == "delete" else None)
    with caplog.at_level("WARNING", logger=publish.__name__):
        do_publish(s3, "C", retention=2)
    assert_pointer_view_is("C", s3)
    assert fixed_keys(s3) == {name: data for name, data, _ in payloads("C")}
    assert publish.prune_health() == {
        "last_outcome": "failed",
        "failures_total": 1,
        "consecutive_failures": 1,
        "last_failure_cycle_id": cycle_id("C"),
    }
    assert [record for record in caplog.records
            if "cycle prune deletion failed" in record.getMessage()]
    s3.fail_when(None)
    assert s3io.list_prefixes(s3, BUCKET, f"{PREFIX}/cycles/") == [
        f"{PREFIX}/cycles/{cycle_id(tag)}/" for tag in ("A", "B", "C")
    ]


def test_prune_listing_failure_is_not_fatal():
    s3 = FakeS3()
    do_publish(s3, "A")
    s3.fail_when(lambda op, key: RuntimeError("list boom")
                 if op == "list" and key == f"{PREFIX}/cycles/" else None)

    do_publish(s3, "B")

    assert_pointer_view_is("B", s3)
    assert fixed_keys(s3) == {name: data for name, data, _ in payloads("B")}
    assert publish.prune_health() == {
        "last_outcome": "failed",
        "failures_total": 1,
        "consecutive_failures": 1,
        "last_failure_cycle_id": cycle_id("B"),
    }
    s3.fail_when(None)
    assert s3io.list_prefixes(s3, BUCKET, f"{PREFIX}/cycles/") == [
        f"{PREFIX}/cycles/{cycle_id(tag)}/" for tag in ("A", "B")
    ]


def test_repeated_prune_failures_are_counted_and_logged(caplog):
    s3 = FakeS3()
    for tag in ("A", "B", "C"):
        do_publish(s3, tag)
    s3.fail_when(lambda op, key: RuntimeError("delete boom")
                 if op == "delete" and cycle_id("A") in key else None)

    with caplog.at_level("WARNING", logger=publish.__name__):
        do_publish(s3, "D")
        do_publish(s3, "E")

    assert publish.prune_health() == {
        "last_outcome": "failed",
        "failures_total": 2,
        "consecutive_failures": 2,
        "last_failure_cycle_id": cycle_id("E"),
    }
    warnings = [record.getMessage() for record in caplog.records
                if "cycle prune deletion failed" in record.getMessage()]
    assert len(warnings) == 2
    assert cycle_id("D") in warnings[0]
    assert cycle_id("E") in warnings[1]


def test_orphaned_staging_prefix_from_a_failed_cycle_is_swept():
    s3 = FakeS3()
    do_publish(s3, "A")
    s3.fail_when(lambda op, key: RuntimeError("stage boom")
                 if op == "put" and cycle_id("B") in key and key.endswith("/commits.parquet")
                 else None)
    with pytest.raises(publish.PublicationError):
        do_publish(s3, "B")
    s3.fail_when(None)

    do_publish(s3, "C")
    do_publish(s3, "D")  # retention 3: {B-orphan, C, D} pushes A out; B goes next
    prefixes = s3io.list_prefixes(s3, BUCKET, f"{PREFIX}/cycles/")
    assert [p.rstrip("/").rsplit("/", 1)[-1] for p in prefixes] == [
        cycle_id("B"), cycle_id("C"), cycle_id("D")
    ]
    do_publish(s3, "E")
    prefixes = s3io.list_prefixes(s3, BUCKET, f"{PREFIX}/cycles/")
    assert [p.rstrip("/").rsplit("/", 1)[-1] for p in prefixes] == [
        cycle_id("C"), cycle_id("D"), cycle_id("E")
    ]


def test_publish_rejects_identity_mismatch_before_any_write():
    s3 = FakeS3()
    with pytest.raises(publish.PublicationError, match="generated_at"):
        publish.publish_cycle(
            s3, BUCKET, PREFIX, payloads("A"),
            cycle_id="20260924T180000Z-0000000a",
            generated_at="2026-09-23T18:00:00Z",
        )
    assert s3.objects == {}


def test_duplicate_payload_names_are_rejected():
    s3 = FakeS3()
    dupe = payloads("A") + [("meta.json", b"{}", "application/json")]
    generated_at, cycle = identity("A")
    with pytest.raises(publish.PublicationError, match="duplicate"):
        publish.publish_cycle(s3, BUCKET, PREFIX, dupe, cycle_id=cycle,
                              generated_at=generated_at)
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
