from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import solver


class LocalProviderTests(unittest.TestCase):
    def setUp(self):
        self.config = solver.load_config(Path(solver.__file__).with_name("config.json"))
        self.messages = [{"role": "user", "content": '{"working_state":{"results":[]}}'}]

    def candidate(self, first):
        value = {"action": "finish", "purpose": "fixture", "args": {"answer_type": "abstain", "reason": "fixture"}}
        if first:
            value["task_spec"] = {"metric": "unknown", "period": "unknown", "grain": "unknown", "unit": "unknown",
                                  "expected_answer_type": "number", "relevant_rules": []}
        return value

    def test_schema_phases_metrics_same_model_and_checkpoints(self):
        responses = [self.candidate(True), self.candidate(False)]
        model = SimpleNamespace(create_chat_completion=Mock(side_effect=[{"choices": [{"message": {"content": json.dumps(action)}, "finish_reason": "stop"}],
                                                                          "usage": {"prompt_tokens": 100, "completion_tokens": 20}} for action in responses]))
        updates = []
        provider = solver.LocalActionProvider(model, self.config, progress_callback=updates.append)
        with patch("solver.count_prompt_tokens", return_value=100) as count:
            for first, expected in zip((True, False), responses):
                self.assertEqual(provider(self.messages, first_action=first), expected)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual([u["status"] for u in updates], ["running", "passed", "running", "passed"])
        self.assertTrue(all(call["prompt_tokens_match"] for call in provider.calls))
        self.assertEqual(provider.calls[0]["preceding_working_state"], {"results": []})
        self.assertTrue(all(call.args[0] is model for call in count.call_args_list))
        for first, call in zip((True, False), model.create_chat_completion.call_args_list):
            self.assertEqual(call.kwargs["response_format"]["schema"], solver.get_action_schema(first_action=first))
            self.assertEqual((call.kwargs["temperature"], call.kwargs["max_tokens"]), (0, 1024))

    def test_truncation_malformed_output_and_failed_metrics_are_preserved(self):
        for content, reason in [('broken', 'stop'), ('{}', 'length'), ('{"x":1,"x":2}', 'stop')]:
            model = SimpleNamespace(create_chat_completion=lambda **kw: {"choices": [{"message": {"content": content}, "finish_reason": reason}],
                                                                          "usage": {"prompt_tokens": 100, "completion_tokens": 10}})
            provider = solver.LocalActionProvider(model, self.config)
            with self.subTest(content=content), patch("solver.count_prompt_tokens", return_value=100), self.assertRaises(ValueError):
                provider(self.messages, first_action=False)
            self.assertEqual(provider.calls[0]["status"], "failed")
            self.assertEqual(provider.calls[0]["raw_content"], content)
            self.assertEqual(provider.calls[0]["finish_reason"], reason)
            self.assertTrue(provider.calls[0]["prompt_tokens_match"])

    def test_token_mismatch_and_missing_usage_are_not_accepted(self):
        for usage in ({"prompt_tokens": 99}, {}):
            model = SimpleNamespace(create_chat_completion=lambda **kw: {"choices": [{"message": {"content": json.dumps(self.candidate(False))}, "finish_reason": "stop"}], "usage": usage})
            provider = solver.LocalActionProvider(model, self.config)
            with self.subTest(usage=usage), patch("solver.count_prompt_tokens", return_value=100), self.assertRaisesRegex(ValueError, "prompt usage"):
                provider(self.messages, first_action=False)
            self.assertFalse(provider.calls[0]["prompt_tokens_match"])


class ProbeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        business = self.root / "data/shop"
        business.mkdir(parents=True)
        (business / "README.md").write_text("Synthetic fixture")
        (business / "data.csv").write_text("ID,value\n1,2\n")
        (self.root / "rulebook.md").write_text("Fixture rules")
        (self.root / "calendar.csv").write_text("date,weekday,lunar_day,lunar_month,lunar_year,lunar_leap,holiday\n2024-02-10,6,1,1,2024,false,Tết\n")
        self.question = {"id": "q1", "business_id": "shop", "question": "fixture?", "as_of_date": "2025-01-01"}
        (self.root / "questions.jsonl").write_text(json.dumps(self.question))
        (self.root / "answers.jsonl").write_text("MUST NOT READ")
        self.config = solver.load_config(Path(solver.__file__).with_name("config.json"))
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(self.config))
        self.progress = self.root / "progress.json"

    def test_selection_missing_id_and_validation_business(self):
        self.assertEqual(solver._select_probe_question(self.root, "q1")[1], self.question)
        with self.assertRaises(ValueError):
            solver._select_probe_question(self.root, "missing")
        with patch("solver._load_split_questions", return_value=(self.root, [{**self.question, "business_id": "biz_107"}])):
            with self.assertRaisesRegex(ValueError, "validation business"):
                solver._select_probe_question(self.root, "q1")

    def test_worker_loads_once_closes_model_and_never_installs_finisher(self):
        model = Mock()
        outcome = {"stop_reason": "abstain", "trajectory": [], "submission": {"id": "q1", "type": "abstain"}}
        def run(*args, **kwargs):
            self.assertIsNone(kwargs["finish_handler"])
            self.assertIsInstance(kwargs["action_provider"], solver.LocalActionProvider)
            kwargs["action_provider"].calls.append({"prompt_tokens_match": True})
            return outcome
        stages = {name: True for name in ("inspection", "loading", "business_compute", "abstain_action")}
        with patch("solver.load_model", return_value=model) as load, patch("solver.run_question", side_effect=run), \
                patch("solver._probe_stages", return_value=(stages, [])):
            report = solver._model_probe(self.root, self.question, self.config, self.progress)
        self.assertEqual(report["status"], "passed")
        load.assert_called_once()
        model.close.assert_called_once()
        self.assertFalse(report["finish_handler_used"])
        self.assertNotIn("answers.jsonl", report["input_sha256"])
        self.assertEqual(report, json.loads(self.progress.read_text()))

    def test_worker_failure_preserves_progress_and_closes_model(self):
        model = Mock()
        with patch("solver.load_model", return_value=model), patch("solver.run_question", side_effect=RuntimeError("fixture failure")):
            report = solver._model_probe(self.root, self.question, self.config, self.progress)
        self.assertEqual(report["status"], "failed")
        self.assertIn("fixture failure", report["error"])
        model.close.assert_called_once()
        with patch("solver.load_model", side_effect=RuntimeError("initialization failed")):
            failed = solver._model_probe(self.root, self.question, self.config, self.progress)
        self.assertEqual(failed["phase"], "complete")
        self.assertEqual(failed["calls"], [])

    def test_constant_select_does_not_establish_business_computation(self):
        base = [{"status": "ok", "turn": 1, "action": {"action": "inspect", "args": {}}, "observation": {}},
                {"status": "ok", "turn": 2, "action": {"action": "load", "args": {}}, "observation": {"tables": [{"table": "raw_001"}]}}]
        for query, expected in (("SELECT 1", False), ("SELECT sum(c001) FROM raw_001", True),
                                ("WITH raw_001 AS (SELECT 1) SELECT * FROM raw_001", False)):
            trajectory = deepcopy(base) + [{"status": "ok", "turn": 3, "action": {"action": "sql", "args": {"query": query, "kind": "compute"}}}]
            with self.subTest(query=query):
                stages, _ = solver._probe_stages({"trajectory": trajectory})
                self.assertEqual(stages["business_compute"], expected)

    def test_inspection_does_not_load_and_sql_failure_supplies_repair_guidance(self):
        calls = []
        def provider(messages, *, first_action):
            calls.append(messages)
            envelope = {"action": "inspect", "purpose": "fixture", "args": {"source_ids": ["S001"], "start_row": 1, "row_count": 2}}
            if first_action:
                envelope["task_spec"] = {"metric": "unknown", "period": "unknown", "grain": "unknown", "unit": "unknown",
                                         "expected_answer_type": "number", "relevant_rules": []}
            if len(calls) == 2:
                envelope.update(action="sql", args={"query": "SELECT count(*) FROM data", "kind": "compute"})
            if len(calls) == 3:
                current = json.loads(messages[-1]["content"])["working_state"]
                self.assertEqual(current["tables"], [])
                error = current["observations"][-1]["observation"]["error"]["message"]
                self.assertIn("Use load", error)
                self.assertIn("preview only", error)
                envelope.update(action="finish", args={"answer_type": "abstain", "reason": "fixture"})
            return envelope
        result = solver.run_question(self.root, self.question, action_provider=provider, token_counter=lambda _:100, config=self.config)
        self.assertEqual(result["turns_used"], 3)
        self.assertEqual(result["source_catalog"], [])

    def test_cli_validates_question_before_config_or_model_initialization(self):
        argv = ["solver.py", "--model-probe", "missing", "--split-root", str(self.root), "--config", str(self.root / "missing.json")]
        with patch.object(sys, "argv", argv), patch("solver.load_config", side_effect=AssertionError("Config read")), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(solver.main(), 1)
        self.assertIn("Question ID", errors.getvalue())

    def test_cli_watchdog_gpu_evidence_and_previous_attempt_archival(self):
        directory = self.root / "artifacts"
        directory.mkdir()
        (directory / "t16-probe.json").write_text('{"old":"preserve"}')
        argv = ["solver.py", "--model-probe", "q1", "--split-root", str(self.root), "--config", str(self.config_path)]
        with patch.object(sys, "argv", argv), patch("solver._run_feasibility_worker", return_value=({"status": "passed"}, "offloaded 37/37 layers to GPU\nMetal")) as run, redirect_stdout(io.StringIO()):
            self.assertEqual(solver.main(), 0)
        self.assertEqual(run.call_args.kwargs, {"overall_seconds": 600, "request_seconds": 300, "initialization_seconds": 300})
        self.assertEqual(len(list((directory / "t16-attempts").glob('*/t16-probe.json'))), 1)
        report = json.loads((directory / "t16-probe.json").read_text())
        self.assertEqual(report["offloaded_layers"], 37)

    def test_cli_invalid_flag_combinations(self):
        for args in (["--model-probe", "q1"], ["--smoke", "--split-root", str(self.root)],
                     ["--model-probe", "q1", "--split-root", str(self.root), "--source-id", "S001"],
                     ["--model-probe", "q1", "--split-root", str(self.root), "--smoke"]):
            with self.subTest(args=args), patch.object(sys, "argv", ["solver.py", *args]), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                solver.main()
            self.assertEqual(error.exception.code, 2)

    def test_watchdog_preserves_partial_initialization_and_question_results(self):
        for phase in ("initializing", "question"):
            partial = {"status": "running", "phase": phase, "active_request_started": 0,
                       "calls": [{"status": "running", "preceding_working_state": {"history": [{"turn": 1, "action": "load", "status": "ok"}]}}]}
            self.progress.write_text(json.dumps(partial))
            process = Mock()
            process.communicate.side_effect = [subprocess.TimeoutExpired("fake", 1), ('', 'partial native log')]
            context = Mock()
            context.__enter__ = Mock(return_value=process)
            context.__exit__ = Mock(return_value=False)
            with self.subTest(phase=phase), patch("solver.subprocess.Popen", return_value=context), \
                    patch("solver.time.monotonic", side_effect=[0, 301, 302]):
                report, log = solver._run_feasibility_worker(['fake'], self.progress, overall_seconds=600, request_seconds=300, initialization_seconds=300)
            process.kill.assert_called_once()
            self.assertEqual(report["status"], "failed")
            self.assertTrue(report["partial_results"])
            self.assertEqual(report["calls"], partial["calls"])
            self.assertIn('native log', log)


if __name__ == "__main__":
    unittest.main()
