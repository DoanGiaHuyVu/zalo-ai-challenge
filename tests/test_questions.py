import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import solver


SOLVER_PATH = Path(solver.__file__).resolve()


class QuestionInputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        business = self.data / "cửa_hàng"
        business.mkdir(parents=True)
        (business / "README.md").write_text("Quy tắc cửa hàng", encoding="utf-8")
        # Invalid workbook bytes deliberately ensure validation never loads rows.
        (business / "sales.xlsx").write_bytes(b"not a workbook")
        self.path = self.root / "questions.jsonl"
        self.record = {
            "id": "0001",
            "business_id": "cửa_hàng",
            "question": "  Ngày 29/2 bán được bao nhiêu ly cà phê?\nTính cả trà.  ",
            "as_of_date": "2024-02-29",
        }

    def write_records(self, records):
        self.path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")

    def assert_input_error(self, text, message, line=1):
        self.path.write_text(text, encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            solver.load_questions(self.path, self.data)
        self.assertIn(f"{self.path}:{line}:", str(caught.exception))
        self.assertIn(message, str(caught.exception))

    def test_preserves_vietnamese_text_order_ids_and_dates(self):
        future = {**self.record, "id": "0002", "as_of_date": "2099-01-01"}
        records = [future, self.record]
        self.write_records(records)
        self.assertEqual(solver.load_questions(self.path, self.data), records)
        self.assertEqual(solver.load_questions(self.path, self.data), records)

    def test_rejects_missing_and_unexpected_fields(self):
        for field in self.record:
            with self.subTest(missing=field):
                record = dict(self.record)
                del record[field]
                self.assert_input_error(json.dumps(record), f"missing fields: {field}")
        for field in ("answer", "type", "alias"):
            with self.subTest(unexpected=field):
                self.assert_input_error(json.dumps({**self.record, field: "gold"}), f"unexpected fields: {field}")

    def test_rejects_empty_or_nonstring_fields(self):
        for field in self.record:
            for value in ("", " \t\n", None, 123, True, [], {}):
                with self.subTest(field=field, value=value):
                    self.assert_input_error(json.dumps({**self.record, field: value}), f"{field}: expected a nonempty string")

    def test_rejects_malformed_nonobject_and_nonfinite_json(self):
        for text, message in (("{", "Expecting property name"), ("[]", "Expected a JSON object"),
                              ("null", "Expected a JSON object"), ('"text"', "Expected a JSON object"),
                              ("{} {}", "Extra data"), ('{"id": NaN}', "Nonfinite JSON value")):
            with self.subTest(text=text):
                self.assert_input_error(text, message)

    def test_rejects_duplicate_json_keys(self):
        text = json.dumps(self.record)[:-1] + ', "id": "replacement"}'
        self.assert_input_error(text, "Duplicate JSON key: 'id'")

    def test_rejects_empty_files_and_blank_records(self):
        for text, message, line in (("", "Empty question file", 1), ("\n", "Blank record", 1),
                                    (json.dumps(self.record) + "\n\n", "Blank record", 2)):
            with self.subTest(text=text):
                self.assert_input_error(text, message, line)

    def test_duplicate_ids_report_both_line_numbers(self):
        self.write_records([self.record, self.record])
        with self.assertRaises(ValueError) as caught:
            solver.load_questions(self.path, self.data)
        self.assertIn(f"{self.path}:2:", str(caught.exception))
        self.assertIn("id: duplicate '0001'; first seen on line 1", str(caught.exception))

    def test_stops_at_first_invalid_record(self):
        text = json.dumps(self.record) + '\n{"id": 2}\nnot json\n'
        self.assert_input_error(text, "missing fields:", 2)

    def test_rejects_impossible_or_noncanonical_dates(self):
        for value in ("2023-02-29", "2026-04-31", "0000-01-01", "2026-13-01"):
            with self.subTest(value=value):
                self.assert_input_error(json.dumps({**self.record, "as_of_date": value}), "invalid calendar date")
        for value in ("20260228", "2026-2-28", "28/02/2026", "2026-02-28T00:00:00", "2026-02-28 "):
            with self.subTest(value=value):
                self.assert_input_error(json.dumps({**self.record, "as_of_date": value}), "expected YYYY-MM-DD")

    def test_rejects_missing_business_or_readme(self):
        self.assert_input_error(json.dumps({**self.record, "business_id": "missing"}), "directory does not exist")
        (self.data / "cửa_hàng" / "README.md").unlink()
        self.assert_input_error(json.dumps(self.record), "README.md does not exist")

    def test_rejects_path_traversal_and_absolute_paths(self):
        for business in (".", "..", "../outside", "/tmp", "folder/child", "folder\\child", "bad\x00name"):
            with self.subTest(business=business):
                self.assert_input_error(json.dumps({**self.record, "business_id": business}), "business_id: expected a single directory name")

    def test_rejects_business_and_readme_symlinks_escaping_root(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "README.md").write_text("Outside")
        (self.data / "escaped").symlink_to(outside, target_is_directory=True)
        self.assert_input_error(json.dumps({**self.record, "business_id": "escaped"}), "resolved directory escapes data root")
        readme = self.data / "cửa_hàng" / "README.md"
        readme.unlink()
        readme.symlink_to(outside / "README.md")
        self.assert_input_error(json.dumps(self.record), "README.md resolves outside data root")

    def test_filesystem_and_encoding_errors_include_location(self):
        with self.assertRaisesRegex(ValueError, "questions.jsonl:1: cannot read questions"):
            solver.load_questions(self.path, self.data)
        self.write_records([self.record])
        with self.assertRaisesRegex(ValueError, "questions.jsonl:1: Data root is not a directory"):
            solver.load_questions(self.path, self.root / "absent")
        self.path.write_bytes(json.dumps(self.record).encode() + b"\n\xff\n")
        with self.assertRaisesRegex(ValueError, "questions.jsonl:2:.*utf-8"):
            solver.load_questions(self.path, self.data)

    def test_validation_mode_skips_config_model_and_artifact_creation(self):
        for valid in (True, False):
            with self.subTest(valid=valid):
                self.path.write_text(json.dumps(self.record) if valid else "[]", encoding="utf-8")
                stdout, stderr = io.StringIO(), io.StringIO()
                argv = ["solver.py", "--validate-questions", str(self.path), "--data-root", str(self.data)]
                with patch.object(sys, "argv", argv), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                        patch("solver.load_config", side_effect=AssertionError("Config loaded")), \
                        patch("solver.load_model", side_effect=AssertionError("Model loaded")):
                    code = solver.main()
                self.assertEqual(code, 0 if valid else 1)
                if valid:
                    self.assertEqual(json.loads(stdout.getvalue()), {"status": "passed", "question_count": 1, "business_count": 1})
                    self.assertEqual(stderr.getvalue(), "")
                else:
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertIn(f"{self.path}:1:", stderr.getvalue())
        self.assertFalse((self.root / "artifacts").exists())

    def test_cli_runs_without_config_and_reports_input_failures(self):
        self.write_records([self.record])
        command = [sys.executable, str(SOLVER_PATH), "--config", str(self.root / "missing.json"),
                   "--validate-questions", str(self.path), "--data-root", str(self.data)]
        passed = subprocess.run(command, capture_output=True, text=True, timeout=10, cwd=self.root)
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertEqual(json.loads(passed.stdout)["question_count"], 1)
        self.path.write_text("[]")
        failed = subprocess.run(command, capture_output=True, text=True, timeout=10, cwd=self.root)
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(failed.stdout, "")
        self.assertIn("Expected a JSON object", failed.stderr)

    def test_cli_requires_paired_arguments_and_exclusive_modes(self):
        for args in (["--validate-questions", str(self.path)], ["--smoke", "--data-root", str(self.data)],
                     ["--validate-questions", str(self.path), "--data-root", str(self.data), "--smoke"]):
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, str(SOLVER_PATH), *args], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("error:", result.stderr)


if __name__ == "__main__":
    unittest.main()
