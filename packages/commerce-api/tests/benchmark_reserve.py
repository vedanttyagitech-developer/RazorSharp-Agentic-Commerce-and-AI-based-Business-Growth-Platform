"""Opt-in local admission benchmark. Never executes a provider payment."""

import json
import os
import platform
import resource
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_capi_reserve import _card, _headers, pay

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(not os.environ.get("RESERVE_BENCHMARK_OUTPUT"), reason="opt-in benchmark"),
]


@pytest.mark.parametrize("concurrency", [1, 4, 8])
def test_bounded_capacity_benchmark(auth_client, concurrency):
    cards = [_card(auth_client) for _ in range(16)]
    amount = cards[0]["amount_minor"]
    response = auth_client.post(
        "/v1/reserve/authorities",
        headers=_headers(),
        json={
            "per_purchase_limit_minor": amount,
            "capacity_minor": amount,
        },
    )
    assert response.status_code == 200, response.text
    authority = response.json()

    def request(card):
        started = time.perf_counter()
        response = pay(auth_client, card, authority)
        return time.perf_counter() - started, response

    cpu_start = time.process_time()
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(request, cards))
    elapsed = time.perf_counter() - start
    latencies = sorted(duration * 1000 for duration, _ in results)
    assert all(response.status_code == 200 for _, response in results)
    admitted = sum(response.json()["allowed"] for _, response in results)
    assert admitted == 1
    current = auth_client.get(f"/v1/reserve/authorities/{authority['authority_id']}").json()
    assert current["allocated_minor"] == amount
    report = {
        "concurrency": concurrency,
        "requests": len(results),
        "admitted": admitted,
        "refused": len(results) - admitted,
        "over_allocation_minor": 0,
        "elapsed_seconds": elapsed,
        "requests_per_second": len(results) / elapsed,
        "latency_ms": {
            "median": statistics.median(latencies),
            "p95": latencies[int(len(latencies) * 0.95)],
            "max": max(latencies),
        },
        "python_process_cpu_seconds": time.process_time() - cpu_start,
        "process_peak_rss_native_units": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "transport": "in-process ASGI + local PostgreSQL",
        "provider_calls": 0,
        "scope": "single buyer/merchant authority; distinct checkouts; one-purchase capacity",
        "cost_limitations": (
            "No cloud/provider calls. Local hardware and PostgreSQL CPU cost not priced. "
            "Not a production capacity, growth or A/B revenue result."
        ),
    }
    destination = Path(os.environ["RESERVE_BENCHMARK_OUTPUT"])
    destination.mkdir(parents=True, exist_ok=True)
    (destination / f"reserve-concurrency-{concurrency}.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
