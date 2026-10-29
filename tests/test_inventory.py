import contextlib
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

import data_tools
import solver


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.business = self.root / "cửa_hàng"
        self.business.mkdir()

    def workbook(self, name, sheets):
        workbook = ET.Element("workbook", xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main")
        tabs = ET.SubElement(workbook, "sheets")
        relationships = ET.Element("Relationships", xmlns="http://schemas.openxmlformats.org/package/2006/relationships")
        for index, (label, state, kind) in enumerate(sheets, 1):
            ET.SubElement(tabs, "sheet", name=label, sheetId=str(index), state=state,
                          attrib={"{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id": f"rId{index}"})
            ET.SubElement(relationships, "Relationship", Id=f"rId{index}",
                          Type=f"http://schemas.openxmlformats.org/officeDocument/2006/relationships/{kind}", Target=f"worksheets/sheet{index}.xml")
        path = self.business / name
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("xl/workbook.xml", ET.tostring(workbook))
            archive.writestr("xl/_rels/workbook.xml.rels", ET.tostring(relationships))
            archive.writestr("xl/worksheets/sheet1.xml", "deliberately invalid cell data; must not be read")
        return path

    def database(self, name="shop.sqlite"):
        path = self.business / name
        with contextlib.closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE z_sales(id INTEGER PRIMARY KEY AUTOINCREMENT, secret TEXT)")
            connection.execute("INSERT INTO z_sales(secret) VALUES ('private business row')")
            connection.execute('CREATE TABLE "khách_hàng"(name TEXT)')
            connection.execute("CREATE TABLE sqliteSales(amount TEXT)")
            connection.execute("CREATE VIEW sales_view AS SELECT * FROM z_sales")
            connection.commit()
        return path

    def test_sources_have_deterministic_ids_original_names_and_workbook_order(self):
        self.workbook("a.xlsx", [("Tháng 2", "visible", "worksheet"), ("Tháng 1", "hidden", "worksheet"),
                                 ("Bí mật", "veryHidden", "worksheet"), ("Chart", "visible", "chartsheet")])
        self.workbook("b.xlsx", [("Tháng 2", "visible", "worksheet")])
        catalog = data_tools.build_inventory(self.business)
        self.assertEqual([s["sheet"] for s in catalog["sources"]], ["Tháng 2", "Tháng 1", "Bí mật", "Tháng 2"])
        self.assertEqual([s["source_id"] for s in catalog["sources"]], ["S001", "S002", "S003", "S004"])
        self.assertEqual([s["file"] for s in catalog["sources"]], ["a.xlsx"] * 3 + ["b.xlsx"])
        self.assertEqual(data_tools.build_inventory(self.business), catalog)
        self.assertIn("Tháng 2", json.dumps(catalog, ensure_ascii=False))

    def test_nested_files_extension_aliases_sizes_and_empty_databases(self):
        nested = self.business / "tháng_01"
        nested.mkdir()
        (nested / "sales.CSV").write_bytes(b"\xffraw csv is not parsed")
        self.workbook("book.XLSX", [("T01", "visible", "worksheet")])
        for name in ("a.db", "b.SQLITE", "c.sqlite3"):
            with contextlib.closing(sqlite3.connect(self.business / name)) as connection:
                connection.execute("VACUUM")
        (self.business / "README.md").write_text("Not a structured source")
        (self.business / "ignored.json").write_text("Not a structured source")
        catalog = data_tools.build_inventory(self.business)
        self.assertEqual([f["file"] for f in catalog["files"]], ["a.db", "b.SQLITE", "book.XLSX", "c.sqlite3", "tháng_01/sales.CSV"])
        self.assertEqual([s["file"] for s in catalog["sources"]], ["book.XLSX", "tháng_01/sales.CSV"])
        for item in catalog["files"]:
            self.assertEqual(item["size_bytes"], (self.business / item["file"]).stat().st_size)
            self.assertIn(item["format"], ("csv", "xlsx", "sqlite"))

    def test_sqlite_excludes_internal_tables_and_views(self):
        self.database()
        tables = [s["table"] for s in data_tools.build_inventory(self.business)["sources"]]
        self.assertEqual(tables, ["khách_hàng", "sqliteSales", "z_sales"])

    def test_file_symlinks_stay_inside_business_and_directory_links_are_rejected(self):
        original = self.business / "original.csv"
        original.write_text("raw")
        (self.business / "alias.csv").symlink_to(original)
        self.assertEqual([s["file"] for s in data_tools.build_inventory(self.business)["sources"]], ["alias.csv", "original.csv"])
        outside = self.root / "outside.csv"
        outside.write_text("outside")
        (self.business / "escaped.csv").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "escaped.csv:.*outside business directory"):
            data_tools.build_inventory(self.business)
        (self.business / "escaped.csv").unlink()
        (self.business / "loop").symlink_to(self.business, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "loop: directory symlinks"):
            data_tools.build_inventory(self.business)

    def test_corrupt_supported_files_raise_instead_of_returning_partial_catalog(self):
        (self.business / "a.csv").write_text("valid earlier source")
        for name, content in (("broken.xlsx", b"not zip"), ("broken.db", b"not sqlite")):
            with self.subTest(name=name):
                path = self.business / name
                path.write_bytes(content)
                with self.assertRaises(ValueError) as caught:
                    data_tools.build_inventory(self.business)
                self.assertIn(f"{name}: cannot inventory source:", str(caught.exception))
                path.unlink()
        with zipfile.ZipFile(self.business / "malformed.xlsx", "w") as archive:
            archive.writestr("xl/workbook.xml", "<broken")
        with self.assertRaisesRegex(ValueError, "malformed.xlsx: cannot inventory source"):
            data_tools.build_inventory(self.business)

    def test_missing_unreadable_and_empty_inputs(self):
        self.assertEqual(data_tools.build_inventory(self.business), {"files": [], "sources": []})
        with self.assertRaisesRegex(ValueError, "Cannot inventory"):
            data_tools.build_inventory(self.root / "absent")
        (self.business / "unreadable.csv").symlink_to(self.business / "missing.csv")
        with self.assertRaisesRegex(ValueError, "unreadable.csv: cannot inventory source"):
            data_tools.build_inventory(self.business)
        (self.business / "unreadable.csv").unlink()
        self.workbook("denied.xlsx", [("T01", "visible", "worksheet")])
        with patch("data_tools.zipfile.ZipFile", side_effect=PermissionError("Access denied")):
            with self.assertRaisesRegex(ValueError, "denied.xlsx:.*Access denied"):
                data_tools.build_inventory(self.business)

    def test_metadata_only_access_is_read_only_and_closes_resources(self):
        workbook = self.workbook("book.xlsx", [("T01", "visible", "worksheet")])
        database = self.database("shop #1?.sqlite")
        (self.business / "raw.csv").write_bytes(b"\xffnot UTF-8")
        before = hashlib.sha256(database.read_bytes()).hexdigest()
        connections, archives = [], []
        native_connect, native_zip, native_read, native_open = sqlite3.connect, zipfile.ZipFile, zipfile.ZipFile.read, Path.open

        def connect(database_uri, **kwargs):
            self.assertTrue(kwargs["uri"])
            self.assertTrue(database_uri.endswith("?mode=ro"))
            connection = native_connect(database_uri, **kwargs)
            connection.set_authorizer(lambda action, table, *rest: sqlite3.SQLITE_DENY
                                      if action == sqlite3.SQLITE_READ and table not in ("sqlite_master", "sqlite_schema")
                                      else sqlite3.SQLITE_OK)
            connections.append(connection)
            return connection

        def archive(*args, **kwargs):
            opened = native_zip(*args, **kwargs)
            archives.append(opened)
            return opened

        def read(opened, member, *args, **kwargs):
            self.assertIn(member, ("xl/workbook.xml", "xl/_rels/workbook.xml.rels"))
            return native_read(opened, member, *args, **kwargs)

        def open_path(path, *args, **kwargs):
            self.assertNotEqual(path.suffix, ".csv", "CSV rows were opened")
            return native_open(path, *args, **kwargs)

        with patch("data_tools.sqlite3.connect", side_effect=connect), patch("data_tools.zipfile.ZipFile", side_effect=archive), \
                patch.object(native_zip, "read", read), patch.object(Path, "open", open_path):
            catalog = data_tools.build_inventory(self.business)
        self.assertEqual(len(catalog["sources"]), 5)
        self.assertNotIn("private business row", json.dumps(catalog))
        self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before)
        self.assertTrue(archives)
        for opened in archives:
            self.assertIsNone(opened.fp)
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
        self.assertTrue(workbook.is_file())

    def test_resources_close_after_metadata_failure(self):
        path = self.business / "bad.xlsx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("xl/workbook.xml", "<invalid")
        native_zip, archives = zipfile.ZipFile, []

        def archive(*args, **kwargs):
            opened = native_zip(*args, **kwargs)
            archives.append(opened)
            return opened

        with patch("data_tools.zipfile.ZipFile", side_effect=archive):
            with self.assertRaises(ValueError):
                data_tools.build_inventory(self.business)
        self.assertIsNone(archives[0].fp)
        path.unlink()
        self.database()
        native_connect, connections = sqlite3.connect, []

        def denied_connect(*args, **kwargs):
            connection = native_connect(*args, **kwargs)
            connection.set_authorizer(lambda *args: sqlite3.SQLITE_DENY)
            connections.append(connection)
            return connection

        with patch("data_tools.sqlite3.connect", side_effect=denied_connect):
            with self.assertRaisesRegex(ValueError, "shop.sqlite: cannot inventory source"):
                data_tools.build_inventory(self.business)
        with self.assertRaises(sqlite3.ProgrammingError):
            connections[0].execute("SELECT 1")

    def test_feasibility_adapter_keeps_original_shape_and_uses_shared_catalog(self):
        catalog = {"files": [{"file": "book.xlsx", "format": "xlsx", "size_bytes": 50},
                             {"file": "empty.DB", "format": "sqlite", "size_bytes": 100}],
                   "sources": [{"source_id": "S001", "file": "book.xlsx", "format": "xlsx", "sheet": "T01"}]}
        with patch("solver.build_inventory", return_value=catalog) as build:
            inventory = solver._inventory_names(self.business)
        build.assert_called_once_with(self.business)
        self.assertEqual(inventory, [{"file": "book.xlsx", "kind": "xlsx", "size_bytes": 50, "sheets": ["T01"]},
                                     {"file": "empty.DB", "kind": "db", "size_bytes": 100, "tables": []}])

    def test_cli_success_and_failure_skip_model_configuration(self):
        (self.business / "sales.csv").write_text("raw")
        for path, expected in ((self.business, 0), (self.root / "absent", 1)):
            with self.subTest(path=path):
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(sys, "argv", ["solver.py", "--inventory", str(path)]), \
                        contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                        patch("solver.load_config", side_effect=AssertionError("Config loaded")), \
                        patch("solver.load_model", side_effect=AssertionError("Model loaded")):
                    self.assertEqual(solver.main(), expected)
                if expected == 0:
                    self.assertEqual(json.loads(stdout.getvalue())["sources"][0]["source_id"], "S001")
                    self.assertEqual(stderr.getvalue(), "")
                else:
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertIn("Inventory error:", stderr.getvalue())

    def test_actual_cli_runs_without_config_and_rejects_combined_modes(self):
        (self.business / "sales.csv").write_text("raw")
        command = [sys.executable, str(Path(solver.__file__).resolve())]
        success = subprocess.run(command + ["--config", str(self.root / "absent.json"), "--inventory", str(self.business)],
                                 capture_output=True, text=True, timeout=10, cwd=self.root)
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual(json.loads(success.stdout)["sources"][0]["file"], "sales.csv")
        failure = subprocess.run(command + ["--inventory", str(self.root / "absent")], capture_output=True, text=True, timeout=10)
        self.assertEqual(failure.returncode, 1)
        self.assertEqual(failure.stdout, "")
        for extra in (["--smoke"], ["--validate-questions", "questions.jsonl"], ["--data-root", str(self.root)]):
            result = subprocess.run(command + ["--inventory", str(self.business), *extra], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
        self.assertFalse((self.root / "artifacts").exists())


if __name__ == "__main__":
    unittest.main()
