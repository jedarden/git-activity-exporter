"""Atomic publication of a cycle's objects.

S3 offers exactly one atomic primitive: a single-object PUT, where every
reader sees all of the old bytes or all of the new ones. It has no
multi-object transaction, and the pre-protocol behavior overwrote the four
objects in place one after another -- so a failure after the first upload
left the prefix holding a new hourly.parquet beside the previous cycle's
commits.parquet: a dataset no cycle ever produced, whose internal joins are
silently wrong, replacing a dataset that was complete.

The protocol turns the single-object primitive into a whole-cycle commit:

  1. Generate every payload before any S3 write (done by the caller). A
     generation failure raises with nothing uploaded anywhere.
  2. Stage the payloads under ``cycles/<cycle_id>/`` -- a prefix no consumer
     reads until it is pointed at. Staged PUTs are write-once: an ambiguous
     response is resolved with a GET and accepted only for an exact match;
     an existing cycle prefix is never overwritten. An upload failure here
     aborts with the previous cycle still live everywhere; the orphaned
     prefix is inert and swept by a later cycle's prune.
  3. Recover before mirroring. On every cycle boundary, read
     ``current.json`` and fetch all immutable objects it names. If the fixed
     keys do not exactly match that complete cycle, rebuild them from this
     snapshot, with meta.json last. This is the durable rollback source: it
     survives pod termination, unlike an in-memory snapshot.
  4. Mirror the staged payloads to the fixed keys, meta.json always last,
     for consumers that have not moved to the pointer (the static panel
     reads hourly.parquet and meta.json directly). A process death can leave
     these legacy keys mixed temporarily; the next cycle repairs them from
     the pointer before staging anything else. Pointer-resolved readers are
     unaffected because they never read these keys.
  5. Commit: PUT ``current.json`` naming the cycle and its object keys.
     This one PUT is the only instant at which the pointer-resolved dataset
     changes. If its response is ambiguous, a GET determines whether the
     new pointer committed; if it did not, recovery restores fixed keys from
     the previous pointer. If the pod dies before that GET, restart follows
     whichever pointer S3 durably contains. The staged objects it names were
     completed in step 2 and are never rewritten, so a pointer-first reader
     always assembles exactly one whole cycle.
  6. Prune: keep the committed cycle plus the newest RETAINED_CYCLES-1
     cycle prefixes, delete the rest. Discovery and deletion failures are
     logged with the committed and failed cycle IDs, recorded in process-local
     health state, and never fatal -- a leftover old cycle costs a few MiB, a
     failed publication costs the protocol.

Fixed-key consumers keep one residual race the pointer removes: a reader
landing between step 4's PUTs can interleave, exactly as it always could.
That is the pre-protocol behavior preserved, not a regression; the pointer
is the migration target and the only read path with an absolute guarantee
(docs/notes/output-schema.md, "Publication protocol").

Single-writer by deployment: one exporter replica publishes this prefix.
"""
import json
import logging
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime

from . import s3io

log = logging.getLogger(__name__)

#: The pointer object, relative to the destination prefix. One atomic PUT of
#: this file is the protocol's commit point.
POINTER_NAME = "current.json"

_GENERATED_AT_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_CYCLE_ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")
_GENERATED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_CYCLE_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"

#: Pointer document shape; bump when the schema changes.
POINTER_SCHEMA_VERSION = 1

#: Committed cycle prefixes kept behind the pointer's cycle. Three cycles is
#: roughly three poll intervals of grace for a consumer that resolved the
#: previous pointer and is still reading, at a few MiB per generation.
RETAINED_CYCLES = 3

META_NAME = "meta.json"

# The production payload set is stable and is also needed to clean a partial
# first publication when no pointer exists yet. Direct callers may supply a
# different set; recovery takes the union with the pointer's names.
DEFAULT_FIXED_NAMES = (
    "hourly.parquet",
    "commits.parquet",
    "bead_events.parquet",
    META_NAME,
)


class PublicationError(RuntimeError):
    """A cycle could not be published; the previous cycle remains live."""


@dataclass(frozen=True)
class PruneReport:
    """The best-effort cleanup result for one committed cycle."""

    outcome: str
    failed_cycle_ids: tuple = ()


_prune_state_lock = threading.Lock()
_prune_state = {
    "last_outcome": None,
    "failures_total": 0,
    "consecutive_failures": 0,
    "last_failure_cycle_id": None,
}


def reset_prune_health():
    """Reset process-local cleanup observability, primarily for test setup."""
    with _prune_state_lock:
        _prune_state.update(
            last_outcome=None,
            failures_total=0,
            consecutive_failures=0,
            last_failure_cycle_id=None,
        )


def prune_health() -> dict:
    """Return the process-local cleanup state for the health endpoint.

    Pruning happens after the pointer commit, so this state is intentionally
    process-local rather than part of the immutable cycle metadata.
    """
    with _prune_state_lock:
        return dict(_prune_state)


def _record_prune_health(keep: str, report: PruneReport):
    failed = report.outcome == "failed"
    with _prune_state_lock:
        _prune_state["last_outcome"] = report.outcome
        if failed:
            _prune_state["failures_total"] += 1
            _prune_state["consecutive_failures"] += 1
            _prune_state["last_failure_cycle_id"] = keep
        else:
            _prune_state["consecutive_failures"] = 0


def _validate_generated_at(generated_at: str) -> None:
    if not isinstance(generated_at, str) or not _GENERATED_AT_RE.fullmatch(generated_at):
        raise PublicationError("generated_at must be YYYY-MM-DDTHH:MM:SSZ")
    try:
        parsed = datetime.strptime(generated_at, _GENERATED_AT_FORMAT)
    except ValueError as e:
        raise PublicationError("generated_at must be a valid UTC calendar timestamp") from e
    if parsed.isoformat(timespec="seconds") + "Z" != generated_at:
        raise PublicationError("generated_at must be a valid UTC calendar timestamp")


def _compact_generated_at(generated_at: str) -> str:
    _validate_generated_at(generated_at)
    return generated_at.replace("-", "").replace(":", "")


def _validate_cycle_stamp(stamp: str) -> None:
    try:
        parsed = datetime.strptime(stamp, _CYCLE_STAMP_FORMAT)
    except ValueError as e:
        raise PublicationError("cycle_id must contain a valid UTC calendar timestamp") from e
    if parsed.isoformat(timespec="seconds").replace("-", "").replace(":", "") + "Z" != stamp:
        raise PublicationError("cycle_id must contain a valid UTC calendar timestamp")


def validate_cycle_id(cycle_id: str, generated_at: str) -> None:
    """Validate the ID grammar and its exact generated_at relationship."""
    expected_stamp = _compact_generated_at(generated_at)
    if not isinstance(cycle_id, str) or not _CYCLE_ID_RE.fullmatch(cycle_id):
        raise PublicationError("cycle_id must be <compacted generated_at>-<8 lowercase hex>")
    stamp = cycle_id.split("-", 1)[0]
    _validate_cycle_stamp(stamp)
    if stamp != expected_stamp:
        raise PublicationError("cycle_id must name the cycle's generated_at")


def _is_valid_cycle_id(cycle_id: str) -> bool:
    if not isinstance(cycle_id, str) or not _CYCLE_ID_RE.fullmatch(cycle_id):
        return False
    try:
        _validate_cycle_stamp(cycle_id.split("-", 1)[0])
    except PublicationError:
        return False
    return True


def new_cycle_id(generated_at: str) -> str:
    """Mint a fixed-width sortable ID for a canonical generated_at value."""
    stamp = _compact_generated_at(generated_at)
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _meta_last(payloads):
    """The fixed-key mirror order is the caller's list order with meta.json
    forced last: meta is the legacy keys' completion marker, and publishing
    it before the data would let a reader see freshness for data that is
    not there yet."""
    payloads = list(payloads)
    meta = [p for p in payloads if p[0] == META_NAME]
    if len(meta) != 1:
        raise PublicationError(f"expected exactly one {META_NAME} payload, got {len(meta)}")
    rest = [p for p in payloads if p[0] != META_NAME]
    return rest + meta


def pointer_bytes(cycle_id: str, generated_at: str, payload_names) -> bytes:
    validate_cycle_id(cycle_id, generated_at)
    doc = {
        "schema_version": POINTER_SCHEMA_VERSION,
        "cycle_id": cycle_id,
        "generated_at": generated_at,
        # Relative to the destination prefix, so a consumer joins them onto
        # whatever base path it fetched the pointer from.
        "objects": {name: f"cycles/{cycle_id}/{name}" for name in payload_names},
    }
    return json.dumps(doc, indent=2).encode()


def _read_pointer(s3, bucket: str, key: str):
    raw = s3io.download_bytes(s3, bucket, key)
    if raw is None:
        return None
    try:
        doc = json.loads(raw)
        if not isinstance(doc, dict) or doc.get("schema_version") != POINTER_SCHEMA_VERSION:
            raise ValueError("unsupported pointer schema")
        cycle_id = doc["cycle_id"]
        generated_at = doc["generated_at"]
        validate_cycle_id(cycle_id, generated_at)
        objects = doc["objects"]
        if not isinstance(objects, dict) or not objects:
            raise ValueError("pointer objects must be a non-empty object")
        for name, object_key in objects.items():
            if not isinstance(name, str) or not name or "/" in name:
                raise ValueError("pointer object names must be simple names")
            if object_key != f"cycles/{cycle_id}/{name}":
                raise ValueError(f"pointer object path is not immutable: {name}")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
        raise PublicationError(f"invalid publication pointer {key}") from e
    return doc


def _ordered_names(names):
    names = list(dict.fromkeys(names))
    if META_NAME in names:
        names.remove(META_NAME)
        names.append(META_NAME)
    return names


def recover_publication(s3, bucket: str, prefix: str, expected_names=None):
    """Reconcile fixed keys to the cycle named by the durable pointer.

    A pod can die after any fixed-key PUT, so fixed keys are not recovery
    input. On restart, fetch every immutable object named by ``current.json``
    first and repair the root mirror from that complete snapshot. If no
    pointer exists, the fixed names are removed because there is no committed
    dataset that could make them authoritative.

    The function is idempotent and writes nothing when the fixed keys already
    match the authoritative cycle.
    """
    pointer_key = f"{prefix}/{POINTER_NAME}"
    pointer = _read_pointer(s3, bucket, pointer_key)
    expected_names = tuple(expected_names or ())
    names = list(pointer["objects"] if pointer else ())
    names.extend(name for name in expected_names if name not in names)
    names = _ordered_names(names)
    if not names:
        return pointer["cycle_id"] if pointer else None

    desired = {name: None for name in names}
    if pointer:
        for name, object_key in pointer["objects"].items():
            desired[name] = s3io.fetch_object(s3, bucket, f"{prefix}/{object_key}")
            if desired[name] is None:
                raise PublicationError(
                    f"pointer {pointer['cycle_id']} names missing object {object_key}"
                )

    current = {
        name: s3io.fetch_object(s3, bucket, f"{prefix}/{name}")
        for name in names
    }
    if current == desired:
        return pointer["cycle_id"] if pointer else None

    # Copy data first and the legacy meta marker last. A process death during
    # this repair is safe because the next restart repeats it from the
    # pointer, never from partially repaired fixed keys.
    for name in _ordered_names(name for name in names if desired[name] is not None):
        data, content_type = desired[name]
        s3io.upload_bytes(
            s3, bucket, f"{prefix}/{name}", data,
            content_type or "application/octet-stream",
        )
    for name in _ordered_names(name for name in names if desired[name] is None):
        if current[name] is not None:
            s3io.delete_key(s3, bucket, f"{prefix}/{name}")
    log.info(
        "reconciled fixed publication keys to cycle %s",
        pointer["cycle_id"] if pointer else "none",
    )
    return pointer["cycle_id"] if pointer else None


def publish_cycle(s3, bucket: str, prefix: str, payloads, cycle_id: str,
                  generated_at: str, retention: int = RETAINED_CYCLES) -> str:
    """Publish one cycle. Returns the pointer key.

    ``payloads`` is [(name, bytes, content_type), ...]; the names become
    both the staged object names and the fixed-key names. On a handled
    failure the exception chains the underlying cause and recovery restores
    the fixed keys from the pointer. If the pod terminates between S3 calls,
    the next cycle performs the same recovery before publishing.
    """
    names = [name for name, _, _ in payloads]
    if len(set(names)) != len(names):
        raise PublicationError(f"duplicate payload names: {names}")
    validate_cycle_id(cycle_id, generated_at)

    try:
        recover_publication(s3, bucket, prefix, expected_names=names)
    except Exception as e:
        raise PublicationError(
            f"publication recovery failed before staging {cycle_id}"
        ) from e

    pointer_key = f"{prefix}/{POINTER_NAME}"
    base = f"{prefix}/cycles/{cycle_id}/"

    try:
        collision = s3io.prefix_exists(s3, bucket, base)
    except Exception as e:
        raise PublicationError(
            f"cycle collision check failed for {cycle_id}; previous cycle untouched"
        ) from e
    if collision:
        raise PublicationError(f"cycle_id collision: {cycle_id} already exists")

    # Step 2: stage. Nothing below can touch the committed cycle, so a
    # failure here needs no rollback anywhere.
    try:
        for name, data, content_type in payloads:
            s3io.upload_immutable_bytes(
                s3, bucket, f"{base}{name}", data, content_type
            )
    except Exception as e:
        raise PublicationError(f"staging {base} failed; previous cycle untouched") from e

    # Step 4: mirror to the fixed keys, meta.json last.
    try:
        for name, data, content_type in _meta_last(payloads):
            s3io.upload_bytes(s3, bucket, f"{prefix}/{name}", data, content_type)
    except Exception as e:
        _recover_after_failure(s3, bucket, prefix, "fixed-key mirror")
        raise PublicationError(
            "fixed-key mirror failed; fixed keys restored to the previous cycle"
        ) from e

    # Step 5: the commit. One atomic PUT.
    committed_pointer = pointer_bytes(cycle_id, generated_at, names)
    try:
        s3io.upload_bytes(
            s3, bucket, pointer_key,
            committed_pointer, "application/json",
        )
    except Exception as e:
        # A PUT can have committed before its response was lost. Resolve the
        # outcome from S3 before deciding that the previous pointer won.
        try:
            if s3io.download_bytes(s3, bucket, pointer_key) == committed_pointer:
                report = _prune(s3, bucket, prefix, keep=cycle_id, retention=retention)
                _record_prune_health(cycle_id, report)
                return pointer_key
        except Exception:
            log.exception("could not resolve ambiguous pointer PUT for %s", cycle_id)
        _recover_after_failure(s3, bucket, prefix, "pointer commit")
        raise PublicationError(
            "pointer write failed; fixed keys restored to the previous cycle"
        ) from e

    report = _prune(s3, bucket, prefix, keep=cycle_id, retention=retention)
    _record_prune_health(cycle_id, report)
    return pointer_key


def _recover_after_failure(s3, bucket, prefix: str, phase: str):
    """Restore from the pointer without masking the phase's original error."""
    try:
        recover_publication(s3, bucket, prefix, expected_names=DEFAULT_FIXED_NAMES)
    except Exception:
        log.exception(
            "recovery after %s failed; fixed keys may remain mixed until restart",
            phase,
        )


def _prune(s3, bucket: str, prefix: str, keep: str, retention: int):
    """Delete the oldest valid cycle prefixes beyond the retention window.

    The pointer's cycle is protected explicitly, not just by sort position:
    an operator-shrunk retention or a clock surprise must not be able to
    delete the cycle consumers are being pointed at. Foreign children are
    ignored rather than being allowed to consume a retention slot.
    """
    base = f"{prefix}/cycles/"
    try:
        ids = sorted(
            (
                cycle_id
                for cycle_id in (
                    p[len(base):].rstrip("/") for p in s3io.list_prefixes(s3, bucket, base)
                )
                if _is_valid_cycle_id(cycle_id)
            ),
            reverse=True,
        )
    except Exception:
        log.warning(
            "cycle prune discovery failed for keep=%s; cleanup deferred",
            keep,
            exc_info=True,
        )
        return PruneReport("failed", (keep,))
    doomed = [cid for cid in ids if cid != keep][max(0, retention - 1):]
    failed = []
    for cid in doomed:
        try:
            for key in s3io.list_keys(s3, bucket, f"{base}{cid}/"):
                s3io.delete_key(s3, bucket, key)
            log.info("pruned cycle %s", cid)
        except Exception:
            failed.append(cid)
            log.warning(
                "cycle prune deletion failed for keep=%s cycle=%s; cleanup deferred",
                keep,
                cid,
                exc_info=True,
            )
    return PruneReport("failed" if failed else "succeeded", tuple(failed))
