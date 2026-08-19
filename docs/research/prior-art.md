# Prior art

## Sibling exporters in this environment

`cluster-status-exporter` and `argo-workflows-exporter` established the shape
this repo follows: a Python poll loop in a Deployment (never a CronJob —
ArgoCD cannot manage those idempotently), writing Parquet to a prefix under
the `dashboard-site` Garage bucket, consumed by a static page that reads it
client-side with hyparquet. `argo-workflows-exporter` also established the
ledger pattern for data that outlives its source object.

## GitHub's punchcard

The canonical "commit matrix" is GitHub's hour × weekday punchcard. It
assumes human working rhythms, which is exactly the assumption that fails
here: an agent fleet commits nearly uniformly across the 24 hours (1.94x
spread) while being violently bursty in absolute time (Fano 22). The
punchcard is retained only as a secondary tile that proves the absence of a
circadian pattern.

## Burstiness measures

The Fano factor (variance/mean of counts per fixed bucket) is used throughout
rather than a Gini coefficient or the Goh–Barabási burstiness parameter. It
is the cheapest measure that answers the actual question — "is this more
clustered than random arrival?" — with an interpretable null: Fano = 1 is a
Poisson process.
