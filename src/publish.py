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
     reads until it is pointed at. An upload failure here aborts with the
     previous cycle still live everywhere; the orphaned prefix is inert and
     swept by a later cycle's prune.
  3. Snapshot the fixed keys in memory -- they are about to be overwritten,
     and a later step may have to put them back.
  4. Mirror the staged payloads to the fixed keys, meta.json always last,
     for consumers that have not moved to the pointer (the static panel
     reads hourly.parquet and meta.json directly). A failure here rolls the
     fixed keys back to the step-3 snapshot and re-raises, so a failed
     cycle never leaves a half-mirrored set behind.
  5. Commit: PUT ``current.json`` naming the cycle and its object keys.
     This one PUT is the only instant at which the published dataset
     changes. The objects it names were completed in step 2 and are never
     rewritten -- a cycle's payload is immutable once staged -- so a
     consumer that reads the pointer and then the objects it names always
     assembles exactly one whole cycle, whichever cycle that was. A pointer
     failure rolls the fixed keys back too (undoing step 4's mirror) and
     re-raises: after a failed cycle, the pointer and the fixed keys must
     not name different cycles.
  6. Prune: keep the committed cycle plus the newest RETAINED_CYCLES-1
     cycle prefixes, delete the rest. Deletion failures are logged, never
     fatal -- a leftover old cycle costs a few MiB, a failed publication
     costs the protocol.

Fixed-key consumers keep one residual race the pointer removes: a reader
landing between step 4's PUTs can interleave, exactly as it always could.
That is the pre-protocol behavior preserved, not a regression; the pointer
is the migration target and the only read path with an absolute guarantee
(docs/notes/output-schema.md, "Publication protocol").

Single-writer by deployment: one exporter replica publishes this prefix.
"""
import json
import logging
import uuid

from . import s3io

log = logging.getLogger(__name__)

#: The pointer object, relative to the destination prefix. One atomic PUT of
#: this file is the protocol's commit point.
POINTER_NAME = "current.json"

#: Pointer document shape; bump when the schema changes.
POINTER_SCHEMA_VERSION = 1

#: Committed cycle prefixes kept behind the pointer's cycle. Three cycles is
#: roughly three poll intervals of grace for a consumer that resolved the
#: previous pointer and is still reading, at a few MiB per generation.
RETAINED_CYCLES = 3

META_NAME = "meta.json"


class PublicationError(RuntimeError):
    """A cycle could not be published; the previous cycle remains live."""


def new_cycle_id(generated_at: str) -> str:
    """Sortable id from the cycle's generated_at plus a short unique suffix.

    The fixed-width timestamp prefix is what makes the prune's lexicographic
    sort a recency order; the suffix keeps two cycles in the same second
    distinct.
    """
    stamp = generated_at.replace("-", "").replace(":", "")
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
    doc = {
        "schema_version": POINTER_SCHEMA_VERSION,
        "cycle_id": cycle_id,
        "generated_at": generated_at,
        # Relative to the destination prefix, so a consumer joins them onto
        # whatever base path it fetched the pointer from.
        "objects": {name: f"cycles/{cycle_id}/{name}" for name in payload_names},
    }
    return json.dumps(doc, indent=2).encode()


def publish_cycle(s3, bucket: str, prefix: str, payloads, cycle_id: str,
                  generated_at: str, retention: int = RETAINED_CYCLES) -> str:
    """Publish one cycle. Returns the pointer key.

    ``payloads`` is [(name, bytes, content_type), ...]; the names become
    both the staged object names and the fixed-key names. On any failure the
    exception chains the underlying cause, and the guarantee holds: the
    previously committed cycle is still what the pointer names, and the
    fixed keys hold it too.
    """
    names = [name for name, _, _ in payloads]
    if len(set(names)) != len(names):
        raise PublicationError(f"duplicate payload names: {names}")

    pointer_key = f"{prefix}/{POINTER_NAME}"
    base = f"{prefix}/cycles/{cycle_id}/"

    # Step 2: stage. Nothing below can touch the committed cycle, so a
    # failure here needs no rollback anywhere.
    try:
        for name, data, content_type in payloads:
            s3io.upload_bytes(s3, bucket, f"{base}{name}", data, content_type)
    except Exception as e:
        raise PublicationError(f"staging {base} failed; previous cycle untouched") from e

    # Step 3: snapshot the fixed keys we are about to overwrite.
    snapshot = {name: s3io.fetch_object(s3, bucket, f"{prefix}/{name}") for name in names}

    # Step 4: mirror to the fixed keys, meta.json last.
    try:
        for name, data, content_type in _meta_last(payloads):
            s3io.upload_bytes(s3, bucket, f"{prefix}/{name}", data, content_type)
    except Exception as e:
        _restore_fixed(s3, bucket, prefix, snapshot)
        raise PublicationError(
            "fixed-key mirror failed; fixed keys restored to the previous cycle"
        ) from e

    # Step 5: the commit. One atomic PUT.
    try:
        s3io.upload_bytes(
            s3, bucket, pointer_key,
            pointer_bytes(cycle_id, generated_at, names), "application/json",
        )
    except Exception as e:
        # The mirror already put the new cycle on the fixed keys; putting it
        # back to the snapshot is what keeps both views on one cycle.
        _restore_fixed(s3, bucket, prefix, snapshot)
        raise PublicationError(
            "pointer write failed; fixed keys restored to the previous cycle"
        ) from e

    _prune(s3, bucket, prefix, keep=cycle_id, retention=retention)
    return pointer_key


def _restore_fixed(s3, bucket: str, prefix: str, snapshot: dict):
    """Best-effort rollback of the fixed keys to their snapshotted state.

    A key that had no object before gets deleted again. A rollback failure
    is logged and leaves the next publish (or an operator) to clean up --
    losing the original failure by raising from here would misreport why
    the cycle failed.
    """
    for name, snap in snapshot.items():
        key = f"{prefix}/{name}"
        try:
            if snap is None:
                s3io.delete_key(s3, bucket, key)
            else:
                data, content_type = snap
                s3io.upload_bytes(s3, bucket, key, data, content_type or "application/octet-stream")
        except Exception:
            log.exception("rollback of %s failed; the fixed keys may hold the aborted cycle", key)


def _prune(s3, bucket: str, prefix: str, keep: str, retention: int):
    """Delete the oldest cycle prefixes beyond the retention window.

    The pointer's cycle is protected explicitly, not just by sort position:
    an operator-shrunk retention or a clock surprise must not be able to
    delete the cycle consumers are being pointed at.
    """
    base = f"{prefix}/cycles/"
    ids = sorted(
        (p[len(base):].rstrip("/") for p in s3io.list_prefixes(s3, bucket, base)),
        reverse=True,
    )
    doomed = [cid for cid in ids if cid != keep][max(0, retention - 1):]
    for cid in doomed:
        try:
            for key in s3io.list_keys(s3, bucket, f"{base}{cid}/"):
                s3io.delete_key(s3, bucket, key)
            log.info("pruned cycle %s", cid)
        except Exception:
            log.warning("could not prune cycle %s; leaving it for the next cycle", cid)
