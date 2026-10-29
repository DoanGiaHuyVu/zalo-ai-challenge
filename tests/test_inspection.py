import base64
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, time, timedelta
import hashlib
import io
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from openpyxl import Workbook
from openpyxl.utils.datetime import CALENDAR_MAC_1904
from openpyxl.worksheet._read_only import ReadOnlyWorksheet

import data_tools
import solver


class InspectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.business = self.root / "shop"
        self.business.mkdir()

    def inspect(self, **kwargs):
        catalog = data_tools.build_inventory(self.business)
        return data_tools.inspect_sources(self.business, catalog, [catalog["sources"][0]["source_id"]], **kwargs)["previews"][0]

    def workbook(self, dimensions="missing"):
        path = self.business / "sổ.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Tháng 1"
        sheet["A1"] = "Sổ bán hàng"
        sheet.merge_cells("A1:C1")
        sheet["A3"] = "Hàng hoá"
        sheet.merge_cells("A3:B3")
        sheet["C3"] = "Tiền"
        sheet.append(["Mã", "Ngày", "Tổng"])
        sheet.append(["0007", "1/5", "79k"])
        sheet["A7"] = "Cuối"
        workbook.save(path)
        workbook.close()
        with zipfile.ZipFile(path) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        xml = entries["xl/worksheets/sheet1.xml"]
        replacement = b"" if dimensions == "missing" else b'<dimension ref="A1:A1"/>'
        entries["xl/worksheets/sheet1.xml"] = re.sub(rb"<dimension[^>]*/>", replacement, xml)
        with zipfile.ZipFile(path, "w") as archive:
            for name, content in entries.items():
                archive.writestr(name, content)
        return path

    def database(self, name="shop #1?.sqlite"):
        path = self.business / name
        with closing(sqlite3.connect(path)) as connection:
            connection.execute('CREATE TABLE "sổ""bán" (code TEXT, payload BLOB)')
            connection.executemany('INSERT INTO "sổ""bán" (rowid,code,payload) VALUES (?,?,?)',
                                   [(9, "0009", b"\x00\xff"), (2, "0002", None), (20, "0020", b"end")])
            connection.commit()
        return path

    def test_csv_preserves_bom_multiline_empty_records_and_raw_strings(self):
        (self.business / "raw.csv").write_bytes('\ufeffMã,Tiền,Ngày,Ghi chú\r\n0001,79k,1/5,"a\r\nb"\r\n\r\n0002," 1.000đ ",45689,\r\n'.encode("utf-8"))
        first = self.inspect(row_count=2)
        self.assertEqual(first["rows"], [{"source_row": 1, "values": ["Mã", "Tiền", "Ngày", "Ghi chú"]},
                                         {"source_row": 2, "values": ["0001", "79k", "1/5", "a\r\nb"]}])
        self.assertTrue(first["has_more"])
        self.assertEqual(first["next_start_row"], 3)
        second = self.inspect(start_row=3, row_count=2)
        self.assertEqual(second["rows"], [{"source_row": 3, "values": []},
                                          {"source_row": 4, "values": ["0002", " 1.000đ ", "45689", ""]}])
        self.assertFalse(second["has_more"])
        self.assertIsNone(second["next_start_row"])
        self.assertEqual(first["columns"][0], {"column_id": "c001", "position": 1})

    def test_empty_and_past_end_csv_do_not_invent_rows(self):
        path = self.business / "empty.csv"
        path.write_text("")
        self.assertEqual(self.inspect()["rows"], [])
        path.write_text("one\ntwo\n")
        preview = self.inspect(start_row=50)
        self.assertEqual(preview["rows"], [])
        self.assertFalse(preview["has_more"])

    def test_excel_titles_two_header_rows_and_gaps_ignore_dimensions(self):
        for dimensions in ("missing", "incorrect"):
            with self.subTest(dimensions=dimensions):
                self.workbook(dimensions)
                preview = self.inspect(row_count=5)
                self.assertEqual([r["source_row"] for r in preview["rows"]], [1, 2, 3, 4, 5])
                self.assertEqual(preview["rows"][0]["values"], ["Sổ bán hàng", None, None])
                self.assertEqual(preview["rows"][1]["values"], [None, None, None])
                self.assertEqual(preview["rows"][2]["values"], ["Hàng hoá", None, "Tiền"])
                self.assertEqual(preview["rows"][3]["values"], ["Mã", "Ngày", "Tổng"])
                self.assertEqual(preview["rows"][4]["values"], ["0007", "1/5", "79k"])
                self.assertTrue(preview["has_more"])
                self.assertEqual(preview["next_start_row"], 6)
                last = self.inspect(start_row=6)
                self.assertEqual(last["rows"], [{"source_row": 6, "values": [None]}, {"source_row": 7, "values": ["Cuối"]}])
                self.assertFalse(last["has_more"])
                self.assertEqual(self.inspect(start_row=20)["rows"], [])

    def test_excel_types_epoch_serial_dates_and_formulas(self):
        path = self.business / "dates.xlsx"
        workbook = Workbook()
        workbook.epoch = CALENDAR_MAC_1904
        sheet = workbook.active
        sheet.append([datetime(2025, 1, 2, 3, 4, 5), 45689, "=SUM(1,2)", True, time(4, 5, 6), timedelta(days=2, seconds=3), "#DIV/0!"])
        workbook.save(path)
        workbook.close()
        preview = self.inspect()
        self.assertEqual(preview["epoch"], "1904-01-01")
        self.assertEqual(preview["rows"][0]["values"], [
            {"type": "datetime", "value": "2025-01-02T03:04:05"}, 45689,
            {"type": "formula", "value": "=SUM(1,2)"}, True,
            {"type": "time", "value": "04:05:06"},
            {"type": "timedelta", "days": 2, "seconds": 3, "microseconds": 0},
            {"type": "error", "value": "#DIV/0!"}])
        json.dumps(preview, allow_nan=False)

    def test_empty_excel_sheet_has_no_fabricated_rows(self):
        path = self.business / "empty.xlsx"
        workbook = Workbook()
        workbook.save(path)
        workbook.close()
        self.assertEqual(self.inspect()["rows"], [])

    def test_sqlite_readonly_rows_order_keys_blob_and_quoted_table(self):
        path = self.database()
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        first = self.inspect(row_count=2)
        self.assertEqual(first["table"], 'sổ"bán')
        self.assertEqual([r["row_key"] for r in first["rows"]], [{"_rowid_": 2}, {"_rowid_": 9}])
        self.assertEqual([r["source_row"] for r in first["rows"]], [1, 2])
        self.assertEqual(first["rows"][0]["values"], ["0002", None])
        self.assertEqual(first["rows"][1]["values"][1], {"type": "bytes", "encoding": "base64", "value": base64.b64encode(b"\x00\xff").decode()})
        self.assertEqual(first["columns"][0]["name"], "code")
        self.assertEqual(first["columns"][1]["declared_type"], "BLOB")
        self.assertEqual(first["next_start_row"], 3)
        last = self.inspect(start_row=3)
        self.assertEqual(last["rows"][0]["row_key"], {"_rowid_": 20})
        self.assertFalse(last["has_more"])
        self.assertEqual(self.inspect(start_row=100)["rows"], [])
        with self.assertRaisesRegex(ValueError, "S001.*cannot inspect source"):
            self.inspect(start_row=10**100)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_sqlite_without_rowid_uses_composite_primary_key(self):
        with closing(sqlite3.connect(self.business / "keys.db")) as connection:
            connection.execute('CREATE TABLE sales ("b key" TEXT, "a key" INTEGER, value TEXT, PRIMARY KEY ("a key","b key")) WITHOUT ROWID')
            connection.executemany("INSERT INTO sales VALUES (?,?,?)", [("z", 2, "last"), ("b", 1, "middle"), ("a", 1, "first")])
            connection.commit()
        preview = self.inspect()
        self.assertEqual(preview["ordering"], {"kind": "primary_key", "columns": ["a key", "b key"]})
        self.assertEqual([r["values"][2] for r in preview["rows"]], ["first", "middle", "last"])
        self.assertEqual(preview["rows"][0]["row_key"], {"a key": 1, "b key": "a"})

    def test_sqlite_rowid_shadowing_and_generated_column_positions(self):
        with closing(sqlite3.connect(self.business / "shadow.db")) as connection:
            connection.execute('CREATE TABLE sales ("_rowid_" TEXT, value INTEGER, doubled INTEGER GENERATED ALWAYS AS (value*2))')
            connection.execute("INSERT INTO sales VALUES ('business value',3)")
            connection.commit()
        preview = self.inspect()
        self.assertEqual(preview["ordering"]["columns"], ["rowid"])
        self.assertEqual(preview["rows"][0]["values"], ["business value", 3, 6])
        self.assertEqual([c["name"] for c in preview["columns"]], ["_rowid_", "value", "doubled"])

    def test_batch_order_and_validation_precede_readers(self):
        for name in ("a.csv", "b.csv"):
            (self.business / name).write_text(name)
        catalog = data_tools.build_inventory(self.business)
        result = data_tools.inspect_sources(self.business, catalog, ["S002", "S001"])
        self.assertEqual([p["source_id"] for p in result["previews"]], ["S002", "S001"])
        cases = [((), {}), ([], {}), (["S001"] * 2, {}), (["S001"] * 9, {}),
                 (["S001", "unknown"], {}), (["S001"], {"start_row": 0}),
                 (["S001"], {"start_row": True}), (["S001"], {"row_count": 51}),
                 (["S001"], {"row_count": 0}), (["S001"], {"row_count": 1.0})]
        with patch("data_tools._inspect_csv", side_effect=AssertionError("Reader called before validation")):
            for ids, kwargs in cases:
                with self.subTest(ids=ids, kwargs=kwargs), self.assertRaises(ValueError):
                    data_tools.inspect_sources(self.business, catalog, ids, **kwargs)

    def test_missing_changed_and_escaping_files_are_source_specific_errors(self):
        path = self.business / "sales.csv"
        path.write_text("original")
        catalog = data_tools.build_inventory(self.business)
        # A different size is the detectable change in the metadata catalog.
        path.write_text("changed source has a different size")
        with self.assertRaisesRegex(ValueError, "S001.*sales.csv.*Source changed"):
            data_tools.inspect_sources(self.business, catalog, ["S001"])
        path.unlink()
        with self.assertRaisesRegex(ValueError, "S001.*sales.csv"):
            data_tools.inspect_sources(self.business, catalog, ["S001"])
        outside = self.root / "outside.csv"
        outside.write_text("original")
        path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "S001.*outside business directory"):
            data_tools.inspect_sources(self.business, catalog, ["S001"])

    def test_read_errors_and_missing_sheet_are_source_specific(self):
        self.workbook()
        catalog = data_tools.build_inventory(self.business)
        catalog["sources"][0]["sheet"] = "Missing"
        with self.assertRaisesRegex(ValueError, "S001.*sổ.xlsx.*Worksheet no longer exists"):
            data_tools.inspect_sources(self.business, catalog, ["S001"])
        (self.business / "sổ.xlsx").unlink()
        (self.business / "bad.csv").write_bytes(b"\xff\n")
        with self.assertRaisesRegex(ValueError, "S001.*bad.csv.*cannot inspect"):
            self.inspect()

    def test_response_limit_applies_to_complete_batch_and_utf8_bytes(self):
        for name in ("a.csv", "b.csv"):
            (self.business / name).write_text("x" * 10000)
        catalog = data_tools.build_inventory(self.business)
        single = data_tools.inspect_sources(self.business, catalog, ["S001"])
        self.assertLessEqual(len(json.dumps(single, ensure_ascii=False, allow_nan=False).encode()), data_tools.INSPECT_MAX_BYTES)
        with self.assertRaisesRegex(ValueError, "request fewer rows or sources"):
            data_tools.inspect_sources(self.business, catalog, ["S001", "S002"])
        (self.business / "b.csv").unlink()
        (self.business / "a.csv").write_text("đ" * 9000, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "UTF-8 JSON bytes"):
            self.inspect()

    def test_fewer_requested_rows_can_resolve_oversized_preview(self):
        (self.business / "large.csv").write_text(("x" * 1000 + "\n") * 50)
        with self.assertRaisesRegex(ValueError, "request fewer rows"):
            self.inspect(row_count=50)
        self.assertEqual(len(self.inspect(row_count=1)["rows"]), 1)

    def test_csv_handles_close_on_success_and_parse_failure(self):
        path = self.business / "raw.csv"
        native_open = Path.open
        for text in ("a\nb\nc\n", '"unterminated'):
            with self.subTest(text=text):
                path.write_text(text)
                catalog = data_tools.build_inventory(self.business)
                streams = []

                def tracked_open(path, *args, **kwargs):
                    stream = native_open(path, *args, **kwargs)
                    streams.append(stream)
                    return stream

                with patch.object(Path, "open", tracked_open):
                    if text.startswith('"'):
                        with self.assertRaises(ValueError):
                            data_tools.inspect_sources(self.business, catalog, ["S001"], row_count=1)
                    else:
                        data_tools.inspect_sources(self.business, catalog, ["S001"], row_count=1)
                self.assertTrue(all(stream.closed for stream in streams))

    def test_excel_workbook_iterator_and_file_close_on_success_and_failure(self):
        self.workbook()
        catalog = data_tools.build_inventory(self.business)
        native_load, native_iter = data_tools.load_workbook, ReadOnlyWorksheet.iter_rows
        workbooks, iterators, streams = [], [], []

        def tracked_load(stream, **kwargs):
            streams.append(stream)
            workbook = native_load(stream, **kwargs)
            workbooks.append(workbook)
            return workbook

        def tracked_iter(sheet, **kwargs):
            iterator = native_iter(sheet, **kwargs)
            iterators.append(iterator)
            return iterator

        for fail in (False, True):
            with self.subTest(fail=fail), patch("data_tools.load_workbook", side_effect=tracked_load), \
                    patch.object(ReadOnlyWorksheet, "iter_rows", tracked_iter):
                if fail:
                    with patch("data_tools._preview_value", side_effect=ValueError("encode failure")):
                        with self.assertRaisesRegex(ValueError, "encode failure"):
                            data_tools.inspect_sources(self.business, catalog, ["S001"], row_count=1)
                else:
                    data_tools.inspect_sources(self.business, catalog, ["S001"], row_count=1)
        def failed_load(stream, **kwargs):
            streams.append(stream)
            raise ValueError("workbook construction failed")

        with patch("data_tools.load_workbook", side_effect=failed_load):
            with self.assertRaisesRegex(ValueError, "workbook construction failed"):
                data_tools.inspect_sources(self.business, catalog, ["S001"])
        self.assertTrue(all(workbook._archive.fp is None for workbook in workbooks))
        self.assertTrue(all(iterator.gi_frame is None for iterator in iterators))
        self.assertTrue(all(stream.closed for stream in streams))

    def test_sqlite_connections_close_including_query_failure(self):
        self.database()
        catalog = data_tools.build_inventory(self.business)
        native_connect = sqlite3.connect
        connections = []
        for fail in (False, True):
            def tracked_connect(database_uri, **kwargs):
                self.assertTrue(database_uri.endswith("?mode=ro"))
                connection = native_connect(database_uri, **kwargs)
                if fail:
                    connection.set_authorizer(lambda action, table, *rest: sqlite3.SQLITE_DENY
                                              if action == sqlite3.SQLITE_READ and table == 'sổ"bán' else sqlite3.SQLITE_OK)
                connections.append(connection)
                return connection

            with self.subTest(fail=fail), patch("data_tools.sqlite3.connect", side_effect=tracked_connect):
                if fail:
                    with self.assertRaises(ValueError):
                        data_tools.inspect_sources(self.business, catalog, ["S001"])
                else:
                    data_tools.inspect_sources(self.business, catalog, ["S001"])
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def test_cli_skips_configuration_and_model(self):
        (self.business / "raw.csv").write_text("a\nb\n")
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ["solver.py", "--inspect", str(self.business), "--source-ids", "S001", "--row-count", "1"]
        with patch.object(sys, "argv", argv), redirect_stdout(stdout), redirect_stderr(stderr), \
                patch("solver.load_config", side_effect=AssertionError("Config loaded")), \
                patch("solver.load_model", side_effect=AssertionError("Model loaded")):
            self.assertEqual(solver.main(), 0)
        self.assertEqual(json.loads(stdout.getvalue())["previews"][0]["rows"], [{"source_row": 1, "values": ["a"]}])
        self.assertEqual(stderr.getvalue(), "")

    def test_actual_cli_errors_argument_exclusivity_and_missing_configuration(self):
        (self.business / "raw.csv").write_text("a\nb\n")
        command = [sys.executable, str(Path(solver.__file__).resolve())]
        args = ["--inspect", str(self.business), "--source-ids", "S001"]
        success = subprocess.run(command + args + ["--config", str(self.root / "absent.json")], capture_output=True, text=True, timeout=10)
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual(len(json.loads(success.stdout)["previews"]), 1)
        failure = subprocess.run(command + ["--inspect", str(self.business), "--source-ids", "unknown"], capture_output=True, text=True, timeout=10)
        self.assertEqual(failure.returncode, 1)
        self.assertEqual(failure.stdout, "")
        self.assertIn("unknown source ID", failure.stderr)
        for bad in (args + ["--row-count", "0"], args + ["--start-row", "-1"], args + ["--smoke"],
                    ["--inspect", str(self.business)], ["--inventory", str(self.business), "--row-count", "1"]):
            result = subprocess.run(command + bad, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2)
        self.assertFalse((self.root / "artifacts").exists())


if __name__ == "__main__":
    unittest.main()
