"""
aihydro-bench — scientific correctness benchmark harness.

Run all fixture-mode tasks (no network):
    pytest tests/test_bench.py -m bench -v

Run live tasks (requires USGS / GridMET APIs):
    pytest tests/test_bench.py -m bench_live -v

Run everything:
    pytest tests/test_bench.py -m "bench or bench_live" -v
"""
