from contextlib import closing, redirect_stdout, redirect_stderr
import csv
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import duckdb

import data_tools
import solver


class CSVLoadingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "raw.csv"
        self.connection = duckdb.connect(":memory:", config={"enable_external_access": False, "memory_limit": "1GB", "threads": 2})
        self.addCleanup(self.connection.close)
        self.connection.execute("CREATE TABLE existing(value INTEGER)")
        self.connection.execute("INSERT INTO existing VALUES (1)")

    def load(self, header_rows=None, first_data_row=2):
        return data_tools.load_csv(self.connection, self.root, data_tools.build_inventory(self.root), "S001",
                                   header_rows=[1] if header_rows is None else header_rows, first_data_row=first_data_row)

    def assert_original_tables(self):
        self.assertEqual(self.connection.execute("SHOW TABLES").fetchall(), [("existing",)])
        self.assertEqual(self.connection.execute("SELECT * FROM existing").fetchall(), [(1,)])

    def test_raw_values_empty_fields_blanks_duplicates_and_multiline_records(self):
        values = ["0007", " 79k ", "1/5", "NA", "NULL", "", "a\r\nb"]
        with self.path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["ID", "Money", "Date", "Text", "Text", "Empty", "Note"])
            writer.writerow(values)
            writer.writerow(values)
            writer.writerow([])
            writer.writerow(["TOTAL", "", "", "", "", "", "100"])
        result = self.load()
        rows = self.connection.execute(f"SELECT * FROM {result['table']} ORDER BY __source_row").fetchall()
        self.assertEqual(rows, [(*values, "S001", 2), (*values, "S001", 3),
                                (*([None] * 7), "S001", 4), ("TOTAL", "", "", "", "", "", "100", "S001", 5)])
        self.assertEqual(result["row_count"], 4)
        self.assertEqual(result["row_reference"], "csv_record")
        self.assertEqual(result["source"], {"source_id": "S001", "file": "raw.csv", "format": "csv"})
        actual = [(r[0], r[1]) for r in self.connection.execute(f"DESCRIBE {result['table']}").fetchall()]
        self.assertEqual(actual, [(f"c{i:03d}", "VARCHAR") for i in range(1, 8)] + [("__source_id", "VARCHAR"), ("__source_row", "BIGINT")])
        self.assertEqual(actual, [(column["name"], column["type"]) for column in result["schema"]])

    def test_explicit_multilevel_layout_preserves_duplicate_and_missing_labels(self):
        self.path.write_text("Title\n\nHàng hoá\nMã,,Mã\nNotes before data\n0001,79k,1/5\nTOTAL,,100\n", encoding="utf-8")
        result = self.load(header_rows=[3, 4], first_data_row=6)
        self.assertEqual([c["header_labels"] for c in result["columns"]], [["Hàng hoá", "Mã"], [None, ""], [None, "Mã"]])
        self.assertEqual(result["layout"], {"header_rows": [3, 4], "first_data_row": 6})
        self.assertEqual(self.connection.execute(f"SELECT * FROM {result['table']} ORDER BY __source_row").fetchall(),
                         [("0001", "79k", "1/5", "S001", 6), ("TOTAL", "", "100", "S001", 7)])

    def test_header_text_is_metadata_and_cannot_become_sql_identifiers(self):
        self.path.write_text('__source_row,__source_id,"x""; DROP TABLE existing;--"\n1,2,3\n')
        result = self.load()
        self.assertEqual(result["columns"][0]["header_labels"], ["__source_row"])
        self.assertEqual(result["columns"][2]["header_labels"], ['x"; DROP TABLE existing;--'])
        self.assertEqual(self.connection.execute("SELECT count(*) FROM existing").fetchone(), (1,))
        self.assertEqual(self.connection.execute(f"SELECT c001,c002,c003 FROM {result['table']}").fetchone(), ("1", "2", "3"))

    def test_header_only_csv_produces_a_typed_empty_table(self):
        self.path.write_text('"",Label\n')
        result = self.load()
        self.assertEqual(result["row_count"], 0)
        self.assertEqual(result["columns"][0]["header_labels"], [""])
        self.assertEqual(self.connection.execute(f"SELECT * FROM {result['table']}").fetchall(), [])

    def test_invalid_layouts_are_rejected_before_reading(self):
        self.path.write_text("header\ndata\n")
        catalog = data_tools.build_inventory(self.root)
        cases = [(headers, first) for headers, first in (([], 2), ([0], 2), ([True], 2), ([1.0], 2),
                  ([2, 1], 3), ([1, 1], 2), ([1], 1), ([1], True), ([1], 2.0), (None, 2))]
        with patch.object(Path, "open", side_effect=AssertionError("Read before validation")):
            for headers, first in cases:
                with self.subTest(headers=headers, first=first), self.assertRaises(ValueError):
                    data_tools.load_csv(self.connection, self.root, catalog, "S001", header_rows=headers, first_data_row=first)
        self.assert_original_tables()

    def test_missing_and_zero_width_headers_do_not_publish_tables(self):
        for text, headers, first, error in (("", [1], 2, "Missing header"), ("one\n", [2], 3, "Missing header"),
                                             ("\nvalue\n", [1], 2, "zero columns")):
            with self.subTest(text=text):
                self.path.write_text(text)
                with self.assertRaisesRegex(ValueError, error):
                    self.load(header_rows=headers, first_data_row=first)
                self.assert_original_tables()

    def test_ragged_data_reports_logical_record_and_rolls_back(self):
        self.path.write_text("a,b\n1,2\n3\n")
        with self.assertRaisesRegex(ValueError, "S001.*raw.csv.*logical record 3: expected 2 fields, found 1"):
            self.load()
        self.assert_original_tables()

    def test_late_width_and_parsing_failures_rollback_an_inserted_chunk(self):
        for ending, reason in (("wrong,extra\n", "logical record 4098"), ('"unterminated', "logical record 4098")):
            with self.subTest(reason=reason, ending=ending):
                self.path.write_text("header\n" + "valid\n" * data_tools.CSV_CHUNK_ROWS + ending)
                native_insert = data_tools._insert_csv_chunk
                with patch("data_tools._insert_csv_chunk", wraps=native_insert) as insert:
                    with self.assertRaisesRegex(ValueError, reason):
                        self.load()
                self.assertEqual(insert.call_count, 1)
                self.assert_original_tables()

    def test_aborted_insert_cleans_registration_after_rollback(self):
        self.path.write_text("header\n" + "valid\n" * (data_tools.CSV_CHUNK_ROWS + 1))
        inserted = 0
        cleanup = []

        def execute(query, *args):
            nonlocal inserted
            if query.startswith('INSERT INTO "raw_001"'):
                inserted += 1
                if inserted == 2:
                    return self.connection.execute("SELECT CAST('bad' AS INTEGER)")
            return self.connection.execute(query, *args)

        def unregister(name):
            try:
                result = self.connection.unregister(name)
                cleanup.append("success")
                return result
            except duckdb.Error:
                cleanup.append("aborted")
                raise

        proxy = SimpleNamespace(execute=execute, register=self.connection.register, unregister=unregister)
        with self.assertRaisesRegex(ValueError, "cannot load CSV"):
            data_tools.load_csv(proxy, self.root, data_tools.build_inventory(self.root), "S001", header_rows=[1], first_data_row=2)
        self.assertEqual(inserted, 2)
        self.assertIn("aborted", cleanup)
        self.assertEqual(cleanup[-1], "success")
        self.assert_original_tables()

    def test_chunk_sizes_remain_bounded_and_staging_views_are_removed(self):
        self.path.write_text("header\n" + "valid\n" * (data_tools.CSV_CHUNK_ROWS + 7))
        native_insert = data_tools._insert_csv_chunk
        sizes = []

        def insert(connection, table, columns, source, records):
            sizes.append(len(records))
            return native_insert(connection, table, columns, source, records)

        with patch("data_tools._insert_csv_chunk", side_effect=insert):
            result = self.load()
        self.assertEqual(sizes, [4096, 7])
        self.assertEqual(result["row_count"], 4103)
        self.assertEqual(self.connection.execute("SHOW TABLES").fetchall(), [("existing",), ("raw_001",)])

    def test_repeated_loads_use_distinct_names_and_preserve_existing_bindings(self):
        self.path.write_text("header\nvalue\n")
        first = self.load()
        second = self.load()
        self.assertEqual((first["table"], second["table"]), ("raw_001", "raw_002"))
        self.connection.execute('CREATE VIEW "_csv_chunk_raw_003" AS SELECT 42 AS value')
        third = self.load()
        self.assertEqual(third["table"], "raw_004")
        self.assertEqual(self.connection.execute('SELECT * FROM "_csv_chunk_raw_003"').fetchone(), (42,))
        self.assertEqual(self.connection.execute("SELECT c001 FROM raw_001").fetchall(), [("value",)])

    def test_shared_source_validation_rejects_wrong_format_change_and_escape(self):
        self.path.write_text("header\ndata\n")
        catalog = data_tools.build_inventory(self.root)
        with self.assertRaisesRegex(ValueError, "unknown source ID"):
            data_tools.load_csv(self.connection, self.root, catalog, "unknown", header_rows=[1], first_data_row=2)
        self.path.write_text("changed size\n")
        with self.assertRaisesRegex(ValueError, "Source changed"):
            data_tools.load_csv(self.connection, self.root, catalog, "S001", header_rows=[1], first_data_row=2)
        self.path.unlink()
        outside = self.root.parent / (self.root.name + "-outside.csv")
        self.addCleanup(outside.unlink, missing_ok=True)
        outside.write_text("header\ndata\n")
        self.path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "outside business directory"):
            data_tools.load_csv(self.connection, self.root, catalog, "S001", header_rows=[1], first_data_row=2)
        self.path.unlink()
        with closing(sqlite3.connect(self.root / "shop.sqlite")) as database:
            database.execute("CREATE TABLE sales(value TEXT)")
        with self.assertRaisesRegex(ValueError, "expected a CSV source"):
            self.load()
        self.assert_original_tables()

    def test_file_closes_on_success_and_utf8_failure(self):
        native_open = Path.open
        for content in (b"header\nvalue\n", b"header\n\xff\n"):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                catalog = data_tools.build_inventory(self.root)
                streams = []

                def open_path(path, *args, **kwargs):
                    stream = native_open(path, *args, **kwargs)
                    streams.append(stream)
                    return stream

                with patch.object(Path, "open", open_path):
                    if b"\xff" in content:
                        with self.assertRaisesRegex(ValueError, "logical record"):
                            data_tools.load_csv(self.connection, self.root, catalog, "S001", header_rows=[1], first_data_row=2)
                    else:
                        data_tools.load_csv(self.connection, self.root, catalog, "S001", header_rows=[1], first_data_row=2)
                self.assertTrue(streams)
                self.assertTrue(all(stream.closed for stream in streams))

    def test_private_ingestion_respects_outer_transaction_for_future_batches(self):
        self.path.write_text("header\nvalue\n")
        source, path = data_tools._resolve_sources(self.root, data_tools.build_inventory(self.root), ["S001"])[0]
        self.connection.execute("BEGIN")
        data_tools._load_csv_into_table(self.connection, path, source, "raw_001", [1], 2)
        self.connection.execute("ROLLBACK")
        self.assert_original_tables()

    def test_public_loader_rejects_nested_transactions_without_committing_them(self):
        self.path.write_text("header\nvalue\n")
        self.connection.execute("BEGIN")
        self.connection.execute("INSERT INTO existing VALUES (2)")
        with self.assertRaisesRegex(ValueError, "cannot load CSV"):
            self.load()
        self.connection.execute("ROLLBACK")
        self.assert_original_tables()

    def test_cli_requires_no_model_configuration_and_closes_connection(self):
        self.path.write_text("header\n0007\n")
        native_connect = duckdb.connect
        connections = []

        def connect(*args, **kwargs):
            connection = native_connect(*args, **kwargs)
            connections.append(connection)
            return connection

        for source_id, code in (("S001", 0), ("unknown", 1)):
            stdout, stderr = io.StringIO(), io.StringIO()
            argv = ["solver.py", "--load-csv", str(self.root), "--source-id", source_id, "--header-rows", "1", "--first-data-row", "2"]
            with patch.object(sys, "argv", argv), redirect_stdout(stdout), redirect_stderr(stderr), \
                    patch("solver.load_config", side_effect=AssertionError("Config loaded")), \
                    patch("solver.load_model", side_effect=AssertionError("Model loaded")), \
                    patch("solver.duckdb.connect", side_effect=connect):
                self.assertEqual(solver.main(), code)
            if code == 0:
                self.assertEqual(json.loads(stdout.getvalue())["row_count"], 1)
                self.assertEqual(stderr.getvalue(), "")
            else:
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("CSV loading error:", stderr.getvalue())
            with self.assertRaises(duckdb.Error):
                connections[-1].execute("SELECT 1")

    def test_actual_cli_argument_validation_and_missing_config(self):
        self.path.write_text("header\nvalue\n")
        command = [sys.executable, str(Path(solver.__file__).resolve())]
        args = ["--load-csv", str(self.root), "--source-id", "S001", "--header-rows", "1", "--first-data-row", "2"]
        success = subprocess.run(command + args + ["--config", str(self.root / "absent.json")], capture_output=True, text=True, timeout=15)
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual(json.loads(success.stdout)["row_count"], 1)
        for bad in (["--load-csv", str(self.root)], args + ["--smoke"], args[:-1] + ["1"],
                    ["--inspect", str(self.root), "--source-ids", "S001", "--header-rows", "1"],
                    ["--inventory", str(self.root), "--first-data-row", "2"]):
            result = subprocess.run(command + bad, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((self.root / "artifacts").exists())


if __name__ == "__main__":
    unittest.main()
