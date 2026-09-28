"""Race coverage for the process-local S3 publication guard."""
import json
import threading

from tests.fake_s3 import FakeS3
from tests.test_publish import PREFIX, cycle_id, do_publish, payloads


class _BlockedPublicationS3(FakeS3):
    """Pause publisher A at one mutation so publisher B gets a race window."""

    def __init__(self, phase):
        super().__init__()
        self.phase = phase
        self.first_phase_started = threading.Event()
        self.second_phase_started = threading.Event()
        self.release_first = threading.Event()
        self.trace = []
        self._trace_lock = threading.Lock()
        self._first_blocked = False

    def put_object(self, Bucket, Key, Body, ContentType, CacheControl=None):
        thread_name = threading.current_thread().name
        with self._trace_lock:
            self.trace.append((thread_name, Key))

        fixed_hourly = f"{PREFIX}/hourly.parquet"
        pointer = f"{PREFIX}/current.json"
        target = fixed_hourly if self.phase == "fixed" else pointer
        if Key == target and thread_name == "publisher-a" and not self._first_blocked:
            self._first_blocked = True
            self.first_phase_started.set()
            if not self.release_first.wait(timeout=5):
                raise AssertionError("test did not release publisher A")
        elif Key == target and thread_name == "publisher-b":
            self.second_phase_started.set()

        super().put_object(Bucket, Key, Body, ContentType, CacheControl)


def _run_race(phase):
    s3 = _BlockedPublicationS3(phase)
    errors = []
    publisher_a_started = threading.Event()
    publisher_b_started = threading.Event()

    def run(tag, started):
        started.set()
        try:
            do_publish(s3, tag)
        except BaseException as error:  # report worker failures in the test thread
            errors.append((tag, error))

    publisher_a = threading.Thread(
        target=run, args=("A", publisher_a_started), name="publisher-a"
    )
    publisher_b = threading.Thread(
        target=run, args=("B", publisher_b_started), name="publisher-b"
    )
    publisher_a.start()
    first_started = s3.first_phase_started.wait(timeout=2)
    publisher_b.start()
    second_started = s3.second_phase_started.wait(timeout=0.5)

    # Always release the blocked writer before asserting so a regression cannot
    # leave a worker thread hanging after a failed assertion.
    s3.release_first.set()
    publisher_a.join(timeout=5)
    publisher_b.join(timeout=5)

    assert publisher_a_started.is_set()
    assert publisher_b_started.is_set()
    assert first_started, f"publisher A never reached the {phase} phase"
    assert not second_started, f"publisher B entered the {phase} phase too soon"
    assert not publisher_a.is_alive()
    assert not publisher_b.is_alive()
    assert errors == []

    # The complete publication, including staging, fixed-key mirroring,
    # current.json, and pruning, belongs to one thread before the other starts.
    trace_threads = [thread_name for thread_name, _ in s3.trace]
    seen_b = False
    for thread_name in trace_threads:
        if thread_name == "publisher-b":
            seen_b = True
        assert thread_name == ("publisher-b" if seen_b else "publisher-a")
    assert json.loads(s3.objects[f"{PREFIX}/current.json"][0])["cycle_id"] == cycle_id("B")
    for name, data, _ in payloads("B"):
        assert s3.objects[f"{PREFIX}/{name}"][0] == data


def test_concurrent_publishers_do_not_interleave_fixed_key_mirroring():
    _run_race("fixed")


def test_concurrent_publishers_do_not_interleave_current_json_commits():
    _run_race("pointer")
