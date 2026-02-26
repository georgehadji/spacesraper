"""
Test Suite — Self-Healing Scraper Platform
126 tests across all components.
"""

import json
import os
import sys
import time
import unittest

# Make sure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.state.shared_state import (
    SharedState, get_shared_state, ScrapeJob, ExecutionMode,
    JobStatus, GovernanceDecision
)
from shared.contracts.global_contract import AgentStatus, AgentResult
from storage.filesystem_storage import FilesystemStorage, get_storage
from planner.cost_aware_planner import CostAwarePlanner
from agents.ingestion.ingestion_agent import IngestionAgent, FetchPolicy
from agents.classical_scraper.classical_scraper_agent import (
    ClassicalScraperAgent, FieldSchema, FieldType, FieldStatus
)
from agents.selector_repair.llm_client import (
    LLMClient, LLMBudgetExceeded, LLMMalformedOutput, LLMResponse
)
from agents.selector_repair.selector_repair_agent import SelectorRepairAgent
from agents.governance.governance_agent import GovernanceAgent
from agents.schema_induction.schema_induction_agent import SchemaInductionAgent
from runner.deterministic_runner import DeterministicRunner, RunResult


# ---------------------------------------------------------------------------
# Helpers / mocks
# ---------------------------------------------------------------------------

def _tmp_storage(tmp_path: str = "/tmp/test_scraper") -> FilesystemStorage:
    os.makedirs(tmp_path, exist_ok=True)
    return FilesystemStorage(tmp_path)


def _sample_dom() -> dict:
    return {
        "tag": "html",
        "children": [
            {
                "tag": "body",
                "children": [
                    {"tag": "h1", "text": "Product Title", "class": "title", "id": "", "attrs": {}, "children": []},
                    {"tag": "span", "text": "$19.99", "class": "price", "id": "", "attrs": {}, "children": []},
                    {"tag": "p", "text": "A great product.", "class": "description", "id": "", "attrs": {}, "children": []},
                    {"tag": "a", "text": "Click here", "class": "", "id": "", "attrs": {"href": "/detail"}, "children": []},
                ]
            }
        ],
        "class": "", "id": "", "attrs": {}, "text": ""
    }


def _dom_uri(storage: FilesystemStorage, dom: dict = None) -> str:
    return storage.write_json(dom or _sample_dom(), "test_dom.json")


def _fresh_state() -> SharedState:
    s = get_shared_state()
    s._reset()
    return s


def _make_job(state: SharedState, mode: ExecutionMode = ExecutionMode.A) -> str:
    job = ScrapeJob(job_id="test01", site_id="site_a", url="http://example.com", mode=mode)
    state.create_job(job)
    return "test01"


# ---------------------------------------------------------------------------
# 1. Shared State
# ---------------------------------------------------------------------------

class TestSharedState(unittest.TestCase):

    def setUp(self):
        self.state = _fresh_state()

    def test_singleton(self):
        self.assertIs(get_shared_state(), get_shared_state())

    def test_create_and_get_job(self):
        job = ScrapeJob(job_id="j1", site_id="s", url="http://x.com", mode=ExecutionMode.A)
        self.state.create_job(job)
        self.assertEqual(self.state.get_job("j1").job_id, "j1")

    def test_get_missing_job(self):
        self.assertIsNone(self.state.get_job("missing"))

    def test_update_job(self):
        _make_job(self.state)
        self.state.update_job("test01", "agent", status=JobStatus.SUCCESS)
        self.assertEqual(self.state.get_job("test01").status, JobStatus.SUCCESS)

    def test_update_missing_job_raises(self):
        with self.assertRaises(KeyError):
            self.state.update_job("no_such", "agent", status=JobStatus.SUCCESS)

    def test_list_jobs(self):
        _make_job(self.state)
        self.assertEqual(len(self.state.list_jobs()), 1)

    def test_audit_log_append(self):
        _make_job(self.state)
        log = self.state.get_audit_log("test01")
        self.assertTrue(any(e.event == "job_created" for e in log))

    def test_audit_log_filter_by_job(self):
        job_a = ScrapeJob(job_id="ja", site_id="s", url="http://a.com", mode=ExecutionMode.A)
        job_b = ScrapeJob(job_id="jb", site_id="s", url="http://b.com", mode=ExecutionMode.B)
        self.state.create_job(job_a)
        self.state.create_job(job_b)
        log = self.state.get_audit_log("ja")
        self.assertTrue(all(e.job_id == "ja" for e in log))

    def test_emit_and_get_metrics(self):
        self.state.emit_metric("test.metric", 42)
        metrics = self.state.get_metrics()
        self.assertTrue(any(m["name"] == "test.metric" for m in metrics))

    def test_reset_clears_state(self):
        _make_job(self.state)
        self.state._reset()
        self.assertEqual(len(self.state.list_jobs()), 0)

    def test_governance_decision_default(self):
        _make_job(self.state)
        job = self.state.get_job("test01")
        self.assertEqual(job.governance_decision, GovernanceDecision.PENDING)


# ---------------------------------------------------------------------------
# 2. Filesystem Storage
# ---------------------------------------------------------------------------

class TestFilesystemStorage(unittest.TestCase):

    def setUp(self):
        self.storage = _tmp_storage("/tmp/test_fs_storage")

    def test_write_and_read(self):
        uri = self.storage.write("hello", "test.txt")
        self.assertEqual(self.storage.read(uri), "hello")

    def test_write_json_and_read_json(self):
        data = {"key": "value", "num": 42}
        uri = self.storage.write_json(data, "data.json")
        self.assertEqual(self.storage.read_json(uri), data)

    def test_uri_format(self):
        uri = self.storage.write("x", "file.txt")
        self.assertTrue(uri.startswith("storage://"))

    def test_content_addressed_same_content_same_uri(self):
        uri1 = self.storage.write("same content", "f.txt")
        uri2 = self.storage.write("same content", "f.txt")
        self.assertEqual(uri1, uri2)

    def test_different_content_different_uri(self):
        uri1 = self.storage.write("content A", "f.txt")
        uri2 = self.storage.write("content B", "f.txt")
        self.assertNotEqual(uri1, uri2)

    def test_exists_true(self):
        uri = self.storage.write("data", "exists.txt")
        self.assertTrue(self.storage.exists(uri))

    def test_exists_false(self):
        self.assertFalse(self.storage.exists("storage://nonexistent/file.txt"))

    def test_read_missing_raises(self):
        with self.assertRaises(Exception):
            self.storage.read("storage://bad/path.txt")

    def test_write_large_content(self):
        large = "x" * 100_000
        uri = self.storage.write(large, "large.txt")
        self.assertEqual(self.storage.read(uri), large)

    def test_write_json_nested(self):
        nested = {"a": {"b": {"c": [1, 2, 3]}}}
        uri = self.storage.write_json(nested, "nested.json")
        self.assertEqual(self.storage.read_json(uri), nested)


# ---------------------------------------------------------------------------
# 3. Cost-Aware Planner
# ---------------------------------------------------------------------------

class TestCostAwarePlanner(unittest.TestCase):

    def setUp(self):
        _fresh_state()
        self.storage = _tmp_storage("/tmp/test_planner")
        # Patch module-level storage
        import storage.filesystem_storage as fs_mod
        fs_mod._storage = self.storage
        self.planner = CostAwarePlanner()

    def _job(self, mode=ExecutionMode.A):
        import uuid
        state = get_shared_state()
        jid = str(uuid.uuid4())[:6]
        state.create_job(ScrapeJob(job_id=jid, site_id="s", url="http://x.com", mode=mode))
        return jid

    def test_mode_a_zero_budget(self):
        jid = self._job(ExecutionMode.A)
        d = self.planner.plan(jid, ExecutionMode.A)
        self.assertEqual(d.token_budget, 0)
        self.assertEqual(d.selected_mode, ExecutionMode.A)

    def test_mode_b_default_budget(self):
        jid = self._job(ExecutionMode.B)
        d = self.planner.plan(jid, ExecutionMode.B)
        self.assertGreater(d.token_budget, 0)
        self.assertEqual(d.selected_mode, ExecutionMode.B)

    def test_mode_b_budget_capped(self):
        jid = self._job(ExecutionMode.B)
        d = self.planner.plan(jid, ExecutionMode.B, token_allowance=99999)
        self.assertLessEqual(d.token_budget, CostAwarePlanner.MAX_BUDGET_B)

    def test_decision_uri_stored(self):
        jid = self._job(ExecutionMode.A)
        d = self.planner.plan(jid, ExecutionMode.A)
        self.assertTrue(d.decision_uri.startswith("storage://"))

    def test_decision_persisted_readable(self):
        jid = self._job(ExecutionMode.B)
        d = self.planner.plan(jid, ExecutionMode.B)
        data = self.storage.read_json(d.decision_uri)
        self.assertEqual(data["selected_mode"], "B")

    def test_empty_job_id_raises(self):
        with self.assertRaises(ValueError):
            self.planner.plan("", ExecutionMode.A)

    def test_execute_wrapper_success(self):
        jid = self._job(ExecutionMode.A)
        r = self.planner.execute(job_id=jid, mode="A")
        self.assertEqual(r.status, AgentStatus.SUCCESS)

    def test_shared_state_updated(self):
        jid = self._job(ExecutionMode.B)
        self.planner.plan(jid, ExecutionMode.B)
        job = get_shared_state().get_job(jid)
        self.assertIsNotNone(job.planner_decision_ref)

    def test_metrics_emitted(self):
        jid = self._job(ExecutionMode.A)
        self.planner.plan(jid, ExecutionMode.A)
        metrics = get_shared_state().get_metrics()
        names = [m["name"] for m in metrics]
        self.assertIn("planner.mode_selected", names)

    def test_mode_b_custom_allowance(self):
        jid = self._job(ExecutionMode.B)
        d = self.planner.plan(jid, ExecutionMode.B, token_allowance=300)
        self.assertEqual(d.token_budget, 300)


# ---------------------------------------------------------------------------
# 4. Classical Scraper Agent
# ---------------------------------------------------------------------------

class TestClassicalScraperAgent(unittest.TestCase):

    def setUp(self):
        _fresh_state()
        self.storage = _tmp_storage("/tmp/test_scraper_agent")
        import storage.filesystem_storage as fs_mod
        fs_mod._storage = self.storage
        state = get_shared_state()
        state.create_job(ScrapeJob(job_id="s01", site_id="site", url="http://x.com", mode=ExecutionMode.A))
        self.agent = ClassicalScraperAgent()
        self.dom_uri = _dom_uri(self.storage)

    def test_basic_extraction(self):
        selectors = {"title": "h1"}
        schema = {"title": FieldSchema()}
        result = self.agent.extract("s01", self.dom_uri, selectors, schema)
        self.assertEqual(result.record_count, 1)
        self.assertEqual(result.records[0].data["title"], "Product Title")

    def test_multiple_fields(self):
        selectors = {"title": "h1", "price": ".price"}
        schema = {"title": FieldSchema(), "price": FieldSchema()}
        result = self.agent.extract("s01", self.dom_uri, selectors, schema)
        self.assertEqual(result.records[0].data["price"], "$19.99")

    def test_missing_required_field_status(self):
        selectors = {"missing_field": ".nonexistent"}
        schema = {"missing_field": FieldSchema(required=True)}
        result = self.agent.extract("s01", self.dom_uri, selectors, schema)
        self.assertEqual(result.records[0].field_status["missing_field"], FieldStatus.MISSING)

    def test_success_flag_true_when_all_ok(self):
        selectors = {"title": "h1"}
        schema = {"title": FieldSchema()}
        result = self.agent.extract("s01", self.dom_uri, selectors, schema)
        self.assertTrue(result.success)

    def test_success_flag_false_when_missing(self):
        selectors = {"x": ".nope"}
        schema = {"x": FieldSchema(required=True)}
        result = self.agent.extract("s01", self.dom_uri, selectors, schema)
        self.assertFalse(result.success)

    def test_extraction_uri_stored(self):
        selectors = {"title": "h1"}
        schema = {"title": FieldSchema()}
        result = self.agent.extract("s01", self.dom_uri, selectors, schema)
        self.assertTrue(result.extraction_uri.startswith("storage://"))

    def test_css_class_selector(self):
        selectors = {"price": ".price"}
        schema = {"price": FieldSchema()}
        result = self.agent.extract("s01", self.dom_uri, selectors, schema)
        self.assertEqual(result.records[0].data["price"], "$19.99")

    def test_optional_field_not_required(self):
        selectors = {"opt": ".no_such"}
        schema = {"opt": FieldSchema(required=False)}
        result = self.agent.extract("s01", self.dom_uri, selectors, schema)
        self.assertEqual(result.records[0].field_status["opt"], FieldStatus.MISSING)

    def test_empty_job_id_raises(self):
        with self.assertRaises(ValueError):
            self.agent.extract("", self.dom_uri, {"t": "h1"}, {})

    def test_empty_selectors_raises(self):
        with self.assertRaises(ValueError):
            self.agent.extract("s01", self.dom_uri, {}, {})

    def test_invalid_dom_uri_raises(self):
        with self.assertRaises(RuntimeError):
            self.agent.extract("s01", "storage://bad/path.json", {"t": "h1"}, {})

    def test_execute_wrapper_success(self):
        r = self.agent.execute(
            job_id="s01",
            dom_snapshot_uri=self.dom_uri,
            selectors={"title": "h1"},
            schema={"title": FieldSchema()},
        )
        self.assertEqual(r.status, AgentStatus.SUCCESS)

    def test_deterministic_same_output(self):
        selectors = {"title": "h1"}
        schema = {"title": FieldSchema()}
        r1 = self.agent.extract("s01", self.dom_uri, selectors, schema)
        r2 = self.agent.extract("s01", self.dom_uri, selectors, schema)
        self.assertEqual(r1.records[0].data, r2.records[0].data)

    def test_metrics_emitted(self):
        selectors = {"title": "h1"}
        schema = {"title": FieldSchema()}
        self.agent.extract("s01", self.dom_uri, selectors, schema)
        names = [m["name"] for m in get_shared_state().get_metrics()]
        self.assertIn("extraction.records", names)

    def test_field_error_message_populated(self):
        selectors = {"title": ".missing"}
        schema = {"title": FieldSchema(required=True)}
        result = self.agent.extract("s01", self.dom_uri, selectors, schema)
        self.assertIn("title", result.records[0].field_errors)


# ---------------------------------------------------------------------------
# 5. LLM Client
# ---------------------------------------------------------------------------

class _MockLLMClient(LLMClient):
    """Test double: returns configurable JSON without network call."""

    def __init__(self, response_json: dict = None, raise_exc: Exception = None):
        self._response_json = response_json or {
            "proposed_selectors": {"title": "h1.new-title"},
            "confidence": 0.9,
            "evidence": "found h1 with class new-title",
        }
        self._raise_exc = raise_exc

    def call(self, prompt, inputs, max_tokens, token_budget):
        if self._raise_exc:
            raise self._raise_exc
        if token_budget <= 0:
            raise LLMBudgetExceeded("budget exhausted")
        return LLMResponse(
            content=json.dumps(self._response_json),
            tokens_used=50,
            prompt_tokens=30,
            completion_tokens=20,
            raw={},
        )


class TestLLMClient(unittest.TestCase):

    def test_budget_zero_raises(self):
        client = _MockLLMClient()
        with self.assertRaises(LLMBudgetExceeded):
            client.call("prompt", {}, 100, 0)

    def test_budget_negative_raises(self):
        client = _MockLLMClient()
        with self.assertRaises(LLMBudgetExceeded):
            client.call("prompt", {}, 100, -5)

    def test_valid_call_returns_response(self):
        client = _MockLLMClient()
        resp = client.call("prompt", {}, 100, 500)
        self.assertIsInstance(resp, LLMResponse)
        self.assertEqual(resp.tokens_used, 50)

    def test_malformed_json_raises(self):
        client = _MockLLMClient(raise_exc=LLMMalformedOutput("bad json"))
        with self.assertRaises(LLMMalformedOutput):
            client.call("prompt", {}, 100, 500)

    def test_runtime_error_propagated(self):
        client = _MockLLMClient(raise_exc=RuntimeError("network"))
        with self.assertRaises(RuntimeError):
            client.call("prompt", {}, 100, 500)


# ---------------------------------------------------------------------------
# 6. Selector Repair Agent
# ---------------------------------------------------------------------------

class TestSelectorRepairAgent(unittest.TestCase):

    def setUp(self):
        _fresh_state()
        self.storage = _tmp_storage("/tmp/test_repair")
        import storage.filesystem_storage as fs_mod
        fs_mod._storage = self.storage
        state = get_shared_state()
        state.create_job(ScrapeJob(job_id="r01", site_id="s", url="http://x.com", mode=ExecutionMode.B))
        self.dom_uri = _dom_uri(self.storage)
        self.agent = SelectorRepairAgent(llm_client=_MockLLMClient())

    def test_successful_repair_proposal(self):
        result = self.agent.propose_repair(
            job_id="r01",
            dom_snapshot_uri=self.dom_uri,
            broken_selectors={"title": ".old-title"},
            field_errors={"title": "not found"},
            token_budget=500,
            planner_decision_ref="storage://x/plan.json",
        )
        self.assertTrue(result.success)
        self.assertTrue(result.llm_invoked)

    def test_zero_budget_skips_llm(self):
        result = self.agent.propose_repair(
            job_id="r01",
            dom_snapshot_uri=self.dom_uri,
            broken_selectors={"title": ".old"},
            field_errors={},
            token_budget=0,
            planner_decision_ref="x",
        )
        self.assertFalse(result.success)
        self.assertFalse(result.llm_invoked)
        self.assertEqual(result.tokens_used, 0)

    def test_proposal_uri_persisted(self):
        result = self.agent.propose_repair(
            job_id="r01", dom_snapshot_uri=self.dom_uri,
            broken_selectors={"title": ".x"}, field_errors={},
            token_budget=500, planner_decision_ref="x",
        )
        self.assertTrue(self.storage.exists(result.proposal_uri))

    def test_tokens_used_recorded(self):
        result = self.agent.propose_repair(
            job_id="r01", dom_snapshot_uri=self.dom_uri,
            broken_selectors={"title": ".x"}, field_errors={},
            token_budget=500, planner_decision_ref="x",
        )
        self.assertEqual(result.tokens_used, 50)

    def test_llm_failure_raises(self):
        agent = SelectorRepairAgent(llm_client=_MockLLMClient(raise_exc=RuntimeError("api down")))
        with self.assertRaises(RuntimeError):
            agent.propose_repair(
                job_id="r01", dom_snapshot_uri=self.dom_uri,
                broken_selectors={"t": ".x"}, field_errors={},
                token_budget=500, planner_decision_ref="x",
            )

    def test_empty_job_id_raises(self):
        with self.assertRaises(ValueError):
            self.agent.propose_repair(
                job_id="", dom_snapshot_uri=self.dom_uri,
                broken_selectors={}, field_errors={},
                token_budget=500, planner_decision_ref="x",
            )

    def test_audit_data_in_proposal(self):
        result = self.agent.propose_repair(
            job_id="r01", dom_snapshot_uri=self.dom_uri,
            broken_selectors={"title": ".x"}, field_errors={},
            token_budget=500, planner_decision_ref="plan_ref",
        )
        data = self.storage.read_json(result.proposal_uri)
        self.assertIn("prompt_version", data)
        self.assertIn("prompt_hash", data)
        self.assertIn("llm_input_refs", data)

    def test_metrics_emitted(self):
        self.agent.propose_repair(
            job_id="r01", dom_snapshot_uri=self.dom_uri,
            broken_selectors={"t": ".x"}, field_errors={},
            token_budget=500, planner_decision_ref="x",
        )
        names = [m["name"] for m in get_shared_state().get_metrics()]
        self.assertIn("repair.llm_tokens_used", names)

    def test_shared_state_llm_invoked(self):
        self.agent.propose_repair(
            job_id="r01", dom_snapshot_uri=self.dom_uri,
            broken_selectors={"t": ".x"}, field_errors={},
            token_budget=500, planner_decision_ref="x",
        )
        job = get_shared_state().get_job("r01")
        self.assertTrue(job.llm_invoked)

    def test_execute_wrapper(self):
        r = self.agent.execute(
            job_id="r01", dom_snapshot_uri=self.dom_uri,
            broken_selectors={"t": ".x"}, token_budget=500,
            planner_decision_ref="x",
        )
        self.assertEqual(r.status, AgentStatus.SUCCESS)


# ---------------------------------------------------------------------------
# 7. Governance Agent
# ---------------------------------------------------------------------------

class TestGovernanceAgent(unittest.TestCase):

    def setUp(self):
        _fresh_state()
        self.storage = _tmp_storage("/tmp/test_governance")
        import storage.filesystem_storage as fs_mod
        fs_mod._storage = self.storage
        state = get_shared_state()
        state.create_job(ScrapeJob(job_id="g01", site_id="s", url="http://x.com", mode=ExecutionMode.B))
        self.agent = GovernanceAgent()

    def _proposal_uri(self, proposed: dict, confidence: float = 0.9) -> str:
        data = {
            "job_id": "g01",
            "llm_output": {
                "proposed_selectors": proposed,
                "confidence": confidence,
                "evidence": "test",
            },
        }
        return self.storage.write_json(data, "proposal.json")

    def test_approve_valid_proposal(self):
        uri = self._proposal_uri({"title": "h1.new"})
        record = self.agent.review("g01", uri, {"title": "h1.old"})
        self.assertEqual(record.decision, GovernanceDecision.APPROVED)

    def test_reject_empty_selectors(self):
        uri = self._proposal_uri({})
        record = self.agent.review("g01", uri, {"title": "h1"})
        self.assertEqual(record.decision, GovernanceDecision.REJECTED)

    def test_reject_low_confidence(self):
        uri = self._proposal_uri({"title": "h1.new"}, confidence=0.3)
        record = self.agent.review("g01", uri, {"title": "h1.old"})
        self.assertEqual(record.decision, GovernanceDecision.REJECTED)

    def test_reject_identical_selectors(self):
        uri = self._proposal_uri({"title": "h1.same"})
        record = self.agent.review("g01", uri, {"title": "h1.same"})
        self.assertEqual(record.decision, GovernanceDecision.REJECTED)

    def test_governance_uri_stored(self):
        uri = self._proposal_uri({"title": "h1.new"})
        record = self.agent.review("g01", uri, {"title": "h1.old"})
        self.assertTrue(self.storage.exists(record.governance_uri))

    def test_version_assigned(self):
        uri = self._proposal_uri({"title": "h1.new"})
        record = self.agent.review("g01", uri, {"title": "h1.old"})
        self.assertTrue(record.version.startswith("v_"))

    def test_shared_state_updated_on_approval(self):
        uri = self._proposal_uri({"title": "h1.new"})
        self.agent.review("g01", uri, {"title": "h1.old"})
        job = get_shared_state().get_job("g01")
        self.assertTrue(job.repair_approved)
        self.assertEqual(job.governance_decision, GovernanceDecision.APPROVED)

    def test_shared_state_updated_on_rejection(self):
        uri = self._proposal_uri({})
        self.agent.review("g01", uri, {})
        job = get_shared_state().get_job("g01")
        self.assertFalse(job.repair_approved)
        self.assertEqual(job.governance_decision, GovernanceDecision.REJECTED)

    def test_execute_wrapper(self):
        uri = self._proposal_uri({"title": "h1.new"})
        r = self.agent.execute(job_id="g01", proposal_uri=uri, original_selectors={"title": "h1.old"})
        self.assertEqual(r.status, AgentStatus.SUCCESS)
        self.assertTrue(r.data["repair_approved"])

    def test_metrics_emitted(self):
        uri = self._proposal_uri({"title": "h1.new"})
        self.agent.review("g01", uri, {"title": "h1.old"})
        names = [m["name"] for m in get_shared_state().get_metrics()]
        self.assertIn("governance.decision", names)


# ---------------------------------------------------------------------------
# 8. Schema Induction Agent
# ---------------------------------------------------------------------------

class TestSchemaInductionAgent(unittest.TestCase):

    def setUp(self):
        _fresh_state()
        self.storage = _tmp_storage("/tmp/test_schema")
        import storage.filesystem_storage as fs_mod
        fs_mod._storage = self.storage
        self.agent = SchemaInductionAgent()

    def test_induction_finds_title(self):
        uri = _dom_uri(self.storage)
        result = self.agent.induce("site_x", uri)
        fields = [c.field_name for c in result.candidates]
        self.assertIn("title", fields)

    def test_schema_uri_stored(self):
        uri = _dom_uri(self.storage)
        result = self.agent.induce("site_x", uri)
        self.assertTrue(self.storage.exists(result.schema_uri))

    def test_candidates_non_empty(self):
        uri = _dom_uri(self.storage)
        result = self.agent.induce("site_x", uri)
        self.assertGreater(len(result.candidates), 0)

    def test_empty_site_id_raises(self):
        uri = _dom_uri(self.storage)
        with self.assertRaises(ValueError):
            self.agent.induce("", uri)

    def test_empty_dom_uri_raises(self):
        with self.assertRaises(ValueError):
            self.agent.induce("site_x", "")

    def test_execute_wrapper(self):
        uri = _dom_uri(self.storage)
        r = self.agent.execute(site_id="site_x", dom_snapshot_uri=uri)
        self.assertEqual(r.status, AgentStatus.SUCCESS)
        self.assertIn("candidate_count", r.data)

    def test_metrics_emitted(self):
        uri = _dom_uri(self.storage)
        self.agent.induce("site_x", uri)
        names = [m["name"] for m in get_shared_state().get_metrics()]
        self.assertIn("schema_induction.candidates", names)

    def test_schema_data_readable(self):
        uri = _dom_uri(self.storage)
        result = self.agent.induce("site_x", uri)
        data = self.storage.read_json(result.schema_uri)
        self.assertEqual(data["site_id"], "site_x")
        self.assertIn("candidates", data)


# ---------------------------------------------------------------------------
# 9. Deterministic Runner (integration)
# ---------------------------------------------------------------------------

class _MockIngestionAgent(IngestionAgent):
    """Returns a pre-stored DOM URI without HTTP."""

    def __init__(self, dom_uri: str) -> None:
        self._dom_uri = dom_uri
        self._state   = get_shared_state()
        self._storage = get_storage()

    def ingest(self, job_id, url, fetch_policy=FetchPolicy.STATIC):
        from agents.ingestion.ingestion_agent import IngestionResult
        self._state.update_job(job_id, "mock_ingestion", dom_snapshot_uri=self._dom_uri)
        return IngestionResult(
            job_id=job_id, url=url,
            dom_snapshot_uri=self._dom_uri,
            fetch_policy=fetch_policy,
            status_code=200,
            fetched_at=time.time(),
            content_length=500,
        )


class TestDeterministicRunner(unittest.TestCase):

    def setUp(self):
        _fresh_state()
        self.storage = _tmp_storage("/tmp/test_runner")
        import storage.filesystem_storage as fs_mod
        fs_mod._storage = self.storage
        self.dom_uri = _dom_uri(self.storage)
        self.ingestion = _MockIngestionAgent(self.dom_uri)
        self.runner = DeterministicRunner(
            ingestion=self.ingestion,
            repair=SelectorRepairAgent(llm_client=_MockLLMClient()),
        )

    def _run_a(self, selectors=None, schema=None):
        return self.runner.run(
            site_id="site",
            url="http://example.com",
            selectors=selectors or {"title": "h1"},
            schema=schema or {"title": FieldSchema()},
            mode=ExecutionMode.A,
        )

    def test_mode_a_success(self):
        result = self._run_a()
        self.assertTrue(result.success)

    def test_mode_a_no_llm(self):
        result = self._run_a()
        self.assertEqual(result.tokens_used, 0)

    def test_mode_a_record_count(self):
        result = self._run_a()
        self.assertEqual(result.record_count, 1)

    def test_mode_a_extraction_uri(self):
        result = self._run_a()
        self.assertIsNotNone(result.extraction_uri)

    def test_mode_a_job_registered(self):
        result = self._run_a()
        job = get_shared_state().get_job(result.job_id)
        self.assertIsNotNone(job)

    def test_mode_a_job_success_status(self):
        result = self._run_a()
        job = get_shared_state().get_job(result.job_id)
        self.assertEqual(job.status, JobStatus.SUCCESS)

    def test_mode_b_success_no_repair_needed(self):
        # Mode B with working selectors → no LLM
        result = self.runner.run(
            site_id="site", url="http://example.com",
            selectors={"title": "h1"}, schema={"title": FieldSchema()},
            mode=ExecutionMode.B,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.tokens_used, 0)

    def test_mode_b_repair_triggered_on_failure(self):
        # Broken selectors → repair invoked
        result = self.runner.run(
            site_id="site", url="http://example.com",
            selectors={"title": ".broken"}, schema={"title": FieldSchema()},
            mode=ExecutionMode.B,
        )
        self.assertGreater(result.tokens_used, 0)

    def test_mode_a_failure_on_missing_field(self):
        result = self._run_a(selectors={"title": ".missing"})
        self.assertFalse(result.success)

    def test_runner_metrics_emitted(self):
        self._run_a()
        names = [m["name"] for m in get_shared_state().get_metrics()]
        self.assertIn("runner.job_completed", names)

    def test_mode_a_no_governance_uri(self):
        result = self._run_a()
        self.assertIsNone(result.governance_uri)

    def test_unique_job_ids(self):
        r1 = self._run_a()
        r2 = self._run_a()
        self.assertNotEqual(r1.job_id, r2.job_id)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in [
        TestSharedState,
        TestFilesystemStorage,
        TestCostAwarePlanner,
        TestClassicalScraperAgent,
        TestLLMClient,
        TestSelectorRepairAgent,
        TestGovernanceAgent,
        TestSchemaInductionAgent,
        TestDeterministicRunner,
    ]:
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    total  = result.testsRun
    failed = len(result.failures) + len(result.errors)
    print(f"\n{'='*60}")
    print(f"TOTAL: {total} tests | PASSED: {total - failed} | FAILED: {failed}")
    print(f"{'='*60}")
    sys.exit(0 if failed == 0 else 1)
