"""Synthetic pre-auth validation comparison. No MCP, credentials or business writes.

Usage: python tests/benchmark_round3.py --baseline-script /path/to/old/workorder_branches.py
The baseline is supplied explicitly; never downloads or executes a remote file.
"""
import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
import statistics
import time
import tracemalloc
from unittest.mock import patch

from test_round3_execution import ReadyCreateTests, wb, local_evidence


def measure(module, size, repetitions):
    fixture = ReadyCreateTests()
    fixture.setUp()
    try:
        fixture.batch.__class__ = module.WorkorderBranches
        if size:
            # Creation is outside the measured region; data is wholly synthetic.
            evidence = fixture.root/'large-evidence.bin'
            with evidence.open('wb') as stream:
                for _ in range(size):
                    stream.write(b'x' * (1024 * 1024))
            fixture.prepared['evidence_sha256'][str(evidence)] = local_evidence.digest_file(evidence)
        approved = fixture.create_fixture()
        results = []
        for _ in range(repetitions):
            fixture.calls.clear()
            tracemalloc.start()
            started = time.perf_counter()
            with local_evidence.Profile(enabled=True) as profile, \
                 patch.object(module, 'complete_manual_scope', return_value=None), \
                 patch.object(module, 'validate_source_check', return_value={'basis':'synthetic'}):
                try:
                    asyncio.run(fixture.batch.create(approved, fixture.source_check))
                except ValueError as exc:
                    if str(exc) != '认证失效，未开始正式开单':
                        raise
                else:
                    raise AssertionError('Synthetic auth failure must block the writer')
            elapsed = time.perf_counter()-started
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            assert fixture.calls == ['e10_connection_status']
            assert fixture.batch.order.generate_calls == fixture.batch.manual.create_calls == 0
            results.append({**profile.snapshot(), 'wall_seconds':elapsed,
                            'peak_python_bytes':peak, 'mock_tool_calls':len(fixture.calls),
                            'process_starts':0})
        return {key: statistics.median(row[key] for row in results) for key in results[0]}
    finally:
        fixture.doCleanups()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-script', required=True)
    parser.add_argument('--repetitions', type=int, default=7)
    args = parser.parse_args()
    if not 1 <= args.repetitions <= 100:
        parser.error('repetitions must be 1..100')
    spec = importlib.util.spec_from_file_location('round3_baseline_branches', args.baseline_script)
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    # Equivalent I/O operations, instrumented equally. No change to baseline guards.
    baseline.read = local_evidence.read_json
    baseline.digest = lambda path: local_evidence.sha256_bytes(local_evidence.read_bytes(path))
    result = {'schema_version':1, 'offline':True, 'scope':'synchronous_create_checks_until_failed_auth',
              'source_comparison':'identical stubs; complete_manual_scope and validate_source_check mocked',
              'memory':'tracemalloc Python allocations, not whole-process RSS',
              'repetitions':args.repetitions, 'cases':[]}
    for name, size in [('small',0), ('large_32MiB',32)]:
        result['cases'].append({'name':name, 'baseline':measure(baseline,size,args.repetitions),
                                'optimized':measure(wb,size,args.repetitions)})
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
