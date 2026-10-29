import base64
from contextlib import closing, redirect_stdout, redirect_stderr
import hashlib
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


class SQLiteLoadingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "shop #1?.sqlite"
        self.connection = duckdb.connect(":memory:", config={"enable_external_access": False, "autoload_known_extensions": False,
                                                             "autoinstall_known_extensions": False, "threads": 2, "memory_limit": "1GB"})
        self.addCleanup(self.connection.close)
        self.connection.execute("CREATE TABLE existing(value INTEGER)")
        self.connection.execute("INSERT INTO existing VALUES (1)")

    def database(self, values=("0007",)):
        with closing(sqlite3.connect(self.path)) as database:
            database.execute('CREATE TABLE "sổ""bán" (value)')
            database.executemany('INSERT INTO "sổ""bán" VALUES (?)', [(value,) for value in values])
            database.commit()

    def load(self):
        return data_tools.load_sqlite(self.connection, self.root, data_tools.build_inventory(self.root), "S001")

    def assert_original(self):
        self.assertEqual(self.connection.execute("SHOW TABLES").fetchall(), [("existing",)])
        self.assertEqual(self.connection.execute("SELECT * FROM existing").fetchall(), [(1,)])

    def test_mixed_storage_values_nulls_duplicates_blob_and_numeric_casts(self):
        raw = ["0007", -(2**63), 2**63 - 1, 1.25, None, b"\x00\xff", "NA", "NULL", "", "0007", float("inf")]
        self.database(raw)
        result = self.load()
        values = [row[0] for row in self.connection.execute("SELECT c001 FROM raw_001 ORDER BY __source_row").fetchall()]
        expected = raw[:5] + [{"type": "bytes", "encoding": "base64", "value": "AP8="}] + raw[6:10] + [{"type": "float", "value": "inf"}]
        self.assertEqual(values, expected)
        self.assertEqual([type(v) for v in values], [type(v) for v in expected])
        self.assertEqual(base64.b64decode(values[5]["value"]), raw[5])
        self.assertEqual(result["row_count"], len(raw))
        self.assertEqual(result["columns"][0]["declared_type"], "")
        self.assertEqual(self.connection.execute("SELECT TRY_CAST(CAST(c001 AS VARCHAR) AS DOUBLE) FROM raw_001 WHERE __source_row=1").fetchone(), (7.0,))
        self.assertEqual(self.connection.execute("SELECT count(*) FROM raw_001 WHERE c001 IS NULL").fetchone(), (1,))

    def test_rowid_gaps_keys_and_order_agree_with_inspection(self):
        self.database([])
        with closing(sqlite3.connect(self.path)) as database:
            database.executemany('INSERT INTO "sổ""bán" (rowid,value) VALUES (?,?)', [(20, "last"), (-4, "first"), (9, "middle")])
            database.commit()
        catalog = data_tools.build_inventory(self.root)
        preview = data_tools.inspect_sources(self.root, catalog, ["S001"])["previews"][0]
        result = data_tools.load_sqlite(self.connection, self.root, catalog, "S001")
        rows = self.connection.execute("SELECT c001,__source_id,__source_row,__source_key FROM raw_001 ORDER BY __source_row").fetchall()
        self.assertEqual(rows, [("first", "S001", 1, {"_rowid_": -4}), ("middle", "S001", 2, {"_rowid_": 9}), ("last", "S001", 3, {"_rowid_": 20})])
        self.assertEqual([row[3] for row in rows], [row["row_key"] for row in preview["rows"]])
        self.assertEqual(result["ordering"], preview["ordering"])
        self.assertEqual(result["columns"], preview["columns"])
        self.assertEqual(result["row_reference"], "ordered_sqlite_record")

    def test_without_rowid_composite_blob_primary_keys(self):
        with closing(sqlite3.connect(self.path)) as database:
            database.execute('CREATE TABLE sales ("b key" BLOB, "a key" INTEGER, note TEXT, PRIMARY KEY ("a key","b key")) WITHOUT ROWID')
            database.executemany("INSERT INTO sales VALUES (?,?,?)", [(b"z", 2, "last"), (b"b", 1, "middle"), (b"a", 1, "first")])
            database.commit()
        result = self.load()
        rows = self.connection.execute("SELECT c003,__source_key FROM raw_001 ORDER BY __source_row").fetchall()
        self.assertEqual([row[0] for row in rows], ["first", "middle", "last"])
        self.assertEqual(rows[0][1], {"a key": 1, "b key": {"type": "bytes", "encoding": "base64", "value": "YQ=="}})
        self.assertEqual(result["ordering"], {"kind": "primary_key", "columns": ["a key", "b key"]})
        self.assertEqual([c["primary_key_position"] for c in result["columns"]], [2, 1, 0])

    def test_rowid_alias_shadowing_generated_columns_and_declared_types(self):
        with closing(sqlite3.connect(self.path)) as database:
            database.execute('CREATE TABLE sales ("_rowid_" TEXT, value INTEGER, doubled INTEGER GENERATED ALWAYS AS (value*2))')
            database.execute("INSERT INTO sales VALUES ('business value',3)")
            database.commit()
        result = self.load()
        self.assertEqual(result["ordering"]["columns"], ["rowid"])
        self.assertEqual(self.connection.execute("SELECT c001,c002,c003,__source_key FROM raw_001").fetchone(),
                         ("business value", 3, 6, {"rowid": 1}))
        self.assertEqual([c["name"] for c in result["columns"]], ["_rowid_", "value", "doubled"])

    def test_missing_table_or_unavailable_ordering_does_not_publish(self):
        self.database()
        catalog = data_tools.build_inventory(self.root)
        catalog["sources"][0]["table"] = "Missing"
        with self.assertRaisesRegex(ValueError, "S001.*Missing.*User table no longer exists"):
            data_tools.load_sqlite(self.connection, self.root, catalog, "S001")
        self.path.unlink()
        with closing(sqlite3.connect(self.path)) as database:
            database.execute("CREATE TABLE sales(rowid TEXT,_rowid_ TEXT,oid TEXT)")
        with self.assertRaisesRegex(ValueError, "stable row identity is unavailable"):
            self.load()
        self.assert_original()

    def test_source_connection_is_readonly_and_original_hash_is_unchanged(self):
        self.database(["0007", "unchanged"])
        catalog = data_tools.build_inventory(self.root)
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        native_connect = sqlite3.connect
        connections = []

        def connect(database_uri, **kwargs):
            self.assertTrue(database_uri.endswith("?mode=ro"))
            self.assertTrue(kwargs["uri"])
            database = native_connect(database_uri, **kwargs)
            connections.append(database)
            with self.assertRaises(sqlite3.OperationalError):
                database.execute("CREATE TABLE forbidden(value)")
            return database

        with patch("data_tools.sqlite3.connect", side_effect=connect):
            data_tools.load_sqlite(self.connection, self.root, catalog, "S001")
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        for database in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                database.execute("SELECT 1")

    def test_wal_writer_cannot_change_the_active_read_snapshot(self):
        writer = sqlite3.connect(self.path)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE sales(value INTEGER)")
        writer.executemany("INSERT INTO sales VALUES (?)", [(i,) for i in range(data_tools.SQLITE_CHUNK_ROWS + 1)])
        writer.commit()
        catalog = data_tools.build_inventory(self.root)
        native_insert = data_tools._insert_sqlite_chunk
        inserted = False

        def insert(*args):
            nonlocal inserted
            native_insert(*args)
            if not inserted:
                writer.execute("INSERT INTO sales VALUES (99999)")
                writer.commit()
                inserted = True

        with patch("data_tools._insert_sqlite_chunk", side_effect=insert):
            result = data_tools.load_sqlite(self.connection, self.root, catalog, "S001")
        self.assertEqual(result["row_count"], data_tools.SQLITE_CHUNK_ROWS + 1)
        self.assertEqual(writer.execute("SELECT count(*) FROM sales").fetchone(), (data_tools.SQLITE_CHUNK_ROWS + 2,))
        self.assertEqual(self.connection.execute("SELECT max(TRY_CAST(c001 AS BIGINT)) FROM raw_001").fetchone(), (4096,))

    def test_late_conversion_failure_rolls_back_an_inserted_chunk(self):
        self.database(["ok"] * data_tools.SQLITE_CHUNK_ROWS + ["bad"])
        normal = data_tools._preview_value

        def convert(value, *args):
            if value == "bad":
                raise ValueError("unsupported value")
            return normal(value, *args)

        with patch("data_tools._preview_value", side_effect=convert), \
                patch("data_tools._insert_sqlite_chunk", wraps=data_tools._insert_sqlite_chunk) as insert:
            with self.assertRaisesRegex(ValueError, "ordered record 4097, column 'value': unsupported value"):
                self.load()
        self.assertEqual(insert.call_count, 1)
        self.assert_original()

    def test_aborted_insertion_rolls_back_and_removes_staging(self):
        self.database(["ok"] * (data_tools.SQLITE_CHUNK_ROWS + 1))
        inserts = 0

        def execute(query, *args):
            nonlocal inserts
            if query.startswith('INSERT INTO "raw_001"'):
                inserts += 1
                if inserts == 2:
                    return self.connection.execute("SELECT CAST('bad' AS INTEGER)")
            return self.connection.execute(query, *args)

        proxy = SimpleNamespace(execute=execute, register=self.connection.register, unregister=self.connection.unregister)
        with self.assertRaisesRegex(ValueError, "cannot load SQLite"):
            data_tools.load_sqlite(proxy, self.root, data_tools.build_inventory(self.root), "S001")
        self.assertEqual(inserts, 2)
        self.assert_original()

    def test_late_source_read_failure_closes_cursor_and_connection(self):
        self.database(["ok"] * (data_tools.SQLITE_CHUNK_ROWS + 1))
        catalog = data_tools.build_inventory(self.root)
        native_connect = sqlite3.connect
        connections, cursors = [], []
        sizes = []

        class FailingCursor:
            def __init__(self, cursor):
                self.cursor = cursor
                self.calls = 0
                self.closed = False

            def fetchmany(self, size):
                sizes.append(size)
                self.calls += 1
                if self.calls == 2:
                    raise sqlite3.OperationalError("synthetic source read failure")
                return self.cursor.fetchmany(size)

            def close(self):
                self.cursor.close()
                self.closed = True

        def connect(*args, **kwargs):
            database = native_connect(*args, **kwargs)
            connections.append(database)

            def execute(query, *parameters):
                cursor = database.execute(query, *parameters)
                if query.startswith("SELECT src."):
                    cursor = FailingCursor(cursor)
                    cursors.append(cursor)
                return cursor

            return SimpleNamespace(execute=execute, close=database.close)

        with patch("data_tools.sqlite3.connect", side_effect=connect):
            with self.assertRaisesRegex(ValueError, "synthetic source read failure"):
                data_tools.load_sqlite(self.connection, self.root, catalog, "S001")
        self.assertEqual(sizes, [4096, 4096])
        self.assertTrue(all(cursor.closed for cursor in cursors))
        for database in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                database.execute("SELECT 1")
        self.assert_original()

    def test_empty_table_retains_schema_and_adds_no_rows(self):
        self.database([])
        result = self.load()
        self.assertEqual(result["row_count"], 0)
        self.assertEqual(len(result["schema"]), 4)
        self.assertEqual(self.connection.execute("SELECT * FROM raw_001").fetchall(), [])

    def test_repeated_loads_preserve_tables_and_avoid_staging_name_collisions(self):
        self.database()
        one = self.load()
        self.connection.execute('CREATE VIEW "_sqlite_chunk_raw_002" AS SELECT 42 AS value')
        two = self.load()
        self.assertEqual((one["table"], two["table"]), ("raw_001", "raw_003"))
        self.assertEqual(self.connection.execute('SELECT * FROM "_sqlite_chunk_raw_002"').fetchone(), (42,))
        self.assertEqual(self.connection.execute("SELECT c001 FROM raw_001").fetchone(), ("0007",))

    def test_private_ingestion_respects_outer_transaction_ownership(self):
        self.database()
        source, path = data_tools._resolve_sources(self.root, data_tools.build_inventory(self.root), ["S001"])[0]
        self.connection.execute("BEGIN")
        data_tools._load_sqlite_into_table(self.connection, path, source, "raw_001")
        self.connection.execute("ROLLBACK")
        self.assert_original()

    def test_shared_source_validation_rejects_change_escape_and_wrong_format(self):
        self.database()
        catalog = data_tools.build_inventory(self.root)
        with self.assertRaisesRegex(ValueError, "unknown source ID"):
            data_tools.load_sqlite(self.connection, self.root, catalog, "unknown")
        original = self.path.read_bytes()
        self.path.write_bytes(original + b"changed")
        with self.assertRaisesRegex(ValueError, "Source changed"):
            data_tools.load_sqlite(self.connection, self.root, catalog, "S001")
        self.path.unlink()
        outside = self.root.parent / (self.root.name + "-outside.sqlite")
        outside.write_bytes(original)
        self.addCleanup(outside.unlink, missing_ok=True)
        self.path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "outside business directory"):
            data_tools.load_sqlite(self.connection, self.root, catalog, "S001")
        self.path.unlink()
        (self.root / "raw.csv").write_text("header\nvalue\n")
        with self.assertRaisesRegex(ValueError, "expected a SQLite table source"):
            self.load()
        self.assert_original()

    def test_cli_skips_model_configuration_and_closes_connection(self):
        self.database()
        native_connect = duckdb.connect
        connections = []

        def connect(*args, **kwargs):
            connection = native_connect(*args, **kwargs)
            connections.append(connection)
            return connection

        for source, expected in (("S001", 0), ("unknown", 1)):
            stdout, stderr = io.StringIO(), io.StringIO()
            argv = ["solver.py", "--load-sqlite", str(self.root), "--source-id", source]
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
                self.assertIn("SQLite loading error:", stderr.getvalue())
            with self.assertRaises(duckdb.Error):
                connections[-1].execute("SELECT 1")

    def test_actual_cli_missing_config_rejects_layout_flags_and_other_modes(self):
        self.database()
        command = [sys.executable, str(Path(solver.__file__).resolve())]
        args = ["--load-sqlite", str(self.root), "--source-id", "S001"]
        success = subprocess.run(command + args + ["--config", str(self.root / "absent.json")], capture_output=True, text=True, timeout=15)
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual(json.loads(success.stdout)["row_count"], 1)
        for bad in (["--load-sqlite", str(self.root)], args + ["--header-rows", "1"], args + ["--first-data-row", "2"],
                    args + ["--load-csv", str(self.root)], ["--inventory", str(self.root), "--source-id", "S001"]):
            result = subprocess.run(command + bad, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
