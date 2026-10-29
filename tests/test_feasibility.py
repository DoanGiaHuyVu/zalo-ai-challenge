import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

import solver


class FeasibilityTests(unittest.TestCase):
    def test_exact_budget_boundaries(self):
        self.assertTrue(solver.context_budget(12288, 16384, 1024)["reserved_fits"])
        self.assertFalse(solver.context_budget(12289, 16384, 1024)["reserved_fits"])
        boundary = solver.context_budget(15360, 16384, 1024)
        self.assertTrue(boundary["mandatory_fits"])
        self.assertEqual(boundary["mandatory_remaining"], 0)
        self.assertFalse(solver.context_budget(15361, 16384, 1024)["mandatory_fits"])

    def test_counting_reuses_generation_formatter_and_tokenization_flags(self):
        template = "{{ messages[-1].content }}{{ '<think>\\n\\n</think>\\n\\n' if enable_thinking == false else '<think>' }}"
        model = SimpleNamespace(metadata={"tokenizer.chat_template": template}, token_eos=lambda: 1, token_bos=lambda: -1, detokenize=lambda tokens, **kwargs: b"<|im_end|>", tokenize=Mock(return_value=list(range(17))))
        solver._non_thinking_handler(model)
        formatter = model._datamate_formatter
        messages = [{"role": "user", "content": "Cà phê /think"}]
        self.assertEqual(solver.count_prompt_tokens(model, messages), 17)
        self.assertIs(model._datamate_formatter, formatter)
        model.tokenize.assert_called_once_with(("Cà phê /think" + solver.NON_THINKING_SUFFIX).encode(), add_bos=False, special=True)

    def test_documents_and_question_are_not_truncated(self):
        rulebook = "Quy tắc\n" * 4000
        readme = "Tên hàng hoá đầy đủ\n" * 2000
        question = {"id": "q", "business_id": "shop", "as_of_date": "2026-09-01", "question": "Doanh thu?"}
        messages = solver._probe_messages(rulebook, readme, [{"file": "dữ liệu.xlsx", "sheets": ["Tháng 1"]}], question)
        content = messages[1]["content"]
        self.assertIn(rulebook, content)
        self.assertIn(readme, content)
        self.assertIn(json.dumps(question, ensure_ascii=False), content)
        self.assertIn("Tháng 1", content)
        self.assertIn("Do not", messages[0]["content"])

    def test_memory_units_are_platform_specific(self):
        self.assertEqual(solver._peak_rss_bytes(2048, "darwin"), 2048)
        self.assertEqual(solver._peak_rss_bytes(2048, "linux"), 2097152)
        with self.assertRaises(ValueError):
            solver._peak_rss_bytes(1, "unknown")

    def test_system_swap_growth_is_a_concern_without_model_attribution(self):
        before = {"swap": {"raw": "total = 2048.00M  used = 1024.00M free = 1024.00M"}, "memory_pressure": {"raw": "System-wide memory free percentage: 44%"}}
        after = {"swap": {"raw": "total = 4096.00M  used = 3072.00M free = 1024.00M"}, "memory_pressure": {"raw": "System-wide memory free percentage: 21%"}}
        summary, warnings = solver._memory_environment(before, after)
        self.assertEqual(summary["system_swap_delta_bytes"], 2 * 1024**3)
        self.assertEqual(summary["system_memory_free_percent_after"], 21)
        self.assertEqual(len(warnings), 1)
        self.assertIn("not a model-only attribution", warnings[0])
        self.assertEqual(solver._memory_environment(before, before)[1], [])
        self.assertIsNone(solver._memory_environment({}, {})[0]["system_swap_delta_bytes"])

    def test_inventory_reads_metadata_without_sheet_cells_or_table_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "sales.csv").write_bytes(b"\xffnot even decodable cell data")
            with zipfile.ZipFile(root / "monthly.xlsx", "w") as archive:
                archive.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheets><sheet name="T01" sheetId="1"/></sheets></workbook>')
            with sqlite3.connect(root / "shop.sqlite") as database:
                database.execute("CREATE TABLE sales(secret TEXT)")
                database.execute("INSERT INTO sales VALUES ('must not be read')")
            inventory = {item["file"]: item for item in solver._inventory_names(root)}
            self.assertEqual(inventory["monthly.xlsx"]["sheets"], ["T01"])
            self.assertEqual(inventory["shop.sqlite"]["tables"], ["sales"])
            self.assertNotIn("must not be read", json.dumps(inventory))

    def test_classification_distinguishes_failure_and_concerns(self):
        report = {"scan_complete": True, "probes": [{"status": "passed"}] * 8, "warnings": [], "overflows": []}
        self.assertEqual(solver._classify_feasibility(report), "passed")
        report["overflows"] = [{"mandatory_fits": True}]
        self.assertEqual(solver._classify_feasibility(report), "passed_with_concerns")
        report["overflows"] = [{"mandatory_fits": False}]
        self.assertEqual(solver._classify_feasibility(report), "failed")
        report["overflows"] = []
        report["warnings"] = ["measurement unavailable"]
        self.assertEqual(solver._classify_feasibility(report), "passed_with_concerns")
        report["probes"] = report["probes"][:7]
        self.assertEqual(solver._classify_feasibility(report), "failed")

    def test_native_buffers_remain_separate_and_use_peak_per_label(self):
        log = "load_tensors: MTL0_Mapped model buffer size = 4.00 MiB\nsched_reserve: MTL0 compute buffer size = 1.00 MiB\nsched_reserve: MTL0 compute buffer size = 2.00 MiB"
        self.assertEqual(solver._metal_allocations(log), {"MTL0_Mapped model": 4194304, "MTL0 compute": 2097152})

    def test_request_timeout_preserves_completed_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            progress = Path(temporary) / "progress.json"
            code = "import json,pathlib,time,sys; pathlib.Path(sys.argv[1]).write_text(json.dumps({'scan_complete':True,'probes':[{'status':'passed'},{'status':'running'}],'active_request_started':time.monotonic()})); time.sleep(10)"
            started = time.monotonic()
            report, _ = solver._run_feasibility_worker([sys.executable, "-c", code, str(progress)], progress, overall_seconds=5, request_seconds=.05)
            self.assertLess(time.monotonic() - started, 4)
            self.assertEqual(report["status"], "failed")
            self.assertTrue(report["partial_results"])
            self.assertEqual([p["status"] for p in report["probes"]], ["passed", "failed"])
            self.assertIn("Diagnostic request", report["error"])

    def test_overall_timeout_preserves_last_atomic_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            progress = Path(temporary) / "progress.json"
            solver._save_progress(progress, {"scan_complete": False, "businesses": [{"business_id": "completed"}]})
            report, _ = solver._run_feasibility_worker([sys.executable, "-c", "import time; time.sleep(10)"], progress, overall_seconds=.1)
            self.assertEqual(report["status"], "failed")
            self.assertTrue(report["partial_results"])
            self.assertEqual(report["businesses"][0]["business_id"], "completed")
            self.assertIn("worker exceeded", report["error"])

    def test_scan_covers_both_splits_but_generates_only_development_probes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = solver.load_config(Path(__file__).resolve().parents[1] / "config.json")
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config))
            for split, businesses in [("train", [("biz_101", 10), ("biz_102", 20), ("biz_103", 30), ("biz_107", 100)]), ("public_test", [("dev_001", 200)])]:
                (root / split / "data").mkdir(parents=True)
                (root / split / "rulebook.md").write_text(split + " rules")
                questions = []
                for business_id, size in businesses:
                    directory = root / split / "data" / business_id
                    directory.mkdir()
                    (directory / "README.md").write_text(business_id * size)
                    (directory / "sales.csv").write_text("header\nprivate rows\n")
                    questions.append({"id": business_id + "_q", "business_id": business_id, "question": "Do not answer this", "as_of_date": "2026-09-01"})
                (root / split / "questions.jsonl").write_text("\n".join(json.dumps(q) for q in questions))
            model = SimpleNamespace(reset=Mock(), close=Mock())
            counter = lambda model, messages: sum(len(m["content"]) for m in messages)

            def generate(model, messages, schema, config, *, metrics):
                self.assertNotIn("biz_107", messages[1]["content"])
                self.assertNotIn("dev_001", messages[1]["content"])
                metrics.update(usage={"prompt_tokens": counter(model, messages), "completion_tokens": 5}, generation_seconds=.01, finish_reason="stop")
                return {"ones": [1] * 128} if schema is solver.ONES_SCHEMA else {"ok": True, "language": "vi"}

            with patch("solver.load_model", return_value=model), patch("solver.count_prompt_tokens", side_effect=counter), patch("solver.generate_json", side_effect=generate), patch("solver._memory_snapshot", return_value={"peak_process_rss_bytes": 1024}):
                report = solver._feasibility(config, config_path, root / "progress.json")
            self.assertEqual(report["status"], "passed")
            self.assertEqual(len(report["businesses"]), 5)
            self.assertEqual(report["selected_businesses"], ["biz_103", "biz_102", "biz_101"])
            self.assertEqual(len(report["probes"]), 8)
            self.assertEqual(sum(p["kind"] == "128_ones" for p in report["probes"]), 1)
            self.assertEqual(model.reset.call_count, 8)
            model.close.assert_called_once()
            self.assertIn("public_test/questions.jsonl", report["input_hashes"])


if __name__ == "__main__":
    unittest.main()
