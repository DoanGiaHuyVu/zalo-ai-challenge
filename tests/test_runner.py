from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import duckdb
import solver


def state(messages):
    return json.loads(messages[-1]["content"])["working_state"]


def request(name, args, first):
    action = {"action": name, "purpose": "Synthetic loop fixture", "args": args}
    if first:
        action["task_spec"] = {"metric": "fixture sum", "period": "unknown", "grain": "row", "unit": "fixture",
                               "expected_answer_type": "number", "relevant_rules": []}
    return action


class FlowProvider:
    """Scripted fixture reading actual observations; no answer inference."""
    def __init__(self, prefix=()):
        self.prefix = list(prefix)
        self.stage = 0
        self.calls = []

    def __call__(self, messages, *, first_action):
        self.calls.append((deepcopy(messages), first_action))
        if self.prefix:
            value = self.prefix.pop(0)
            return value(messages, first_action) if callable(value) else deepcopy(value)
        step = self.stage
        self.stage += 1
        current = state(messages)
        if step == 0:
            return request("inspect", {"source_ids": ["S001"], "start_row": 1, "row_count": 12}, first_action)
        if step == 1:
            return request("load", {"requests": [{"source_ids": ["S001"], "header_rows": [1], "first_data_row": 2}]}, first_action)
        if step == 2:
            return request("sql", {"query": "SELECT sum(TRY_CAST(c002 AS DOUBLE)) total FROM raw_001", "kind": "compute"}, first_action)
        candidate = next(r["result_ref"] for r in current["results"] if r["kind"] == "compute")
        if step == 3:
            return request("sql", {"query": "SELECT 'fixture' check_name,true passed", "kind": "check", "candidate_ref": candidate}, first_action)
        check = next(r["result_ref"] for r in current["results"] if r["kind"] == "check")
        if step == 4:
            return request("finish", {"answer_type": "number", "result_ref": candidate, "check_ref": check, "layout": "scalar", "columns": [1]}, first_action)
        return request("finish", {"answer_type": "abstain", "reason": "Fixture finish rejected"}, first_action)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        business = self.root / "data/shop"
        business.mkdir(parents=True)
        (business / "README.md").write_text("Synthetic rows only; no real business question.\r\n")
        (business / "sales.csv").write_text("ID,amount\n0001,12\n0002,8\n")
        (self.root / "rulebook.md").write_text("Synthetic fixture rules.\r\n")
        (self.root / "calendar.csv").write_text("date,weekday,lunar_day,lunar_month,lunar_year,lunar_leap,holiday\n2024-02-10,6,1,1,2024,false,Tết\n")
        self.question = {"id": "fixture_q", "business_id": "shop", "question": "Fixture total?", "as_of_date": "2025-01-01"}
        self.config = solver.load_config(Path(solver.__file__).with_name("config.json"))
        self.stores = []

    def finish(self, action, *, question, store, results, source_catalog):
        """Trusted test fixture only; not production diagnostic interpretation."""
        self.stores.append(store)
        args = action["args"]
        self.assertEqual(results[args["check_ref"]]["candidate_ref"], args["result_ref"])
        self.assertEqual(list(store.iter_rows(args["check_ref"])), [("fixture", True)])
        rows = list(store.iter_rows(args["result_ref"]))
        self.assertEqual(rows, [(20.0,)])
        self.assertEqual(len(source_catalog), 1)
        return {"id": question["id"], "type": "number", "answer": rows[0][0]}

    def run_fixture(self, provider, *, finish=True, **kwargs):
        return solver.run_question(self.root, self.question, action_provider=provider,
            token_counter=kwargs.pop("token_counter", lambda messages: 100), config=kwargs.pop("config", self.config),
            finish_handler=self.finish if finish else None, **kwargs)

    def stream(self, actions):
        iterator = iter(actions)
        return lambda messages, first_action: (next(iterator))(messages, first_action)

    def test_five_turn_flow_executes_real_tools_and_returns_fixture_answer(self):
        provider = FlowProvider()
        with patch("solver.load_model", side_effect=AssertionError("Real model loaded")):
            result = self.run_fixture(provider)
        self.assertEqual(result["submission"], {"id": "fixture_q", "type": "number", "answer": 20.0})
        self.assertEqual(result["stop_reason"], "finish_accepted_by_handler")
        self.assertEqual(result["turns_used"], 5)
        self.assertEqual([step["action"]["action"] for step in result["trajectory"]], ["inspect", "load", "sql", "sql", "finish"])
        self.assertTrue(result["finish_handler_used"])
        self.assertEqual(len(result["source_catalog"]), 1)
        self.assertEqual([r["kind"] for r in result["results"]], ["compute", "check"])
        json.dumps(result, allow_nan=False)

    def test_malformed_first_responses_repair_without_losing_task_spec_phase(self):
        provider = FlowProvider(prefix=['broken', '{"action":"shell"}'])
        result = self.run_fixture(provider)
        self.assertEqual(result["turns_used"], 7)
        self.assertEqual([first for _, first in provider.calls[:4]], [True, True, True, False])
        self.assertEqual(result["submission"]["answer"], 20.0)

    def test_tool_error_after_valid_action_advances_phase_and_repairs(self):
        provider = FlowProvider(prefix=[lambda m,f:request("inspect", {"source_ids": ["missing"], "start_row": 1, "row_count": 2}, f)])
        result = self.run_fixture(provider)
        self.assertEqual(result["turns_used"], 6)
        self.assertFalse(provider.calls[1][1])
        self.assertIn("unknown source", result["trajectory"][0]["observation"]["error"]["message"])

    def test_finish_is_accepted_on_eighth_turn(self):
        provider = FlowProvider(prefix=['bad'] * 3)
        result = self.run_fixture(provider)
        self.assertEqual((result["turns_used"], len(provider.calls)), (8, 8))
        self.assertEqual(result["submission"]["type"], "number")

    def test_ninth_provider_invocation_never_occurs(self):
        provider = FlowProvider(prefix=['bad'] * 4)
        result = self.run_fixture(provider)
        self.assertEqual((result["turns_used"], len(provider.calls)), (8, 8))
        self.assertEqual(result["stop_reason"], "turn_limit")
        self.assertEqual(result["submission"], {"id": "fixture_q", "type": "abstain"})

    def test_default_computed_finish_is_rejected_then_abstains(self):
        result = self.run_fixture(FlowProvider(), finish=False)
        self.assertEqual(result["submission"]["type"], "abstain")
        self.assertEqual(result["turns_used"], 6)
        self.assertIn("validation unavailable", result["trajectory"][4]["observation"]["error"]["message"])

    def test_default_unanswerable_finish_is_not_accepted(self):
        provider = self.stream([lambda m,f:request("finish", {"answer_type": "unanswerable", "reason": "missing", "evidence": "fixture only"}, f),
                                lambda m,f:request("finish", {"answer_type": "abstain", "reason": "not implemented"}, f)])
        result = self.run_fixture(provider, finish=False)
        self.assertEqual(result["submission"]["type"], "abstain")
        self.assertEqual(result["turns_used"], 2)

    def test_handler_rejection_can_repair_and_retry_finish(self):
        provider = FlowProvider()
        original = self.finish
        attempts = []
        def handler(action, **kwargs):
            attempts.append(action)
            if len(attempts) == 1:
                raise ValueError("Fixture rejects first finish")
            return original(action, **kwargs)
        # Repeat the same valid finish after the provider's fifth scripted stage.
        native = provider.__call__
        def provide(messages, *, first_action):
            if provider.stage == 5:
                provider.stage = 4
            return native(messages, first_action=first_action)
        result = solver.run_question(self.root, self.question, action_provider=provide, token_counter=lambda _:100,
                                     config=self.config, finish_handler=handler)
        self.assertEqual(result["turns_used"], 6)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(result["submission"]["answer"], 20.0)

    def test_provider_exhaustion_and_fatal_failure_abstain(self):
        for error, reason in [(StopIteration("empty"), "provider_exhausted"), (RuntimeError("failed backend"), "provider_error")]:
            def provider(*args, **kwargs):
                raise error
            with self.subTest(reason=reason):
                result = self.run_fixture(provider)
                self.assertEqual(result["stop_reason"], reason)
                self.assertEqual(result["turns_used"], 1)
                self.assertEqual(result["submission"]["type"], "abstain")

    def test_provider_value_errors_spend_turns_and_repair(self):
        calls = []
        def provider(messages, *, first_action):
            calls.append(first_action)
            if len(calls) == 1:
                raise ValueError("Truncated JSON")
            return request("finish", {"answer_type": "abstain", "reason": "fixture"}, first_action)
        result = self.run_fixture(provider)
        self.assertEqual(calls, [True, True])
        self.assertEqual(result["turns_used"], 2)

    def test_failed_load_does_not_register_or_authorize_tables(self):
        (self.root / "data/shop/bad.csv").write_text("a,b\nwrong\n")
        provider = self.stream([
            lambda m,f:request("load", {"requests": [{"source_ids": ["S002"], "header_rows": [1], "first_data_row": 2},
                                                     {"source_ids": ["S001"], "header_rows": [1], "first_data_row": 2}]}, f),
            lambda m,f:request("sql", {"query": "SELECT count(*) FROM raw_001", "kind": "compute"}, f),
            lambda m,f:request("finish", {"answer_type": "abstain", "reason": "fixture"}, f)])
        result = self.run_fixture(provider)
        self.assertEqual(result["source_catalog"], [])
        self.assertEqual(result["results"], [])
        self.assertIn("not permitted", result["trajectory"][1]["observation"]["error"]["message"])

    def test_unknown_and_check_kind_references_are_rejected_before_sql(self):
        provider = self.stream([
            lambda m,f:request("sql", {"query": "SELECT 1", "kind": "compute"}, f),
            lambda m,f:request("sql", {"query": "SELECT true", "kind": "check", "candidate_ref": state(m)["results"][0]["result_ref"]}, f),
            lambda m,f:request("sql", {"query": "SELECT true", "kind": "check", "candidate_ref": state(m)["results"][1]["result_ref"]}, f),
            lambda m,f:request("sql", {"query": "SELECT true", "kind": "check", "candidate_ref": "other-question"}, f),
            lambda m,f:request("finish", {"answer_type": "abstain", "reason": "fixture"}, f)])
        with patch("solver.execute_sql", wraps=solver.execute_sql) as execute:
            result = self.run_fixture(provider)
        self.assertEqual(execute.call_count, 2)
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["trajectory"][2]["status"], "error")

    def test_sql_permissions_still_apply_in_the_loop(self):
        provider = self.stream([
            lambda m,f:request("sql", {"query": "SELECT * FROM read_text('/etc/passwd')", "kind": "compute"}, f),
            lambda m,f:request("finish", {"answer_type": "abstain", "reason": "fixture"}, f)])
        result = self.run_fixture(provider)
        self.assertIn("not permitted", result["trajectory"][0]["observation"]["error"]["message"])
        self.assertEqual(result["results"], [])

    def test_resources_close_and_questions_never_share_derived_state(self):
        connections = []
        create = solver.create_connection
        def connect(**kwargs):
            connection = create(**kwargs)
            connections.append(connection)
            return connection
        with patch("solver.create_connection", side_effect=connect):
            first = self.run_fixture(FlowProvider())
            second_provider = FlowProvider()
            second = self.run_fixture(second_provider)
        self.assertEqual(len(connections), 2)
        for connection in connections:
            with self.assertRaises(duckdb.Error):
                connection.execute("SELECT 1")
        for store in self.stores:
            self.assertEqual(store.used_bytes, 0)
            with self.assertRaises(ValueError):
                store.describe(first["results"][0]["result_ref"])
        self.assertNotEqual(first["results"][0]["result_ref"], second["results"][0]["result_ref"])
        self.assertEqual(state(second_provider.calls[0][0])["results"], [])
        self.assertEqual(state(second_provider.calls[0][0])["history"], [])

    def test_invalid_question_and_setup_errors_happen_before_provider(self):
        provider = lambda *a,**k:self.fail("Provider invoked")
        for question in ({**self.question, "answer": 20}, {**self.question, "business_id": "../escape"}, {**self.question, "as_of_date": "bad"}):
            with self.subTest(question=question), self.assertRaises(ValueError):
                solver.run_question(self.root, question, action_provider=provider, token_counter=lambda _:100, config=self.config)

    def test_provider_mutations_cannot_modify_host_context(self):
        provider = FlowProvider()
        native = provider.__call__
        def mutate(messages, *, first_action):
            response = native(messages, first_action=first_action)
            messages[1]["content"] = "mutated"
            return response
        self.run_fixture(mutate)
        for messages, _ in provider.calls:
            self.assertIn("Synthetic fixture rules", messages[1]["content"])

    def test_replay_reexecutes_real_tools_and_rebinds_result_references(self):
        first = self.run_fixture(FlowProvider())
        replay = solver.ReplayProvider(first["trajectory"])
        second = self.run_fixture(replay)
        self.assertEqual(first["submission"], second["submission"])
        self.assertNotEqual(first["results"][0]["result_ref"], second["results"][0]["result_ref"])
        finish = second["trajectory"][-1]["action"]["args"]
        self.assertEqual(finish["result_ref"], second["results"][0]["result_ref"])
        self.assertEqual(finish["check_ref"], second["results"][1]["result_ref"])

    def test_replay_preserves_sql_strings_and_rejects_incompatible_results(self):
        first = self.run_fixture(FlowProvider())
        recorded = deepcopy(first["trajectory"])
        recorded[2]["observation"]["row_count"] = 999
        result = self.run_fixture(solver.ReplayProvider(recorded))
        self.assertEqual(result["stop_reason"], "provider_error")
        self.assertIn("Incompatible replay", result["trajectory"][-1]["observation"]["error"]["message"])
        replay = self.run_fixture(solver.ReplayProvider(first["trajectory"]))
        for old, new in zip(first["trajectory"], replay["trajectory"]):
            if (old.get("action") or {}).get("action") == "sql":
                self.assertEqual(old["action"]["args"]["query"], new["action"]["args"]["query"])

    def test_replay_includes_malformed_response_repairs(self):
        first = self.run_fixture(FlowProvider(prefix=['invalid json']))
        second = self.run_fixture(solver.ReplayProvider(first["trajectory"]))
        self.assertEqual(second["submission"], first["submission"])
        self.assertEqual(second["turns_used"], 6)

    def test_replay_never_rewrites_reference_looking_sql_or_purpose_text(self):
        old_action = request("sql", {"query": "SELECT 1", "kind": "compute"}, True)
        follow = request("sql", {"query": "SELECT 'R_old' tag", "kind": "check", "candidate_ref": "R_old"}, False)
        follow["purpose"] = "Keep literal R_old unchanged"
        recorded = [
            {"turn": 1, "first_action": True, "response_received": True, "action": old_action, "status": "ok",
             "observation": {"result_ref": "R_old", "row_count": 1, "schema": []}},
            {"turn": 2, "first_action": False, "response_received": True, "action": follow, "status": "ok", "observation": {}}]
        replay = solver.ReplayProvider(recorded)
        self.assertEqual(replay([{"content": json.dumps({"working_state": {"history": [], "results": []}})}], first_action=True), old_action)
        current = {"history": [{"turn": 1, "action": "sql", "status": "ok", "result_ref": "R_new"}],
                   "results": [{"result_ref": "R_new", "row_count": 1, "schema": []}]}
        output = replay([{"content": json.dumps({"working_state": current})}], first_action=False)
        self.assertEqual(output["args"]["candidate_ref"], "R_new")
        self.assertEqual(output["args"]["query"], "SELECT 'R_old' tag")
        self.assertEqual(output["purpose"], follow["purpose"])

    def test_replay_rejects_unrecorded_provider_failures(self):
        def fail(*args, **kwargs):
            raise RuntimeError("Backend failed before response")
        result = self.run_fixture(fail)
        with self.assertRaises(ValueError):
            solver.ReplayProvider(result["trajectory"])
        with self.assertRaises(ValueError):
            solver.ReplayProvider([])

    def test_bad_handler_record_is_rejected_without_becoming_an_answer(self):
        result = solver.run_question(self.root, self.question, action_provider=FlowProvider(), token_counter=lambda _:100,
            config=self.config, finish_handler=lambda *a,**k:{"id": "wrong_question", "type": "number", "answer": 20})
        self.assertEqual(result["submission"]["type"], "abstain")
        self.assertIn("matching question", result["trajectory"][4]["observation"]["error"]["message"])

    def test_context_overflow_or_bad_counter_prevents_provider_invocation(self):
        for counter, reason in [(lambda _:99999, "context_limit"), (lambda _:True, "context_error")]:
            with self.subTest(reason=reason):
                result = self.run_fixture(lambda *a,**k:self.fail("Provider called"), token_counter=counter)
                self.assertEqual(result["stop_reason"], reason)
                self.assertEqual(result["turns_used"], 0)

    def test_soft_deadline_discards_late_provider_response_in_bounded_process(self):
        code = '''
from pathlib import Path
import json,time
from solver import run_question,load_config
cfg=load_config(Path('config.json'));cfg['timeouts']['question_mac_seconds']=.2
def slow(*a,**k):
    time.sleep(.3)
    return {'action':'finish','purpose':'test','args':{'answer_type':'abstain','reason':'late'}}
result=run_question(Path(__import__('sys').argv[1]),{'id':'q','business_id':'shop','question':'fixture','as_of_date':'2025-01-01'},action_provider=slow,token_counter=lambda _:100,config=cfg)
assert result['stop_reason']=='question_deadline',result
assert result['submission']['type']=='abstain'
print(json.dumps({'deadline':True,'turns':result['turns_used']}))
'''
        result = subprocess.run([sys.executable, "-c", code, str(self.root)], cwd=Path(solver.__file__).parent, capture_output=True, text=True, timeout=6)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["deadline"])


class ContextAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.config = solver.load_config(Path(solver.__file__).with_name("config.json"))
        self.prepared = {"business_id": "shop", "rulebook": "Rulebook\r\n", "business_readme": "README\r\n",
                         "inventory": {}, "calendar": {"table": "calendar"}}
        self.question = {"id": "q", "business_id": "shop", "question": "fixture", "as_of_date": "2025-01-01"}

    def build(self, trajectory, counter, **kwargs):
        return solver.build_run_messages(self.prepared, self.question, first_action=False, task_spec={"metric": "test"},
            tables=kwargs.get("tables", []), results=kwargs.get("results", {}), trajectory=trajectory,
            token_counter=counter, config=self.config)

    def test_drops_old_observations_then_whole_sql_preview_rows(self):
        observation = {"result_ref": "r", "schema": [], "row_count": 100, "preview_rows": [[n] for n in range(12)],
                       "preview_row_count": 12, "preview_truncated": True, "is_preview": True}
        trajectory = [{"turn": n, "status": "ok", "action": request("sql", {"kind": "compute", "query": "SELECT 1"}, False),
                       "observation": deepcopy(observation)} for n in (1, 2)]
        original = deepcopy(trajectory)
        def count(messages):
            observations = state(messages)["observations"]
            return 15300 + 100 * (len(observations) - 1) + 20 * len(observations[-1]["observation"]["preview_rows"])
        messages, tokens, compactions = self.build(trajectory, count)
        self.assertLessEqual(tokens + 1024, 16384)
        self.assertEqual(compactions[0]["kind"], "older_observation_omitted")
        shown = state(messages)["observations"][0]["observation"]
        self.assertEqual(shown["preview_rows"], [[0], [1], [2]])
        self.assertEqual(shown["row_count"], 100)
        self.assertTrue(shown["context_preview_omitted"])
        self.assertEqual(trajectory, original)

    def test_inspection_omissions_keep_source_rows_and_original_pagination(self):
        preview = {"source_id": "S001", "rows": [{"source_row": n, "values": [str(n)]} for n in range(1, 13)],
                   "columns": [{"column_id": "c001"}], "next_start_row": 13, "has_more": True, "requested_rows": 12}
        trajectory = [{"turn": 1, "status": "ok", "action": request("inspect", {"source_ids": ["S001"], "start_row": 1, "row_count": 12}, False),
                       "observation": {"previews": [preview]}}]
        def count(messages):
            return 15300 + 20 * len(state(messages)["observations"][0]["observation"]["previews"][0]["rows"])
        messages, _, _ = self.build(trajectory, count)
        shown = state(messages)["observations"][0]["observation"]["previews"][0]
        self.assertEqual([r["source_row"] for r in shown["rows"]], [1, 2, 3])
        self.assertEqual(shown["context_omitted_source_rows"], list(range(4, 13)))
        self.assertEqual(shown["next_start_row"], 13)
        self.assertTrue(shown["has_more"])

    def test_mandatory_documents_and_schemas_are_never_shortened(self):
        table = {"table": "raw_001", "source_ids": ["S001"], "row_count": 1, "schema": [{"name": "c001", "type": "VARCHAR"}],
                 "columns": [{"column_id": "c001", "header_labels": ['long name' * 1000]}]}
        captured = []
        def count(messages):
            captured.append(deepcopy(messages))
            return 99999
        messages, _, _ = self.build([], count, tables=[table])
        self.assertIsNone(messages)
        self.assertEqual(state(captured[-1])["tables"][0]["columns"], table["columns"])
        payload = json.loads(captured[-1][1]["content"])["reference_data"]
        self.assertEqual(payload["rulebook"], self.prepared["rulebook"])
        self.assertEqual(payload["business_readme"], self.prepared["business_readme"])


if __name__ == "__main__":
    unittest.main()
