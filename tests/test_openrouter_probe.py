from contextlib import closing, redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib import error

import openrouter_probe as probe
import solver
from data_tools import ResultStore, create_connection, execute_sql


def action(first=False, name="finish", args=None):
    value = {"action": name, "purpose": "fixture", "args": args or {"answer_type": "abstain", "reason": "fixture"}}
    if first:
        value["task_spec"] = {"metric": "unknown", "period": "unknown", "grain": "unknown", "unit": "unknown",
                              "expected_answer_type": "number", "relevant_rules": []}
    return value


def response(value=None, *, reason="stop", model=probe.MODEL):
    return {"id": "fixture", "model": model, "provider": "fixture-provider", "usage": {"prompt_tokens": 102, "completion_tokens": 20},
            "choices": [{"message": {"content": json.dumps(value or action())}, "finish_reason": reason}]}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.config = solver.load_config(probe.ROOT / "config.json")
        self.messages = [{"role": "user", "content": '{"working_state":{"history":[]}}'}]
        self.key = "fixture-secret-never-log"

    def provider(self, result, **kwargs):
        stream = io.BytesIO(json.dumps(result).encode())
        opener = Mock(return_value=stream)
        return probe.OpenRouterProvider(self.key, self.config, lambda _:100, deadline=time.monotonic()+30,
                                        opener=opener, **kwargs), opener

    def test_request_auth_model_schema_and_parameters_both_phases(self):
        for first in (True, False):
            with self.subTest(first=first):
                provider, opener = self.provider(response(action(first)))
                self.assertEqual(provider(self.messages, first_action=first), action(first))
                req = opener.call_args.args[0]
                payload = json.loads(req.data)
                self.assertEqual(req.full_url, probe.ENDPOINT)
                self.assertEqual(req.get_header("Authorization"), f"Bearer {self.key}")
                self.assertNotIn(self.key, req.data.decode())
                self.assertEqual(payload["model"], probe.MODEL)
                self.assertEqual(payload["response_format"]["json_schema"]["schema"], probe.wire_action_schema(first))
                self.assertEqual(payload["provider"], {"require_parameters": True})
                self.assertFalse(payload["reasoning"]["enabled"])
                self.assertEqual((payload["temperature"], payload["max_tokens"], payload["seed"]), (0,1024,42))
                self.assertTrue(0 < opener.call_args.kwargs["timeout"] <= 30)
                self.assertEqual(provider.calls[0]["prompt_estimate_difference"], 2)
                self.assertEqual(provider.calls[0]["provider"], "fixture-provider")

    def test_wire_schema_omits_only_uniqueness_without_mutating_host_schema(self):
        def remove_unique(value):
            if isinstance(value, dict):
                return {k:remove_unique(v) for k,v in value.items() if k != "uniqueItems"}
            if isinstance(value, list):
                return [remove_unique(v) for v in value]
            return value
        for first in (True, False):
            original = solver.get_action_schema(first_action=first)
            wire = probe.wire_action_schema(first)
            self.assertEqual(wire, remove_unique(original))
            self.assertEqual(original, solver.get_action_schema(first_action=first))
            self.assertIn('"uniqueItems": true', json.dumps(original))
            self.assertNotIn('"uniqueItems"', json.dumps(wire))

    def test_duplicates_from_transport_schema_are_still_rejected_locally(self):
        duplicate = action(name="inspect", args={"source_ids":["S001","S001"],"start_row":1,"row_count":2})
        provider, _ = self.provider(response(duplicate))
        with self.assertRaisesRegex(ValueError, "unique"):
            provider(self.messages, first_action=False)
        self.assertEqual(provider.calls[0]["host_only_schema_constraints"], ["uniqueItems"])
        self.assertEqual(provider.calls[0]["status"], "failed")

    def test_invalid_or_truncated_actions_keep_metrics(self):
        for content, reason in [('broken','stop'), ('{}','length'), ('{"x":1,"x":2}','stop')]:
            result = response()
            result["choices"][0].update(message={"content": content}, finish_reason=reason)
            provider, opener = self.provider(result)
            with self.subTest(content=content), self.assertRaises(ValueError):
                provider(self.messages, first_action=False)
            self.assertEqual(provider.calls[0]["raw_content"], content)
            self.assertEqual(provider.calls[0]["status"], "failed")
            self.assertEqual(provider.calls[0]["usage"]["prompt_tokens"], 102)
            opener.assert_called_once()

    def test_http_failure_does_not_retry_and_redacts_key(self):
        updates = []
        opener = Mock(side_effect=error.HTTPError(probe.ENDPOINT, 400, "bad", {}, io.BytesIO(f"unsupported schema {self.key}".encode())))
        provider = probe.OpenRouterProvider(self.key, self.config, lambda _:100, deadline=time.monotonic()+30,
                                            opener=opener, checkpoint=updates.append)
        with self.assertRaisesRegex(RuntimeError, "HTTP 400") as failure:
            provider(self.messages, first_action=False)
        self.assertNotIn(self.key, str(failure.exception))
        self.assertNotIn(self.key, json.dumps(updates))
        self.assertIn("unsupported schema", updates[-1][0]["error"])
        self.assertEqual(len(updates), 2)
        opener.assert_called_once()

    def test_timeout_does_not_retry_or_lose_failure_metrics(self):
        provider = probe.OpenRouterProvider(self.key, self.config, lambda _:100, deadline=time.monotonic()+.5,
                                            opener=Mock(side_effect=TimeoutError("fixture socket timeout")))
        with self.assertRaisesRegex(RuntimeError, "socket timeout"):
            provider(self.messages, first_action=False)
        provider.opener.assert_called_once()
        self.assertLessEqual(provider.opener.call_args.kwargs["timeout"], .5)
        self.assertEqual(provider.calls[0]["status"], "failed")

    def test_no_request_after_deadline_or_call_budget(self):
        provider, opener = self.provider(response())
        provider.deadline = time.monotonic()-1
        with self.assertRaisesRegex(RuntimeError, "deadline"):
            provider(self.messages, first_action=False)
        provider.deadline = time.monotonic()+10
        provider.calls = [{}]*8
        with self.assertRaisesRegex(RuntimeError, "budget"):
            provider(self.messages, first_action=False)
        opener.assert_not_called()

    def test_different_model_and_remote_context_overflow_fail(self):
        for result, message in ((response(model="another/model"), "different"), (response(), "budget")):
            if message == "budget":
                result["usage"]["prompt_tokens"] = 16384
            provider, _ = self.provider(result)
            with self.subTest(message=message), self.assertRaisesRegex(RuntimeError, message):
                provider(self.messages, first_action=False)

    def test_partial_state_and_raw_content_are_sanitized_before_checkpoint(self):
        result = response(action(args={"answer_type":"abstain", "reason":self.key}))
        updates = []
        provider, _ = self.provider(result, checkpoint=updates.append)
        provider(self.messages, first_action=False)
        self.assertNotIn(self.key, json.dumps(updates))
        self.assertEqual(updates[0][0]["preceding_working_state"], {"history":[]})

    def test_missing_key_and_redirect_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "OPENROUTER_API_KEY"):
            probe.OpenRouterProvider("", self.config, lambda _:100, deadline=1)
        with self.assertRaises(error.HTTPError):
            probe._NoRedirect().redirect_request(Mock(full_url=probe.ENDPOINT), None, 302, "redirect", {}, "https://example.com")


class HarnessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        business = self.root / "data/shop"
        business.mkdir(parents=True)
        (business / "README.md").write_text("Synthetic fixture")
        (business / "data.csv").write_text("ID,value\n001,12\n002,8\n")
        (self.root / "rulebook.md").write_text("Synthetic fixture rules")
        (self.root / "calendar.csv").write_text("date,weekday,lunar_day,lunar_month,lunar_year,lunar_leap,holiday\n2024-02-10,6,1,1,2024,false,Tết\n")
        self.question = {"id":"q1","business_id":"shop","question":"fixture?","as_of_date":"2025-01-01"}
        (self.root / "questions.jsonl").write_text(json.dumps(self.question))
        (self.root / "answers.jsonl").write_text("MUST NOT READ")
        self.config = solver.load_config(probe.ROOT / "config.json")

    def test_real_tools_and_experimental_callback_complete_five_turns(self):
        def respond(req, **kwargs):
            payload = json.loads(req.data)
            state = json.loads(payload["messages"][-1]["content"])["working_state"]
            step = len(state["history"])
            if step == 0:
                value = action(True, "inspect", {"source_ids":["S001"],"start_row":1,"row_count":12})
            elif step == 1:
                value = action(name="load", args={"requests":[{"source_ids":["S001"],"header_rows":[1],"first_data_row":2}]})
            elif step == 2:
                value = action(name="sql", args={"query":"SELECT sum(CAST(c002 AS BIGINT)) FROM raw_001", "kind":"compute"})
            else:
                candidate = state["results"][0]["result_ref"]
                if step == 3:
                    value = action(name="sql",args={"query":"SELECT 'fixture' check_name, true passed", "kind":"check", "candidate_ref":candidate})
                else:
                    value = action(args={"answer_type":"number","layout":"scalar","columns":[1],"result_ref":candidate,"check_ref":state["results"][1]["result_ref"]})
            return io.BytesIO(json.dumps(response(value)).encode())
        provider = probe.OpenRouterProvider("fixture-key", self.config, lambda _:100, deadline=time.monotonic()+20, opener=respond)
        result = solver.run_question(self.root, self.question, action_provider=provider, token_counter=lambda _:100,
                                     config=self.config, finish_handler=probe.experimental_finish)
        self.assertEqual(result["submission"], {"id":"q1","type":"number","answer":20})
        self.assertEqual(result["turns_used"], 5)
        self.assertTrue(result["finish_handler_used"])
        self.assertEqual(len(provider.calls), 5)

    def test_numeric_projection_and_check_binding_failures(self):
        with closing(create_connection()) as connection, ResultStore("q1") as store:
            candidate = execute_sql(connection, "SELECT 20 value", store=store, allowed_tables=set(), purpose="fixture")
            check = execute_sql(connection, "SELECT true passed", store=store, allowed_tables=set(), purpose="fixture")
            ref, check_ref = candidate["result_ref"], check["result_ref"]
            roles = {ref:{"kind":"compute"}, check_ref:{"kind":"check","candidate_ref":ref}}
            value = action(args={"answer_type":"number","layout":"scalar","columns":[1],"result_ref":ref,"check_ref":check_ref})
            finish = lambda a,r:probe.experimental_finish(a, question=self.question, store=store, results=r, source_catalog=[])
            self.assertEqual(finish(value, roles)["answer"], 20)
            percent = deepcopy(value); percent["args"]["answer_type"] = "percent"
            self.assertEqual(finish(percent, roles), {"id":"q1", "type":"percent", "answer":20})
            bad = deepcopy(roles); bad[check_ref]["candidate_ref"] = "other"
            with self.assertRaisesRegex(ValueError, "bound"):
                finish(value, bad)
            for sql in ("SELECT true", "SELECT NULL", "SELECT '20'", "SELECT 'NaN'::DOUBLE", "SELECT 1 WHERE false", "SELECT * FROM range(2)"):
                other = execute_sql(connection, sql, store=store, allowed_tables=set(), purpose="fixture")["result_ref"]
                role = {other:{"kind":"compute"}, check_ref:{"kind":"check","candidate_ref":other}}
                bad_action = deepcopy(value); bad_action["args"]["result_ref"] = other
                with self.subTest(sql=sql), self.assertRaises(ValueError):
                    finish(bad_action, role)
            for args in ({"answer_type":"unanswerable"}, {**value["args"],"columns":[0]}):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    finish(action(args=args), roles)

    def test_worker_vocabulary_only_cleanup_and_partial_failure_evidence(self):
        tokenizer = Mock()
        progress = self.root / "progress.json"
        def fail(*args, **kwargs):
            provider = kwargs["action_provider"]
            provider.calls.append({"status":"running", "preceding_working_state":{"history":[{"turn":1}]}})
            provider._checkpoint()
            raise RuntimeError("fixture failure")
        with patch("solver.verify_model_artifact", return_value=Path("fixture.gguf")), patch("llama_cpp.Llama", return_value=tokenizer) as load, \
             patch("solver.run_question", side_effect=fail), patch("solver.load_model", side_effect=AssertionError("Full model loaded")):
            result = probe.run_probe(self.root, self.question, self.config, "fixture-key", progress)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["calls"][0]["preceding_working_state"]["history"], [{"turn":1}])
        self.assertTrue(load.call_args.kwargs["vocab_only"])
        self.assertEqual(load.call_args.kwargs["n_gpu_layers"], 0)
        tokenizer.close.assert_called_once()
        self.assertNotIn("answers.jsonl", result["input_sha256"])
        self.assertEqual(json.loads(progress.read_text()), result)

    def test_cli_missing_key_and_invalid_question_before_initialization(self):
        with patch.dict(os.environ, {}, clear=True), patch("solver.verify_model_artifact", side_effect=AssertionError("Weights read")), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(probe.main(["--question-id","q1","--split-root",str(self.root)]), 1)
        self.assertIn("OPENROUTER_API_KEY", err.getvalue())
        with patch.dict(os.environ, {}, clear=True), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(probe.main(["--question-id","missing","--split-root",str(self.root)]), 1)
        self.assertIn("Question ID", err.getvalue())

    def test_worker_surfaces_provider_failure_in_report(self):
        outcome = {"stop_reason":"provider_error", "submission":{"id":"q1","type":"abstain"},
                   "trajectory":[{"observation":{"error":{"message":"OpenRouter HTTP 401: invalid key"}}}]}
        with patch("solver.verify_model_artifact", return_value=Path("fixture.gguf")), \
             patch("llama_cpp.Llama", return_value=Mock()), patch("solver.run_question", return_value=outcome):
            report = probe.run_probe(self.root, self.question, self.config, "fixture-key", self.root / "progress.json")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["error"], "OpenRouter HTTP 401: invalid key")

    def test_cli_parent_watchdogs_outputs_and_secret_redaction(self):
        output = self.root / "run"
        result = {"status":"experimental_answer", "outcome":{"submission":{"id":"q1","type":"number","answer":20},
                  "stop_reason":"finish_accepted_by_handler", "turns_used":5},"total_seconds":1, "error":"fixture-key"}
        with patch.dict(os.environ, {"OPENROUTER_API_KEY":"fixture-key"}), \
             patch("solver._run_feasibility_worker", return_value=(result,"fixture-key log")) as run, redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(probe.main(["--question-id","q1","--split-root",str(self.root),"--output-dir",str(output)]), 0)
        self.assertEqual(run.call_args.kwargs, {"overall_seconds":600,"request_seconds":300,"initialization_seconds":300})
        self.assertNotIn("fixture-key", stdout.getvalue())
        self.assertNotIn("fixture-key", (output / "report.json").read_text())
        self.assertNotIn("fixture-key", (output / "runtime.log").read_text())
        with patch.dict(os.environ, {"OPENROUTER_API_KEY":"fixture-key"}), redirect_stderr(io.StringIO()):
            self.assertEqual(probe.main(["--question-id","q1","--split-root",str(self.root),"--output-dir",str(output)]), 1)


if __name__ == "__main__":
    unittest.main()
