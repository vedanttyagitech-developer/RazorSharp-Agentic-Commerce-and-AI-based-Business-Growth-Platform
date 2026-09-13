from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from commerce_domain.workload import WorkloadExceededError, WorkloadGate


def test_concurrent_admission_and_exception_release():
    gate = WorkloadGate()
    barrier = Barrier(2)
    release = Event()

    def hold():
        with gate.admit([("tenant", 20, 1)]):
            barrier.wait(timeout=5)
            release.wait(timeout=5)
            raise RuntimeError("provider failed")

    with ThreadPoolExecutor() as pool:
        running = pool.submit(hold)
        barrier.wait(timeout=5)
        try:
            with pytest.raises(WorkloadExceededError), gate.admit([("tenant", 20, 1)]):
                pass
        finally:
            release.set()
        with pytest.raises(RuntimeError):
            running.result()
    with gate.admit([("tenant", 20, 1)]):
        pass


def test_rejections_do_not_charge_other_dimensions_and_windows_expire():
    now = [0.0]
    gate = WorkloadGate(clock=lambda: now[0], max_keys=2)
    with gate.admit([("user", 1, 1)]):
        pass
    for _ in range(10):
        with pytest.raises(WorkloadExceededError), gate.admit([("global", 1, 1), ("user", 1, 1)]):
            pass
    with gate.admit([("global", 1, 1)]):
        pass
    with pytest.raises(WorkloadExceededError), gate.admit([("new", 1, 1)]):
        pass
    now[0] = 60
    with gate.admit([("new", 1, 1)]):
        pass
