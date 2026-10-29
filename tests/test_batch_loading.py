from contextlib import closing, redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import duckdb
from openpyxl import Workbook
from openpyxl.utils.datetime import CALENDAR_MAC_1904

import data_tools
import solver


class BatchLoadingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.connection = duckdb.connect(":memory:", config={"enable_external_access": False,
            "autoload_known_extensions": False, "autoinstall_known_extensions": False,
            "memory_limit": "1GB", "threads": 2})
        self.addCleanup(self.connection.close)
        self.connection.execute("CREATE TABLE existing AS SELECT 42 AS value")

    def csv(self, name, content):
        (self.root / name).write_text(content, encoding="utf-8", newline="")

    def excel(self, name, rows, epoch=None):
        workbook = Workbook()
        try:
            if epoch is not None:
                workbook.epoch = epoch
            for row in rows:
                workbook.active.append(row)
            workbook.save(self.root / name)
        finally:
            workbook.close()

    def sqlite(self, definitions):
        with closing(sqlite3.connect(self.root / "source.sqlite")) as connection:
            connection.executescript(definitions)

    def request(self, *ids, headers=None, first=2):
        return {"source_ids": list(ids), "header_rows": [1] if headers is None else headers, "first_data_row": first}

    def load(self, requests, connection=None, inventory=None):
        return data_tools.load_sources(self.connection if connection is None else connection, self.root,
            data_tools.build_inventory(self.root) if inventory is None else inventory, requests)

    def assert_original(self):
        self.assertEqual(self.connection.execute("SHOW TABLES").fetchall(), [("existing",)])
        self.assertEqual(self.connection.execute("SELECT * FROM existing").fetchall(), [(42,)])

    def test_csv_combines_in_explicit_order_and_preserves_values_and_references(self):
        self.csv("a.csv", "ID,,ID\n0007,, NA \n0007,, NA \n\n")
        self.csv("b.csv", 'ID,,ID\n"two\nlines",NULL,79k\n')
        result = self.load([self.request("S002", "S001")])
        self.assertEqual(result["tables"][0]["source_ids"], ["S002", "S001"])
        self.assertEqual(result["tables"][0]["row_count"], 4)
        self.assertEqual([r["source"]["source_id"] for r in result["source_catalog"]], ["S002", "S001"])
        self.assertEqual([r["row_count"] for r in result["source_catalog"]], [1, 3])
        self.assertEqual(self.connection.execute("SELECT * FROM raw_001 ORDER BY __source_id,__source_row").fetchall(),
            [("0007", "", " NA ", "S001", 2), ("0007", "", " NA ", "S001", 3),
             (None, None, None, "S001", 4), ("two\nlines", "NULL", "79k", "S002", 2)])
        self.assertEqual([r["table"] for r in result["source_catalog"]], ["raw_001", "raw_001"])
        self.assertEqual(result["tables"][0]["columns"][1]["header_labels"], [""])
        json.dumps(result, allow_nan=False)

    def test_multiple_requests_can_mix_formats_as_separate_tables(self):
        self.csv("a.csv", "Name\n0001\n")
        self.excel("b.xlsx", [["Title"], ["ID", "ID"], [None, "Code"], [True, "0002"]])
        self.sqlite('CREATE TABLE t(x); INSERT INTO t VALUES (123);')
        result = self.load([{"source_ids": ["S003"]}, self.request("S001"), self.request("S002", headers=[2, 3], first=4)])
        self.assertEqual([t["table"] for t in result["tables"]], ["raw_001", "raw_002", "raw_003"])
        self.assertEqual([t["source_ids"] for t in result["tables"]], [["S003"], ["S001"], ["S002"]])
        self.assertEqual(self.connection.execute("SELECT c001,c002,__source_row FROM raw_003").fetchone(), (True, "0002", 4))
        self.assertEqual(result["source_catalog"][2]["layout"], {"header_rows": [2, 3], "first_data_row": 4})
        self.assertEqual(self.connection.execute("SHOW TABLES").fetchall(), [("existing",), ("raw_001",), ("raw_002",), ("raw_003",)])

    def test_excel_combination_keeps_variant_types_errors_and_counts(self):
        self.excel("a.xlsx", [["ID", "Value"], ["0001", True], [2, "#N/A"]])
        self.excel("b.xlsx", [["ID", "Value"], [3.5, "=1+2"], [None, "0007"]])
        result = self.load([self.request("S001", "S002")])
        table = result["tables"][0]
        self.assertEqual((table["row_count"], table["formula_cells"], table["error_cells"]), (4, 1, 1))
        rows = self.connection.execute("SELECT c001,c002,__source_id,__source_row FROM raw_001 ORDER BY __source_id,__source_row").fetchall()
        self.assertEqual(rows, [("0001", True, "S001", 2), (2, {"type": "error", "value": "#N/A"}, "S001", 3),
            (3.5, {"type": "formula", "value": "=1+2"}, "S002", 2), (None, "0007", "S002", 3)])
        self.assertIs(type(rows[0][1]), bool)
        self.assertIs(type(rows[1][0]), int)
        self.assertEqual([m["error_cells"] for m in result["source_catalog"]], [1, 0])

    def test_sqlite_combination_preserves_actual_rowid_keys_and_blob_values(self):
        self.sqlite('CREATE TABLE a(x); CREATE TABLE b(x); INSERT INTO a(rowid,x) VALUES (7,\'0001\'),(11,X\'0102\'); '
                    'INSERT INTO b(rowid,x) VALUES (7,2.5),(99,NULL);')
        result = self.load([{"source_ids": ["S002", "S001"]}])
        self.assertEqual(result["tables"][0]["row_count"], 4)
        self.assertEqual(self.connection.execute("SELECT * FROM raw_001 ORDER BY __source_id,__source_row").fetchall(),
            [("0001", "S001", 1, {"_rowid_": 7}), ({"type": "bytes", "encoding": "base64", "value": "AQI="}, "S001", 2, {"_rowid_": 11}),
             (2.5, "S002", 1, {"_rowid_": 7}), (None, "S002", 2, {"_rowid_": 99})])

    def test_sqlite_composite_primary_keys_keep_source_identity(self):
        self.sqlite('CREATE TABLE a(x TEXT,y INTEGER,PRIMARY KEY(x,y)) WITHOUT ROWID; '
            'CREATE TABLE b(x TEXT,y INTEGER,PRIMARY KEY(x,y)) WITHOUT ROWID; '
            'INSERT INTO a VALUES (\'A\',2); INSERT INTO b VALUES (\'A\',2);')
        result = self.load([{"source_ids": ["S001", "S002"]}])
        self.assertEqual(result["tables"][0]["ordering"], {"kind": "primary_key", "columns": ["x", "y"]})
        self.assertEqual(self.connection.execute("SELECT __source_id,__source_row,__source_key FROM raw_001 ORDER BY __source_id").fetchall(),
            [("S001", 1, {"x": "A", "y": 2}), ("S002", 1, {"x": "A", "y": 2})])

    def test_invalid_requests_are_rejected_before_ingestion(self):
        self.csv("a.csv", "H\nv\n")
        self.csv("b.csv", "H\nv\n")
        bad = [None, {}, [], [None], [{}], [{"source_ids": []}], [{"source_ids": "S001"}],
            [self.request("S001", "S001")], [self.request("S001"), self.request("S001")], [self.request("unknown")],
            [self.request("S001"), {"source_ids": ["S002"]}], [dict(self.request("S001"), extra=True)],
            [self.request("S001", headers=[True])], [self.request("S001", first=1)], [{"source_ids": [1]}]]
        inventory = data_tools.build_inventory(self.root)
        with patch.object(data_tools, "_load_csv_into_table", side_effect=AssertionError("premature ingestion")):
            for requests in bad:
                with self.subTest(requests=requests), self.assertRaises(ValueError):
                    self.load(requests, inventory=inventory)
        self.assert_original()

    def test_formats_and_sqlite_layout_flags_are_rejected_before_ingestion(self):
        self.csv("a.csv", "H\nv\n")
        self.sqlite("CREATE TABLE t(x)")
        for requests in ([self.request("S001", "S002")], [self.request("S002")]):
            with self.subTest(requests=requests), self.assertRaises(ValueError):
                self.load(requests)
        self.assert_original()

    def test_all_paths_are_checked_before_loading_any_source(self):
        self.csv("a.csv", "H\nv\n")
        self.csv("b.csv", "H\nv\n")
        inventory = data_tools.build_inventory(self.root)
        (self.root / "b.csv").write_text("H\nchanged\n")
        with patch.object(data_tools, "_load_csv_into_table", side_effect=AssertionError("premature ingestion")), self.assertRaisesRegex(ValueError, "S002.*changed"):
            self.load([self.request("S001"), self.request("S002")], inventory=inventory)
        outside = self.root.parent / (self.root.name + "-outside.csv")
        outside.write_text("H\nv\n")
        self.addCleanup(outside.unlink, missing_ok=True)
        (self.root / "b.csv").unlink()
        (self.root / "b.csv").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "outside business directory"):
            self.load([self.request("S001", "S002")], inventory=inventory)
        self.assert_original()

    def test_csv_header_and_width_mismatches_rollback_complete_batch(self):
        self.csv("a.csv", "H,,H\n1,2,3\n")
        for content, reason in (("H,H,H\n4,5,6\n", "Incompatible column c002"), ("H\n4\n", "Incompatible column count")):
            self.csv("b.csv", content)
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, "S002.*" + reason):
                self.load([self.request("S001", "S002")])
            self.assert_original()

    def test_excel_header_types_and_epochs_must_match(self):
        self.excel("a.xlsx", [[True], [1]])
        self.excel("b.xlsx", [[1], [2]])
        with self.assertRaisesRegex(ValueError, "S002.*Incompatible column c001"):
            self.load([self.request("S001", "S002")])
        self.assert_original()
        self.excel("b.xlsx", [[True], [2]], CALENDAR_MAC_1904)
        with self.assertRaisesRegex(ValueError, "S002.*Incompatible epoch"):
            self.load([self.request("S001", "S002")])
        self.assert_original()

    def test_sqlite_schema_and_ordering_mismatches_fail(self):
        cases = [('CREATE TABLE a(x); CREATE TABLE b(y)', "column c001"),
            ('CREATE TABLE a(x TEXT); CREATE TABLE b(x INTEGER)', "column c001"),
            ('CREATE TABLE a(x TEXT PRIMARY KEY); CREATE TABLE b(x TEXT)', "column c001"),
            ('CREATE TABLE a(x TEXT PRIMARY KEY); CREATE TABLE b(x TEXT PRIMARY KEY) WITHOUT ROWID', "ordering")]
        for ddl, reason in cases:
            with self.subTest(reason=reason):
                (self.root / "source.sqlite").unlink(missing_ok=True)
                self.sqlite(ddl)
                with self.assertRaisesRegex(ValueError, "S002.*Incompatible " + reason):
                    self.load([{"source_ids": ["S001", "S002"]}])
                self.assert_original()

    def test_mismatch_in_later_request_removes_earlier_tables(self):
        self.csv("a.csv", "H\nv\n")
        self.csv("b.csv", "H\nv\n")
        self.csv("c.csv", "Different\nv\n")
        with self.assertRaisesRegex(ValueError, "S003.*Incompatible"):
            self.load([self.request("S001"), self.request("S002", "S003")])
        self.assert_original()

    def test_late_ragged_record_rolls_back_chunks_and_earlier_request(self):
        self.csv("a.csv", "H\nv\n")
        self.csv("b.csv", "H\n" + "v\n" * (data_tools.CSV_CHUNK_ROWS + 1) + "too,wide\n")
        with patch.object(data_tools, "_insert_csv_chunk", wraps=data_tools._insert_csv_chunk) as insert:
            with self.assertRaisesRegex(ValueError, "S002.*logical record 4099"):
                self.load([self.request("S001"), self.request("S002")])
        self.assertEqual(insert.call_count, 2)
        self.assert_original()

    def test_native_insert_failure_cleans_staging_after_rollback(self):
        self.csv("a.csv", "H\nv\n")
        self.csv("b.csv", "H\nv\n")
        cleanup = []

        def execute(query, *args):
            if query.startswith('INSERT INTO "raw_002"'):
                return self.connection.execute("SELECT CAST('bad' AS INTEGER)")
            return self.connection.execute(query, *args)

        def unregister(name):
            try:
                self.connection.unregister(name)
                cleanup.append("success")
            except duckdb.Error:
                cleanup.append("aborted")
                raise

        proxy = SimpleNamespace(execute=execute, register=self.connection.register, unregister=unregister)
        with self.assertRaisesRegex(ValueError, "S002"):
            self.load([self.request("S001"), self.request("S002")], connection=proxy)
        self.assertIn("aborted", cleanup)
        self.assertEqual(cleanup[-1], "success")
        self.assert_original()

    def test_empty_sources_and_repeated_calls_keep_catalog_counts_and_unused_names(self):
        self.csv("a.csv", "H\n")
        self.csv("b.csv", "H\nv\n")
        first = self.load([self.request("S001", "S002")])
        self.connection.execute('CREATE VIEW "_sqlite_chunk_raw_002" AS SELECT 7')
        second = self.load([self.request("S001", "S002")])
        self.assertEqual([r["row_count"] for r in first["source_catalog"]], [0, 1])
        self.assertEqual((first["tables"][0]["table"], second["tables"][0]["table"]), ("raw_001", "raw_003"))
        self.assertEqual(self.connection.execute("SELECT count(*) FROM raw_001").fetchone(), (1,))
        self.assertEqual(self.connection.execute('SELECT * FROM "_sqlite_chunk_raw_002"').fetchone(), (7,))

    def test_excel_and_sqlite_insert_failures_rollback_earlier_tables(self):
        self.csv("a.csv", "H\nv\n")
        for kind in ("xlsx", "sqlite"):
            with self.subTest(kind=kind):
                if kind == "xlsx":
                    self.excel("b.xlsx", [["H"], [1]])
                else:
                    (self.root / "b.xlsx").unlink()
                    self.sqlite("CREATE TABLE t(x); INSERT INTO t VALUES (1)")

                def execute(query, *args):
                    if query.startswith('INSERT INTO "raw_002"'):
                        return self.connection.execute("SELECT CAST('bad' AS INTEGER)")
                    return self.connection.execute(query, *args)

                proxy = SimpleNamespace(execute=execute, register=self.connection.register, unregister=self.connection.unregister)
                request = self.request("S002") if kind == "xlsx" else {"source_ids": ["S002"]}
                with self.assertRaisesRegex(ValueError, "S002"):
                    self.load([self.request("S001"), request], connection=proxy)
                self.assert_original()

    def test_append_chunks_remain_bounded_for_each_source(self):
        self.csv("a.csv", "H\na\nb\nc\n")
        self.csv("b.csv", "H\nd\ne\nf\n")
        native = data_tools._insert_csv_chunk
        chunks = []

        def insert(connection, table, columns, source_id, records):
            chunks.append((source_id, len(records)))
            native(connection, table, columns, source_id, records)

        with patch.object(data_tools, "CSV_CHUNK_ROWS", 2), patch.object(data_tools, "_insert_csv_chunk", side_effect=insert):
            result = self.load([self.request("S001", "S002")])
        self.assertEqual(chunks, [("S001", 2), ("S001", 1), ("S002", 2), ("S002", 1)])
        self.assertEqual(result["tables"][0]["row_count"], 6)

    def test_cli_model_free_success_and_duplicate_key_failure(self):
        self.csv("a.csv", "H\nv\n")
        request_path = self.root / "requests.json"
        request_path.write_text(json.dumps([self.request("S001")]))
        args = ["solver.py", "--config", str(self.root / "missing.json"), "--load-sources", str(self.root), "--requests-file", str(request_path)]
        with patch.object(sys, "argv", args), patch.object(solver, "load_config", side_effect=AssertionError("model configuration")), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(solver.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["tables"][0]["row_count"], 1)
        for text in ('[{"source_ids":[],"source_ids":["S001"]}]', '{', '[]', 'NaN'):
            request_path.write_text(text)
            with self.subTest(text=text), patch.object(sys, "argv", args), redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(solver.main(), 1)
            self.assertIn("Batch loading error", errors.getvalue())
        request_path.unlink()
        with patch.object(sys, "argv", args), redirect_stderr(io.StringIO()):
            self.assertEqual(solver.main(), 1)

    def test_cli_argument_combinations_exit_two(self):
        cases = [["--load-sources", str(self.root)], ["--inventory", str(self.root), "--requests-file", "x"],
            ["--load-sources", str(self.root), "--requests-file", "x", "--source-id", "S001"],
            ["--load-sources", str(self.root), "--requests-file", "x", "--header-rows", "1"],
            ["--load-sources", str(self.root), "--requests-file", "x", "--first-data-row", "2"],
            ["--load-sources", str(self.root), "--requests-file", "x", "--source-ids", "S001"]]
        for args in cases:
            with self.subTest(args=args), patch.object(sys, "argv", ["solver.py", *args]), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                solver.main()
            self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
