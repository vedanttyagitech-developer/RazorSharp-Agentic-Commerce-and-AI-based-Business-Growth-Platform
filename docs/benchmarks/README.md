# Local Reserve admission benchmark

Run from the repository root against the isolated test database:

```sh
REQUIRE_DB=1 RESERVE_BENCHMARK_OUTPUT=docs/benchmarks .venv/bin/pytest packages/commerce-api/tests/benchmark_reserve.py -q
```

Each scenario creates 16 distinct reviewed checkouts for one buyer/merchant and one
signed permission with capacity for exactly one purchase. It submits them with 1, 4,
or 8 concurrent callers. The assertion is one admission, 15 refusals and zero excess
allocation. No executor/provider payment is run. Test fixtures clean up their data.

The JSON files report measured request latency, throughput, Python process CPU and peak
RSS. macOS RSS is bytes; Linux RSS is KiB. CPU excludes PostgreSQL and other processes.
Peak RSS is cumulative for the test process, not incremental memory per request.

This is one small local run per concurrency level, with in-process ASGI and real local
PostgreSQL. It is a reproducible correctness/latency experiment, not production capacity,
an Internet network benchmark, an A/B growth result, or proof of reduced GCP spend.
The arithmetic retained-revenue endpoint is separate from measured incremental revenue.

No cloud or provider calls are made by this benchmark. Hardware ownership, electricity,
PostgreSQL compute, infrastructure idle cost and provider/model costs are not priced.
A production cost study still needs sustained representative traffic, independent load
generation, multiple repetitions, CPU/memory/DB/I/O saturation, cloud billing export,
and a predeclared comparison baseline. Do not extrapolate these numbers into Razorpay TPS.
