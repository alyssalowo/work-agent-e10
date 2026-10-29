"""Offline execution-gate regressions for workorder_branches (synthetic files only)."""
import asyncio
import hashlib
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / ".agents" / "skills" / "packing-order" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import workorder_branches as wb
import local_evidence

try:
    import jsonschema  # noqa: F401 - optional in the bundled offline test runtime
except ModuleNotFoundError:
    # read_sequence ordering tests do not exercise schema validation. Keep the
    # import offline and avoid making the optional MCP runtime a test dependency.
    class _NoopValidator:
        def __init__(self, _schema):
            pass

        def iter_errors(self, _value):
            return iter(())

    _jsonschema = types.ModuleType("jsonschema")
    _jsonschema.Draft202012Validator = _NoopValidator
    sys.modules["jsonschema"] = _jsonschema

import mcp_session


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


class OrderStub:
    def __init__(self):
        self.state = {"generation": {"entries": []}}
        self.ready_calls = 0
        self.generate_calls = 0

    def ready(self):
        self.ready_calls += 1

    async def generate(self):
        self.generate_calls += 1


class ManualStub:
    def __init__(self, directory):
        self.directory = directory
        self.create_calls = 0

    async def create(self, *_args):
        self.create_calls += 1


class ReadyCreateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.batch = wb.WorkorderBranches.__new__(wb.WorkorderBranches)
        self.batch.base = self.root
        self.batch.directory = self.root / "branch"
        self.batch.directory.mkdir()
        self.batch.session_id = "synthetic-session"
        self.batch.state = {"stage": "prepared"}
        self.batch.order = OrderStub()
        self.batch.call = self.fake_call
        self.calls = []
        self.batch.manual = ManualStub(self.batch.directory / "manual19")
        self.batch.manual.directory.mkdir()

        self.batch.config_path = self.root / "config.json"
        self.batch.order_request_path = self.root / "order-request.json"
        self.batch.manual_scope_path = self.root / "manual-scope.json"
        put(self.batch.config_path, {"synthetic": "config"})
        put(self.batch.order_request_path, {"synthetic": "order-request"})
        put(self.batch.manual_scope_path, {"specs": []})
        self.batch.input_hashes = {
            str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (self.batch.config_path, self.batch.order_request_path, self.batch.manual_scope_path)
        }
        self.evidence = self.root / "evidence.json"
        put(self.evidence, {"evidence": "synthetic"})
        self.source_check = self.root / "source-check.json"
        put(self.source_check, {"snapshot": "synthetic"})
        self.manual_plan_path = self.batch.manual.directory / "prepared.json"
        self.manual_plan = {
            "source_snapshot": str(self.source_check),
            "source_sha256": hashlib.sha256(self.source_check.read_bytes()).hexdigest(),
            "session_id": self.batch.session_id,
            "first_prepare_utc": datetime.now(timezone.utc).isoformat(),
        }
        manual_plan_hash = put(self.manual_plan_path, self.manual_plan)
        self.prepared_path = self.batch.directory / "prepared.json"
        self.prepared = {"session_id": self.batch.session_id, "evidence_sha256": {
            str(self.evidence): hashlib.sha256(self.evidence.read_bytes()).hexdigest(),
            str(self.manual_plan_path): manual_plan_hash,
        }}
        self.batch.state["prepared_sha256"] = put(self.prepared_path, self.prepared)

    async def fake_call(self, name, _args=None):
        self.calls.append(name)
        if name == "e10_connection_status":
            return {"ok": False, "authenticated": False}
        return {"ok": True}

    def ready(self):
        return self.batch.ready()

    def assert_prepared_rejected(self, mutate):
        mutate()
        with self.assertRaises((ValueError, KeyError)):
            self.ready()

    def test_ready_rejects_modified_input_and_evidence(self):
        self.assert_prepared_rejected(lambda: self.batch.order_request_path.write_text("changed", encoding="utf-8"))

    def test_ready_rejects_modified_prepared_and_evidence(self):
        self.assert_prepared_rejected(lambda: self.evidence.write_text("changed", encoding="utf-8"))

    def test_ready_rejects_modified_prepared_json(self):
        self.assert_prepared_rejected(lambda: self.prepared_path.write_text("{}", encoding="utf-8"))

    def test_ready_returns_call_local_verified_json_and_fingerprint_then_next_call_reloads(self):
        with local_evidence.Profile(enabled=True) as profile:
            first = self.ready()
            self.assertEqual(first.json(self.prepared_path), self.prepared)
            self.assertEqual(first.fingerprint(self.prepared_path), self.batch.state["prepared_sha256"])
        counts = profile.snapshot()
        self.assertEqual(counts["repeated_file_reads"], 0)
        self.assertEqual(counts["file_reads"], 6)
        self.assertEqual(counts["sha256_calls"], 6)
        put(self.prepared_path, {**self.prepared, "between_calls": True})
        self.assertEqual(first.json(self.prepared_path), self.prepared)
        with local_evidence.Profile(enabled=True) as next_profile:
            with self.assertRaises((ValueError, KeyError)):
                self.ready()
        self.assertGreater(next_profile.snapshot()["file_reads"], 0)

    def create_fixture(self, *, stage="prepared", wrong_approval=False, wrong_session=False,
                       expired=False, checkpoint=False):
        self.batch.state["stage"] = stage
        if wrong_session:
            self.prepared["session_id"] = "different-synthetic-session"
        if expired:
            put(self.batch.manual_scope_path, {"specs": [{"sheet": "synthetic", "row": 1}]})
            self.batch.input_hashes[str(self.batch.manual_scope_path)] = hashlib.sha256(self.batch.manual_scope_path.read_bytes()).hexdigest()
            self.manual_plan["first_prepare_utc"] = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        manual_hash = put(self.manual_plan_path, self.manual_plan)
        self.prepared["evidence_sha256"][str(self.manual_plan_path)] = manual_hash
        # Refresh only the expected approved artifact fingerprints for this intended fixture state.
        prepared_hash = put(self.prepared_path, self.prepared)
        self.batch.state["prepared_sha256"] = prepared_hash
        if checkpoint:
            put(self.batch.manual.directory / "checkpoint.json", {"stage": "creating"})
        approved = "0" * 64 if wrong_approval else prepared_hash
        return approved

    def run_create(self, **kwargs):
        approved = self.create_fixture(**kwargs)
        with patch.object(wb, "complete_manual_scope", return_value=None), \
             patch.object(wb, "validate_source_check", return_value={"basis": "synthetic"}):
            with self.assertRaises((ValueError, KeyError)):
                asyncio.run(self.batch.create(approved, self.source_check))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.batch.order.generate_calls, 0)
        self.assertEqual(self.batch.manual.create_calls, 0)

    def test_create_wrong_approval_hash_blocks_before_business_calls(self):
        self.run_create(wrong_approval=True)

    def test_create_wrong_session_blocks_before_business_calls(self):
        self.run_create(wrong_session=True)

    def test_create_expired_manual_draft_blocks_before_business_calls(self):
        self.run_create(expired=True)

    def test_create_existing_checkpoint_blocks_before_business_calls(self):
        self.run_create(checkpoint=True)

    def test_create_wrong_stage_blocks_before_business_calls(self):
        self.run_create(stage="created")

    def test_create_stops_at_failed_fresh_auth_check_before_writer(self):
        approved = self.create_fixture()
        with patch.object(wb, "complete_manual_scope", return_value=None), \
             patch.object(wb, "validate_source_check", return_value={"basis": "synthetic"}):
            with self.assertRaises(ValueError):
                asyncio.run(self.batch.create(approved, self.source_check))
        self.assertEqual(self.calls, ["e10_connection_status"])
        self.assertEqual(self.batch.order.generate_calls, 0)
        self.assertEqual(self.batch.manual.create_calls, 0)

    def test_create_reuses_verified_files_before_fresh_auth_boundary(self):
        approved = self.create_fixture()
        with patch.object(wb, "complete_manual_scope", return_value=None), \
             patch.object(wb, "validate_source_check", return_value={"basis": "synthetic"}):
            with local_evidence.Profile(enabled=True) as profile:
                with self.assertRaises(ValueError):
                    asyncio.run(self.batch.create(approved, self.source_check))
        counts = profile.snapshot()
        self.assertEqual(counts["repeated_file_reads"], 0)
        self.assertEqual(counts["file_reads"], 7)
        self.assertEqual(counts["sha256_calls"], 6)
        self.assertEqual(counts["json_parses"], 4)
        self.assertEqual(self.calls, ["e10_connection_status"])


class LocalEvidenceTests(unittest.TestCase):
    def test_run_subprocess_profiles_success_without_launching(self):
        expected = object()
        with patch.object(local_evidence.subprocess, "run", return_value=expected) as mocked:
            with local_evidence.Profile(enabled=True) as profile:
                result = local_evidence.run_subprocess("synthetic-command", timeout=1)
        self.assertIs(result, expected)
        mocked.assert_called_once_with("synthetic-command", timeout=1)
        counts = profile.snapshot()
        self.assertEqual(counts["subprocess_calls"], 1)
        self.assertGreaterEqual(counts["subprocess_seconds"], 0)

    def test_run_subprocess_profiles_and_propagates_exception_without_retry(self):
        failure = RuntimeError("synthetic launch failure")
        with patch.object(local_evidence.subprocess, "run", side_effect=failure) as mocked:
            with local_evidence.Profile(enabled=True) as profile:
                with self.assertRaises(RuntimeError) as caught:
                    local_evidence.run_subprocess("synthetic-command")
        self.assertIs(caught.exception, failure)
        mocked.assert_called_once_with("synthetic-command")
        counts = profile.snapshot()
        self.assertEqual(counts["subprocess_calls"], 1)
        self.assertGreaterEqual(counts["subprocess_seconds"], 0)

    def test_streamed_digest_and_repeated_read_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.bin"
            payload = b"local synthetic evidence" * 100
            path.write_bytes(payload)
            with local_evidence.Profile(enabled=True) as profile:
                first = local_evidence.digest_file(path)
                second = local_evidence.digest_file(path)
            counts = profile.snapshot()
            self.assertEqual(first, hashlib.sha256(payload).hexdigest())
            self.assertEqual(second, first)
            self.assertEqual(counts["file_reads"], 2)
            self.assertEqual(counts["sha256_calls"], 2)
            self.assertEqual(counts["sha256_bytes"], 2 * len(payload))
            self.assertEqual(counts["repeated_file_reads"], 1)

    def test_verified_json_is_operation_local_and_new_ready_rejects_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bound.json"
            digest = put(path, {"version": "old"})
            verified = local_evidence.VerifiedInputs(keep=(path,))
            verified.verify(path, digest, "changed")
            self.assertEqual(verified.json(path), {"version": "old"})
            put(path, {"version": "new"})
            self.assertEqual(verified.json(path), {"version": "old"})
            self.assertNotEqual(verified.fingerprint(path), hashlib.sha256(path.read_bytes()).hexdigest())


class ReadSequenceTests(unittest.TestCase):
    def setup_sequence(self, directory):
        root = Path(directory)
        requests = []
        for index in range(2):
            request = root / f"request-{index}.json"
            request.write_text(json.dumps({"name": "e10_connection_status", "arguments": {}}), encoding="utf-8")
            requests.append({"request": str(request), "output": str(root / f"output-{index}.json")})
        return {"job": "read_sequence", "steps": requests}

    def test_failed_read_stops_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            async def fake_call(name, **_kwargs):
                calls.append(name)
                return {"ok": False, "error": "synthetic failure"}
            result = asyncio.run(mcp_session.read_sequence(fake_call, self.setup_sequence(directory), Path(directory)))
            self.assertFalse(result["ok"])
            self.assertEqual(len(calls), 1)
            self.assertFalse((Path(directory) / "output-1.json").exists())

    def test_timeout_propagates_without_continuation(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            async def fake_call(name, **_kwargs):
                calls.append(name)
                raise TimeoutError("synthetic timeout")
            with self.assertRaises(TimeoutError):
                asyncio.run(mcp_session.read_sequence(fake_call, self.setup_sequence(directory), Path(directory)))
            self.assertEqual(len(calls), 1)
            self.assertFalse((Path(directory) / "output-1.json").exists())


class PerformanceLogTests(unittest.TestCase):
    def test_performance_log_contains_only_fixed_tags_and_numeric_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            metrics = {"scope": "command", "session_id": "synthetic-session",
                       "sequence": 2, "file_reads": 3, "sha256_calls": 1,
                       "elapsed_seconds": 0.25}
            mcp_session.append_performance(base, metrics)
            rows = [json.loads(line) for line in (base / "local-performance.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0], {"schema_version": 1, **metrics})
            self.assertFalse({"path", "arguments", "params", "request"} & rows[0].keys())
            self.assertTrue(all(isinstance(value, (int, float)) for key, value in rows[0].items()
                                if key not in {"scope", "session_id"}))

    def test_performance_log_oserror_is_reported_once_without_retry_or_raise(self):
        events = []
        with patch.object(Path, "open", side_effect=OSError("synthetic disk error")) as mocked, \
             patch.object(mcp_session, "print_json", side_effect=events.append):
            mcp_session.append_performance(Path("synthetic-base"),
                                           {"scope": "command", "session_id": "synthetic-session", "sequence": 1})
        mocked.assert_called_once()
        self.assertEqual(events, [{"event": "performance_log_error", "automatic_retry": False}])

if __name__ == "__main__":
    unittest.main()
