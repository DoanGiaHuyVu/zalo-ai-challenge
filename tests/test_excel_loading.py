from contextlib import closing, redirect_stdout, redirect_stderr
from datetime import datetime, time, timedelta
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

import duckdb
from openpyxl import Workbook
from openpyxl.utils.datetime import CALENDAR_MAC_1904, CALENDAR_WINDOWS_1900
from openpyxl.worksheet._read_only import ReadOnlyWorksheet

import data_tools
import solver


class ExcelLoadingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "sổ.xlsx"
        self.connection = duckdb.connect(":memory:", config={"enable_external_access": False, "autoload_known_extensions": False,
                                                             "autoinstall_known_extensions": False, "threads": 2, "memory_limit": "1GB"})
        self.addCleanup(self.connection.close)
        self.connection.execute("CREATE TABLE existing(value INTEGER)")
        self.connection.execute("INSERT INTO existing VALUES (1)")

    def workbook(self, rows, epoch=CALENDAR_WINDOWS_1900):
        workbook = Workbook()
        workbook.epoch = epoch
        workbook.active.title = "Tháng 1"
        for row in rows:
            workbook.active.append(row)
        workbook.save(self.path)
        workbook.close()

    def load(self, header_rows=None, first_data_row=2):
        return data_tools.load_excel(self.connection, self.root, data_tools.build_inventory(self.root), "S001",
                                     header_rows=[1] if header_rows is None else header_rows, first_data_row=first_data_row)

    def assert_original(self):
        self.assertEqual(self.connection.execute("SHOW TABLES").fetchall(), [("existing",)])
        self.assertEqual(self.connection.execute("SELECT * FROM existing").fetchall(), [(1,)])

    def test_mixed_native_types_leading_zeros_nulls_and_explicit_casts(self):
        self.workbook([["Value"], [45689], ["0007"], [1.0], [1.25], [True], [False], [], ["79k"], [9007199254740991]])
        result = self.load()
        values = [row[0] for row in self.connection.execute("SELECT c001 FROM raw_001 ORDER BY __source_row").fetchall()]
        # openpyxl reads an integral Excel number as int; preserve that source value.
        expected = [45689, "0007", 1, 1.25, True, False, None, "79k", 9007199254740991]
        self.assertEqual(values, expected)
        self.assertEqual([type(v) for v in values], [type(v) for v in expected])
        self.assertEqual(result["schema"][0], {"name": "c001", "type": "VARIANT"})
        self.assertEqual(self.connection.execute("SELECT count(*) FROM raw_001 WHERE c001 IS NULL").fetchone(), (1,))
        self.assertEqual(self.connection.execute("SELECT TRY_CAST(CAST(c001 AS VARCHAR) AS DOUBLE) FROM raw_001 WHERE __source_row=3").fetchone(), (7.0,))
        self.assertIn("TRY_CAST(CAST(c001 AS VARCHAR) AS DOUBLE)", result["sql_hint"])

    def test_dates_epochs_and_numeric_serials(self):
        for epoch, label in ((CALENDAR_WINDOWS_1900, "1899-12-30"), (CALENDAR_MAC_1904, "1904-01-01")):
            with self.subTest(epoch=epoch):
                self.workbook([["Date", "Serial", "Time"], [datetime(2025, 1, 2, 3, 4, 5), 45689, time(4, 5, 6)]], epoch)
                result = self.load()
                self.assertEqual(result["epoch"], label)
                self.assertEqual(self.connection.execute(f"SELECT c001,c002,c003 FROM {result['table']}").fetchone(),
                                 ("2025-01-02T03:04:05", 45689, "04:05:06"))

    def test_formulas_errors_and_durations_are_literals_not_cached_results(self):
        self.workbook([["Formula", "Error", "Elapsed"], ["=SUM(1,2)", "#DIV/0!", timedelta(days=2, seconds=3)]])
        result = self.load()
        row = self.connection.execute("SELECT c001,c002,c003 FROM raw_001").fetchone()
        self.assertEqual(row, ({"type": "formula", "value": "=SUM(1,2)"}, {"type": "error", "value": "#DIV/0!"},
                               {"type": "timedelta", "days": 2, "seconds": 3, "microseconds": 0}))
        self.assertEqual((result["formula_cells"], result["error_cells"]), (1, 1))

    def test_two_header_rows_merged_blanks_sparse_rows_and_duplicates(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Tháng 1"
        sheet.append(["Hàng hoá", None, "Tiền"])
        sheet.merge_cells("A1:B1")
        sheet.append(["Mã", "Mã"])
        sheet.append(["0001", "Giữ nguyên", 100])
        sheet.append([])
        sheet.append(["0001", "Giữ nguyên", 100])
        sheet.append([None, None, "TOTAL"])
        workbook.save(self.path)
        workbook.close()
        result = self.load(header_rows=[1, 2], first_data_row=3)
        self.assertEqual([c["header_labels"] for c in result["columns"]], [["Hàng hoá", "Mã"], [None, "Mã"], ["Tiền", None]])
        self.assertEqual(self.connection.execute("SELECT * FROM raw_001 ORDER BY __source_row").fetchall(),
                         [("0001", "Giữ nguyên", 100, "S001", 3), (None, None, None, "S001", 4),
                          ("0001", "Giữ nguyên", 100, "S001", 5), (None, None, "TOTAL", "S001", 6)])

    def test_row_four_layout_ignores_incorrect_or_missing_dimensions(self):
        for dimension in (b"", b'<dimension ref="A1:A1"/>'):
            with self.subTest(dimension=dimension):
                self.workbook([["Title"], ["Report notes"], [], ["ID", "Value"], ["0007", "79k"]])
                with zipfile.ZipFile(self.path) as archive:
                    entries = {name: archive.read(name) for name in archive.namelist()}
                entries["xl/worksheets/sheet1.xml"] = re.sub(rb"<dimension[^>]*/>", dimension, entries["xl/worksheets/sheet1.xml"])
                with zipfile.ZipFile(self.path, "w") as archive:
                    for name, content in entries.items():
                        archive.writestr(name, content)
                result = self.load(header_rows=[4], first_data_row=5)
                self.assertEqual(self.connection.execute(f"SELECT * FROM {result['table']}").fetchall(), [("0007", "79k", "S001", 5)])

    def test_empty_data_tail_and_header_only_sheet(self):
        self.workbook([["ID", "Value"]])
        result = self.load()
        self.assertEqual(result["row_count"], 0)
        self.assertEqual(len(result["schema"]), 4)
        self.assertEqual(self.connection.execute("SELECT * FROM raw_001").fetchall(), [])

    def test_invalid_layout_missing_header_and_sheet_do_not_publish_tables(self):
        self.workbook([["ID"], ["Value"]])
        for headers, first in (([], 2), ([2, 1], 3), ([1, 1], 2), ([True], 2), ([1], 1), ([100], 101)):
            with self.subTest(headers=headers), self.assertRaises(ValueError):
                self.load(header_rows=headers, first_data_row=first)
            self.assert_original()
        catalog = data_tools.build_inventory(self.root)
        catalog["sources"][0]["sheet"] = "Missing"
        with self.assertRaisesRegex(ValueError, "S001.*Missing.*Worksheet no longer exists"):
            data_tools.load_excel(self.connection, self.root, catalog, "S001", header_rows=[1], first_data_row=2)
        self.assert_original()

    def test_late_out_of_width_value_rolls_back_an_inserted_chunk(self):
        self.workbook([["ID"], *([["valid"]] * data_tools.EXCEL_CHUNK_ROWS), ["valid", "outside"]])
        with patch("data_tools._insert_excel_chunk", wraps=data_tools._insert_excel_chunk) as insert:
            with self.assertRaisesRegex(ValueError, "Tháng 1!B4098: nonempty cell beyond"):
                self.load()
        self.assertEqual(insert.call_count, 1)
        self.assert_original()

    def test_late_conversion_error_has_coordinates_and_rolls_back(self):
        self.workbook([["ID"], ["good"], ["bad"]])
        normal = data_tools._excel_value

        def convert(cell):
            if cell.value == "bad":
                raise ValueError("unsupported value")
            return normal(cell)

        with patch("data_tools._excel_value", side_effect=convert):
            with self.assertRaisesRegex(ValueError, "Tháng 1!A3: unsupported value"):
                self.load()
        self.assert_original()

    def test_aborted_insert_rolls_back_and_removes_staging(self):
        self.workbook([["ID"], *([["valid"]] * (data_tools.EXCEL_CHUNK_ROWS + 1))])
        inserts = 0

        def execute(query, *args):
            nonlocal inserts
            if query.startswith('INSERT INTO "raw_001"'):
                inserts += 1
                if inserts == 2:
                    return self.connection.execute("SELECT CAST('bad' AS INTEGER)")
            return self.connection.execute(query, *args)

        proxy = SimpleNamespace(execute=execute, register=self.connection.register, unregister=self.connection.unregister)
        with self.assertRaisesRegex(ValueError, "cannot load Excel"):
            data_tools.load_excel(proxy, self.root, data_tools.build_inventory(self.root), "S001", header_rows=[1], first_data_row=2)
        self.assertEqual(inserts, 2)
        self.assert_original()

    def test_repeated_loads_share_allocator_without_overwriting_tables_or_views(self):
        self.workbook([["ID"], [1]])
        one = self.load()
        self.connection.execute('CREATE VIEW "_excel_chunk_raw_002" AS SELECT 42 AS value')
        two = self.load()
        self.assertEqual((one["table"], two["table"]), ("raw_001", "raw_003"))
        self.assertEqual(self.connection.execute('SELECT * FROM "_excel_chunk_raw_002"').fetchone(), (42,))
        self.assertEqual(self.connection.execute("SELECT c001 FROM raw_001").fetchone(), (1,))

    def test_transaction_neutral_ingestion_supports_outer_batch_rollback(self):
        self.workbook([["ID"], [1]])
        source, path = data_tools._resolve_sources(self.root, data_tools.build_inventory(self.root), ["S001"])[0]
        self.connection.execute("BEGIN")
        data_tools._load_excel_into_table(self.connection, path, source, "raw_001", [1], 2)
        self.connection.execute("ROLLBACK")
        self.assert_original()

    def test_source_validation_rejects_non_excel_changes_and_escape(self):
        self.workbook([["ID"], [1]])
        catalog = data_tools.build_inventory(self.root)
        with self.assertRaisesRegex(ValueError, "unknown source ID"):
            data_tools.load_excel(self.connection, self.root, catalog, "unknown", header_rows=[1], first_data_row=2)
        original = self.path.read_bytes()
        self.path.write_bytes(original + b"changed")
        with self.assertRaisesRegex(ValueError, "Source changed"):
            data_tools.load_excel(self.connection, self.root, catalog, "S001", header_rows=[1], first_data_row=2)
        self.path.unlink()
        outside = self.root.parent / (self.root.name + "-outside.xlsx")
        outside.write_bytes(original)
        self.addCleanup(outside.unlink, missing_ok=True)
        self.path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "outside business directory"):
            data_tools.load_excel(self.connection, self.root, catalog, "S001", header_rows=[1], first_data_row=2)
        self.path.unlink()
        (self.root / "raw.csv").write_text("header\nvalue\n")
        with self.assertRaisesRegex(ValueError, "expected an Excel worksheet source"):
            self.load()
        self.assert_original()

    def test_workbook_iterator_and_file_cleanup_on_success_failure_and_bad_open(self):
        self.workbook([["ID"], [1]])
        catalog = data_tools.build_inventory(self.root)
        native_load, native_iter = data_tools.load_workbook, ReadOnlyWorksheet.iter_rows
        workbooks, iterators, streams = [], [], []

        def load(stream, **kwargs):
            streams.append(stream)
            workbook = native_load(stream, **kwargs)
            workbooks.append(workbook)
            return workbook

        def rows(sheet, **kwargs):
            iterator = native_iter(sheet, **kwargs)
            iterators.append(iterator)
            return iterator

        for fail in (False, True):
            with patch("data_tools.load_workbook", side_effect=load), patch.object(ReadOnlyWorksheet, "iter_rows", rows):
                if fail:
                    with patch("data_tools._excel_value", side_effect=ValueError("conversion failed")):
                        with self.assertRaises(ValueError):
                            data_tools.load_excel(self.connection, self.root, catalog, "S001", header_rows=[1], first_data_row=2)
                else:
                    data_tools.load_excel(self.connection, self.root, catalog, "S001", header_rows=[1], first_data_row=2)

        def bad_open(stream, **kwargs):
            streams.append(stream)
            raise ValueError("workbook open failed")

        with patch("data_tools.load_workbook", side_effect=bad_open), self.assertRaises(ValueError):
            data_tools.load_excel(self.connection, self.root, catalog, "S001", header_rows=[1], first_data_row=2)
        self.assertTrue(all(workbook._archive.fp is None for workbook in workbooks))
        self.assertTrue(all(iterator.gi_frame is None for iterator in iterators))
        self.assertTrue(all(stream.closed for stream in streams))

    def test_cli_requires_no_model_configuration_and_closes_connection(self):
        self.workbook([["ID"], [1]])
        native_connect = duckdb.connect
        connections = []

        def connect(*args, **kwargs):
            connection = native_connect(*args, **kwargs)
            connections.append(connection)
            return connection

        for source, expected in (("S001", 0), ("unknown", 1)):
            stdout, stderr = io.StringIO(), io.StringIO()
            argv = ["solver.py", "--load-excel", str(self.root), "--source-id", source, "--header-rows", "1", "--first-data-row", "2"]
            with patch.object(sys, "argv", argv), redirect_stdout(stdout), redirect_stderr(stderr), \
                    patch("solver.load_config", side_effect=AssertionError("Config loaded")), \
                    patch("solver.load_model", side_effect=AssertionError("Model loaded")), \
                    patch("solver.duckdb.connect", side_effect=connect):
                self.assertEqual(solver.main(), expected)
            if expected == 0:
                self.assertEqual(json.loads(stdout.getvalue())["row_count"], 1)
                self.assertEqual(stderr.getvalue(), "")
            else:
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("Excel loading error:", stderr.getvalue())
            with self.assertRaises(duckdb.Error):
                connections[-1].execute("SELECT 1")

    def test_actual_cli_missing_config_layout_flags_and_exclusive_modes(self):
        self.workbook([["ID"], [1]])
        command = [sys.executable, str(Path(solver.__file__).resolve())]
        args = ["--load-excel", str(self.root), "--source-id", "S001", "--header-rows", "1", "--first-data-row", "2"]
        success = subprocess.run(command + args + ["--config", str(self.root / "absent.json")], capture_output=True, text=True, timeout=15)
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual(json.loads(success.stdout)["schema"][0]["type"], "VARIANT")
        for bad in (["--load-excel", str(self.root)], args + ["--load-csv", str(self.root)], args[:-1] + ["1"],
                    ["--inventory", str(self.root), "--header-rows", "1"]):
            result = subprocess.run(command + bad, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
