"""Behavioral pins for configuration.md's "Poll-cycle lifecycle" section.

Each rule the section states about main()'s loop is exercised here against
the real loop code -- main.main() runs unmodified, with only its boundaries
(config, forge, S3, health socket, signal registration) stubbed: the first
poll precedes the first sleep, the interval is a fixed post-cycle sleep
identical after success and failure, cycles are serialized on one thread, and
a stop signal never aborts the cycle in progress.
"""

import logging
import threading
from types import SimpleNamespace

import pytest

from src import main


@pytest.fixture(autouse=True)
def reset_published():
    main._published.clear()
    yield
    main._published.clear()


class ScriptedStop:
    """Stands in for the loop's stop event. A shutdown signal arrives from
    inside a cycle -- that is where the handler fires -- so a cycle stub must
    be able to reach the live instance; `instances` is that handle."""

    instances = []
    log = []
    stop_after_waits = 1

    def __init__(self):
        self.stopped = False
        self.waits = 0
        ScriptedStop.instances.append(self)

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, timeout):
        self.waits += 1
        ScriptedStop.log.append(("wait", timeout))
        if self.waits >= ScriptedStop.stop_after_waits:
            self.stopped = True
        return self.stopped


def _drive(monkeypatch, outcomes, on_cycle=None, poll_interval_seconds=3600):
    """Run main.main() over `outcomes`, one per cycle attempt (an exception
    fails that cycle, None succeeds it). The loop stops after len(outcomes)
    interval sleeps. Returns the ordered health/cycle/wait event log, where
    the first entry is always the health server binding."""
    events = []
    ScriptedStop.instances = []
    ScriptedStop.log = events
    ScriptedStop.stop_after_waits = len(outcomes)
    pending = iter(outcomes)

    def run_cycle(*_args):
        n = sum(1 for kind, *_ in events if kind == "cycle") + 1
        events.append(("cycle", n))
        if on_cycle is not None:
            on_cycle(n)
        outcome = next(pending)
        if outcome is not None:
            events.append(("cycle-failed", n))
            raise outcome
        events.append(("cycle-ok", n))

    cfg = SimpleNamespace(
        log_level=logging.CRITICAL,
        families_file="families.yaml",
        dest=object(),
        health_port=0,
        poll_interval_seconds=poll_interval_seconds,
    )
    monkeypatch.setattr(main.config, "load", lambda: cfg)
    monkeypatch.setattr(main.families, "load", lambda _path: {})
    monkeypatch.setattr(main.s3io, "client", lambda _dest: object())
    monkeypatch.setattr(main.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(
        main, "_serve_health", lambda port: events.append(("health", port))
    )
    monkeypatch.setattr(main, "_run_cycle", run_cycle)
    monkeypatch.setattr(main, "threading", SimpleNamespace(Event=ScriptedStop))

    main.main()
    return events


def _kinds(events):
    return [kind for kind, *_ in events]


def test_first_poll_starts_immediately_after_the_health_server_binds(monkeypatch):
    events = _drive(monkeypatch, [None, None])

    # The loop shape is health -> cycle -> sleep -> cycle: no sleep ever
    # precedes the first cycle, so a fresh deployment's first publication is
    # one cycle-duration away, not interval + cycle-duration.
    assert _kinds(events) == [
        "health", "cycle", "cycle-ok", "wait", "cycle", "cycle-ok", "wait",
    ]
    assert main._published.is_set()


def test_interval_is_a_fixed_post_cycle_sleep_identical_after_failure(monkeypatch):
    events = _drive(
        monkeypatch,
        [RuntimeError("forge unreachable"), None],
        poll_interval_seconds=1500,
    )

    # The failed attempt waited the same full configured interval as the
    # successful one -- the next poll is the retry, with no backoff and no
    # shortened catch-up sleep.
    assert _kinds(events) == [
        "health", "cycle", "cycle-failed", "wait", "cycle", "cycle-ok", "wait",
    ]
    assert [value for kind, value in events if kind == "wait"] == [1500, 1500]
    assert main._published.is_set(), "the successful retry still flips readiness"


def test_cycles_are_serialized_on_one_thread_and_never_overlap(monkeypatch):
    threads = []
    events = _drive(
        monkeypatch, [None, None, None], on_cycle=lambda _n: threads.append(threading.get_ident())
    )

    assert set(threads) == {threads[0]}, "every cycle ran on the loop's own thread"
    # Strict alternation: a cycle only ever follows the previous attempt's
    # full interval sleep, so an overrun cycle delays its successor instead
    # of running concurrently with it.
    assert _kinds(events) == [
        "health", "cycle", "cycle-ok", "wait", "cycle", "cycle-ok", "wait",
        "cycle", "cycle-ok", "wait",
    ]


def test_stop_signal_during_a_cycle_lets_it_complete_and_publish(monkeypatch):
    def signal_mid_cycle(n):
        # Where the SIGTERM handler would fire: mid-cycle, not between cycles.
        if n == 2:
            ScriptedStop.instances[-1].set()

    events = _drive(monkeypatch, [None, None], on_cycle=signal_mid_cycle)

    assert ("cycle-ok", 2) in events, "the interrupted-by-signal cycle ran to completion"
    assert main._published.is_set(), "that completion still counts as a publication"
    assert _kinds(events) == ["health", "cycle", "cycle-ok", "wait", "cycle", "cycle-ok", "wait"], \
        "the process exits after the cycle finishes, with no third cycle and no trailing interval"


def test_stop_signal_during_the_sleep_exits_without_another_cycle(monkeypatch):
    events = _drive(monkeypatch, [None])

    assert _kinds(events) == ["health", "cycle", "cycle-ok", "wait"]
    assert main._published.is_set()


def test_a_restarted_process_repolls_immediately_and_starts_unready(monkeypatch):
    # What a restart must not do: skip polling because S3 already holds a
    # publication, or come up ready. The readiness latch is process-local, so
    # a first attempt that fails leaves the new process unready until a later
    # cycle of its own succeeds.
    events = _drive(monkeypatch, [RuntimeError("first cycle after restart failed")])

    assert _kinds(events) == ["health", "cycle", "cycle-failed", "wait"]
    assert not main._published.is_set()
