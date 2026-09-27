# Git activity output-contract fixture v1

This directory is the versioned, small cross-repository fixture for the
`git-activity-exporter` output contract. It is intentionally a complete
published view rather than a collection of isolated rows:

- `current.json` is the atomic pointer. Its relative object names resolve
  below this directory, just as they resolve below the S3 prefix in
  production.
- `cycles/<cycle_id>/` is the immutable pointer-resolved view.
- The four files at the fixture root are the legacy fixed-key mirror. They are
  byte-identical to the objects named by the pointer.
- `ledger/joins.json` contains attempts and expected matches for the
  documented `(workspace_uuid, issue_id, actor, time window)` bead-event join
  and `(repo, sha)` commit bridge. The redispatch case deliberately has a
  `system` close: the worker must come from the in-window claim, not the close.
- `manifest.json` records the fixture version and the complete Parquet column
  contract so non-Python consumers can validate the fixture without importing
  exporter code.

The fixture is consumed by `tests/test_output_contract_fixture.py`. Keep this
directory backwards-compatible: a schema change gets a new `vN` directory;
do not rewrite v1 to make a changed exporter pass.

The dashboard-site panel and the declarative-config factory-ledger sync are
separate repositories. Their consumer-side beads should copy or fetch this
fixture at the pinned exporter commit and run their own native reader against
the pointer-resolved files. This repository test is the producer-side gate
that makes the handoff deterministic and auditable.
