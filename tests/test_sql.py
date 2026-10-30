from contextlib import closing
from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import duckdb
from openpyxl import Workbook
import pandas as pd

import data_tools


class RestrictedSQLTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.connection = data_tools.create_connection()
        self.addCleanup(self.connection.close)
        self.connection.execute("CREATE TABLE raw_001(c001 VARCHAR,c002 VARIANT)")
        self.connection.execute("INSERT INTO raw_001 VALUES ('001',CAST('12' AS JSON)::VARIANT),('002',CAST('\"8\"' AS JSON)::VARIANT),('002',NULL)")
        self.connection.execute("CREATE TABLE forbidden(secret INTEGER)")
        self.connection.execute("INSERT INTO forbidden VALUES(999)")

    def query(self, sql, allowed=None, **kwargs):
        with data_tools.restricted_query(self.connection, sql, allowed_tables={"raw_001"} if allowed is None else allowed, **kwargs) as cursor:
            return cursor.fetchall()

    def test_factory_sets_and_locks_security_and_resource_defaults(self):
        settings = dict(self.connection.execute("SELECT name,value FROM duckdb_settings()").fetchall())
        for name, value in data_tools._SQL_SECURITY.items():
            self.assertEqual(settings[name], str(value).lower() if isinstance(value, bool) else value)
        self.assertEqual(settings["threads"], "2")
        self.assertEqual(settings["memory_limit"], "953.6 MiB")
        self.assertEqual(settings["lock_configuration"], "true")
        for sql in ("SET threads=4", "SET memory_limit='2GB'", "SET enable_external_access=true", "SET lock_configuration=false"):
            with self.subTest(sql=sql), self.assertRaises(duckdb.Error):
                self.connection.execute(sql)

    def test_factory_validates_arguments_and_parser_version(self):
        for kwargs in ({"threads": True}, {"threads": 0}, {"threads": 1.5}, {"memory_limit": ""}, {"memory_limit": None}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                data_tools.create_connection(**kwargs)
        with patch.object(duckdb, "__version__", "different"), self.assertRaisesRegex(ValueError, "pinned"):
            data_tools.create_connection()
        with closing(data_tools.create_connection(memory_limit="128MB", threads=1)) as connection:
            self.assertEqual(connection.execute("SELECT current_setting('threads')").fetchone(), (1,))

    def test_aggregates_variant_casts_text_parsing_and_duplicate_grain(self):
        self.assertEqual(self.query("SELECT sum(TRY_CAST(CAST(c002 AS VARCHAR) AS DOUBLE)),count(*),count(c002),count(DISTINCT c001) FROM raw_001"),
                         [(20.0, 3, 2, 2)])
        self.assertEqual(self.query("SELECT regexp_replace(trim(' 79k '),'k','000'),lower('ABC'),CAST('0007' AS BIGINT)"), [("79000", "abc", 7)])

    def test_joins_ctes_unions_and_window_functions(self):
        sql = "WITH x AS (SELECT c001,count(*) n FROM raw_001 GROUP BY c001) SELECT x.c001,x.n,row_number() OVER(ORDER BY x.c001) FROM x JOIN (VALUES('001'),('002')) y(id) ON x.c001=y.id ORDER BY x.c001"
        self.assertEqual(self.query(sql), [("001", 1, 1), ("002", 2, 2)])
        self.assertEqual(self.query("WITH x AS MATERIALIZED (SELECT 2 n) SELECT n FROM x UNION ALL SELECT n FROM x"), [(2,), (2,)])

    def test_recursive_cte_and_scope_are_supported(self):
        sql = "WITH RECURSIVE x(n) AS(SELECT 1 UNION ALL SELECT n+1 FROM x WHERE n<4) SELECT sum(n) FROM x"
        self.assertEqual(self.query(sql), [(10,)])
        self.assertEqual(self.query("WITH forbidden AS(SELECT 7 n) SELECT * FROM forbidden"), [(7,)])
        self.assertEqual(self.query("WITH x AS(SELECT 1 n),y AS(SELECT n+1 n FROM x) SELECT * FROM y"), [(2,)])
        self.assertEqual(self.query("WITH x AS(SELECT 7 n) SELECT (WITH x AS(SELECT n FROM x) SELECT * FROM x) FROM x"), [(7,)])

    def test_cte_name_cannot_launder_disallowed_physical_table(self):
        cases = ["WITH forbidden AS(SELECT * FROM forbidden) SELECT * FROM forbidden",
            "WITH RECURSIVE forbidden(secret) AS(SELECT * FROM forbidden UNION ALL SELECT secret+1 FROM forbidden WHERE secret<1) SELECT * FROM forbidden",
            "SELECT (WITH forbidden AS(SELECT 1 n) SELECT n FROM forbidden),secret FROM forbidden",
            "WITH x AS(SELECT * FROM forbidden),forbidden AS(SELECT 1 n) SELECT * FROM x",
            "WITH x AS(SELECT 1 n) SELECT * FROM main.x"]
        for sql in cases:
            with self.subTest(sql=sql), self.assertRaisesRegex(ValueError, "table is not permitted"):
                self.query(sql)

    def test_comments_quoted_identifiers_and_semicolons_in_literals(self):
        self.assertEqual(self.query('/* DROP TABLE x; */ WITH "select" AS(SELECT c001 FROM "main"."raw_001") SELECT \'DELETE; --\',count(*) FROM "select"; -- end'), [("DELETE; --", 3)])
        self.assertEqual(self.query('SELECT "c001" FROM "RAW_001" ORDER BY "c001" LIMIT 1'), [("001",)])

    def test_calendar_dates_and_reviewed_macros(self):
        self.connection.execute("CREATE TABLE calendar(date DATE,holiday VARCHAR)")
        self.connection.execute("INSERT INTO calendar VALUES (DATE '2024-02-10','Tết')")
        self.assertEqual(self.query("SELECT date,holiday,date_add(date,INTERVAL 1 DAY),date_part('year',date) FROM calendar", allowed={"calendar"}),
                         [(date(2024, 2, 10), "Tết", __import__('datetime').datetime(2024, 2, 11), 2024)])
        self.assertEqual(self.query("SELECT if(true,1,2),nullif(2,2),coalesce(NULL,3),CASE WHEN true THEN 4 END,try_strptime('31/01/2024','%d/%m/%Y')::DATE"),
                         [(1, None, 3, 4, date(2024, 1, 31))])

    def test_pure_table_functions_and_nested_arguments(self):
        self.assertEqual(self.query("SELECT sum(i) FROM range(4) t(i)"), [(6,)])
        self.assertEqual(self.query("SELECT count(*) FROM generate_series(DATE '2024-01-01',DATE '2024-01-03',INTERVAL 1 DAY)"), [(3,)])
        self.assertEqual(self.query("SELECT * FROM unnest([1,2])"), [(1,), (2,)])
        self.assertEqual(self.query("SELECT key FROM json_each('{\"a\":1}')"), [("a",)])
        self.assertEqual(self.query("SELECT count(*) FROM json_tree('[1,2]')"), [(3,)])
        with self.assertRaisesRegex(ValueError, "function is not permitted"):
            self.query("SELECT * FROM range((SELECT nextval('x')))")

    def test_only_one_select_and_no_parameters(self):
        cases = ["", "  ", None, "-- comment", "SELECT 1;SELECT 2", "PRAGMA version", "EXPLAIN SELECT 1", "SHOW TABLES",
                 "DESCRIBE raw_001", "CALL checkpoint()", "SELECT ?", "SELECT $x", "SELECT FROM"]
        for sql in cases:
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                self.query(sql)
        self.assertEqual(self.query("SELECT 42", allowed=set()), [(42,)])

    def test_write_config_and_extension_statements_are_rejected(self):
        cases = ["INSERT INTO raw_001 VALUES('bad',NULL)", "DELETE FROM raw_001", "UPDATE raw_001 SET c001='bad'",
            "CREATE TABLE evil(i INTEGER)", "DROP TABLE raw_001", "ALTER TABLE raw_001 ADD COLUMN x INTEGER",
            "CREATE MACRO evil() AS 1", "CREATE SECRET s(TYPE s3,KEY_ID 'fixture',SECRET 'fixture')",
            "SET threads=10", "RESET threads", "SET VARIABLE x=1", "BEGIN", "COMMIT", "ROLLBACK",
            "INSTALL httpfs", "LOAD httpfs", "ATTACH ':memory:' AS other", "DETACH other"]
        for sql in cases:
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                self.query(sql)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM raw_001").fetchone(), (3,))

    def test_file_network_dynamic_and_python_functions_are_rejected(self):
        sentinel = self.root / "sentinel.csv"
        sentinel.write_text("private\nfixture\n")
        digest = hashlib.sha256(sentinel.read_bytes()).hexdigest()
        quoted = str(sentinel).replace("'", "''")
        cases = [f"SELECT * FROM read_csv('{quoted}')", f"SELECT * FROM read_text('{quoted}')", f"SELECT * FROM read_blob('{quoted}')",
            f"SELECT * FROM '{quoted}'", f"SELECT * FROM glob('{quoted}')", "SELECT * FROM read_csv('https://example.invalid/data.csv')",
            "SELECT * FROM read_json('s3://fixture/data.json')", "SELECT * FROM pandas_scan(0)", "SELECT * FROM arrow_scan(0,0,0)",
            "SELECT * FROM query('SELECT * FROM forbidden')", "SELECT * FROM query_table('forbidden')",
            "SELECT * FROM json_execute_serialized_sql('{}')", "SELECT (SELECT content FROM read_text('missing'))",
            "WITH x AS(SELECT * FROM query('SELECT 1')) SELECT * FROM x", f"COPY raw_001 TO '{quoted}'", f"ATTACH '{quoted}' AS x"]
        for sql in cases:
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                self.query(sql)
        self.assertEqual(hashlib.sha256(sentinel.read_bytes()).hexdigest(), digest)
        self.assertEqual(list(self.root.iterdir()), [sentinel])
        # Independently exercise the engine settings without the SQL policy.
        for sql in (f"SELECT * FROM read_text('{quoted}')", f"COPY raw_001 TO '{quoted}'", "LOAD httpfs"):
            with self.subTest(engine=sql), self.assertRaises(duckdb.Error):
                self.connection.execute(sql)
        self.assertEqual(hashlib.sha256(sentinel.read_bytes()).hexdigest(), digest)

    def test_system_tables_views_and_staging_objects_are_rejected(self):
        self.connection.execute("CREATE VIEW view_data AS SELECT * FROM raw_001")
        self.connection.register("staging", pd.DataFrame({"private": [999]}))
        self.addCleanup(self.connection.unregister, "staging")
        for sql in ["SELECT * FROM information_schema.tables", "SELECT * FROM duckdb_tables()", "SELECT * FROM duckdb_secrets()",
                    "SELECT * FROM pg_catalog.pg_tables", "SELECT * FROM view_data", "SELECT * FROM staging",
                    "SELECT * FROM memory.main.raw_001", "SELECT * FROM forbidden"]:
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                self.query(sql)
        for names in ({"view_data"}, {"staging"}, {"missing"}, "raw_001", {1}):
            with self.subTest(names=names), self.assertRaises(ValueError):
                self.query("SELECT 1", allowed=names)

    def test_parent_python_registration_cannot_shadow_allowed_physical_table(self):
        self.connection.register("raw_001", pd.DataFrame({"c001": ["private"]}))
        self.addCleanup(self.connection.unregister, "raw_001")
        self.assertEqual(self.query("SELECT count(*) FROM raw_001"), [(3,)])

    def test_side_effecting_functions_and_user_definitions_are_rejected(self):
        self.connection.execute("CREATE SEQUENCE seq")
        self.connection.execute("CREATE MACRO evil() AS (SELECT secret FROM forbidden)")
        self.connection.execute("CREATE MACRO table_evil() AS TABLE SELECT * FROM forbidden")
        calls = []
        self.connection.create_function("python_evil", lambda: calls.append(True) or 999, return_type="INTEGER", side_effects=False)
        for sql in ["SELECT nextval('seq')", "SELECT currval('seq')", "SELECT setseed(0.5)", "SELECT evil()",
                    "SELECT python_evil()", "SELECT * FROM table_evil()"]:
            with self.subTest(sql=sql), self.assertRaisesRegex(ValueError, "function is not permitted"):
                self.query(sql)
        self.assertEqual(self.connection.execute("SELECT nextval('seq')").fetchone(), (1,))
        self.assertEqual(calls, [])

    def test_builtin_macro_overrides_are_not_trusted_as_original_functions(self):
        self.connection.execute("CREATE OR REPLACE MACRO date_add(a,b) AS (SELECT secret FROM forbidden)")
        with self.assertRaisesRegex(ValueError, "function is not permitted"):
            self.query("SELECT date_add(DATE '2024-01-01',INTERVAL 1 DAY)")

    def test_unknown_tree_structures_fail_closed(self):
        for tree in ({"type": "NEW_QUERY_NODE"}, {"class": "UNKNOWN_EXPRESSION", "type": "X"},
                     {"type": "NEW_TABLE_REFERENCE"}, {"class": "TYPE", "catalog": "other"}):
            with self.subTest(tree=tree), self.assertRaises(ValueError):
                data_tools._validate_sql_tree(tree, set(), set(), set())

    def test_insecure_connections_and_invalid_timeouts_are_rejected(self):
        with closing(duckdb.connect(":memory:")) as insecure:
            with self.assertRaisesRegex(ValueError, "secured connection"):
                with data_tools.restricted_query(insecure, "SELECT 1", allowed_tables=set()):
                    self.fail("Unsafe connection admitted")
            self.assertEqual(insecure.execute("SELECT 2").fetchone(), (2,))
        for seconds in (0, -1, True, float('nan'), float('inf'), '1'):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                self.query("SELECT 1", timeout_seconds=seconds)

    def test_binder_and_conversion_errors_remain_actionable_and_recoverable(self):
        for sql, error in [("SELECT missing FROM raw_001", duckdb.BinderException), ("SELECT CAST('bad' AS INTEGER)", duckdb.ConversionException)]:
            with self.subTest(sql=sql), self.assertRaises(error):
                self.query(sql)
            self.assertEqual(self.query("SELECT count(*) FROM raw_001"), [(3,)])

    def test_caller_exception_early_close_and_fetchmany_leave_parent_open(self):
        with self.assertRaisesRegex(RuntimeError, "caller failure"):
            with data_tools.restricted_query(self.connection, "SELECT * FROM raw_001", allowed_tables={"raw_001"}) as cursor:
                cursor.fetchmany(1)
                raise RuntimeError("caller failure")
        with data_tools.restricted_query(self.connection, "SELECT * FROM raw_001", allowed_tables={"raw_001"}) as cursor:
            self.assertEqual(len(cursor.fetchmany(1)), 1)
            cursor.close()
        self.assertEqual(self.query("SELECT 42"), [(42,)])
        self.connection.execute("INSERT INTO raw_001 VALUES ('003',NULL)")
        self.assertEqual(self.query("SELECT count(*) FROM raw_001"), [(4,)])

    def test_query_cursor_uses_an_actual_read_only_transaction(self):
        with self.assertRaisesRegex(duckdb.TransactionException, "read-only"):
            with data_tools.restricted_query(self.connection, "SELECT 1", allowed_tables=set()) as cursor:
                cursor.execute("DELETE FROM raw_001")
        self.assertEqual(self.query("SELECT count(*) FROM raw_001"), [(3,)])

    def test_timer_is_cancelled_and_joined_after_success_and_error(self):
        baseline = {t.ident for t in threading.enumerate()}
        self.query("SELECT 1")
        with self.assertRaises(ValueError):
            self.query("DELETE FROM raw_001")
        self.assertEqual({t.ident for t in threading.enumerate()}, baseline)

    def test_timer_start_failure_does_not_close_parent_connection(self):
        with patch.object(threading.Timer, "start", side_effect=RuntimeError("cannot start timer")), self.assertRaisesRegex(RuntimeError, "cannot start timer"):
            self.query("SELECT 1")
        self.assertEqual(self.query("SELECT 42"), [(42,)])

    def test_real_timeout_and_consumption_deadline_in_bounded_subprocess(self):
        code = '''
from contextlib import closing
import json, time, threading
from data_tools import create_connection, restricted_query
from unittest.mock import patch
with closing(create_connection()) as c:
    baseline={t.ident for t in threading.enumerate()}
    start=time.monotonic()
    try:
        with restricted_query(c,"SELECT sum(i) FROM range(100000000000) t(i)",allowed_tables=set(),timeout_seconds=.1) as r:
            r.fetchall()
        raise AssertionError("Expensive query finished")
    except TimeoutError:
        elapsed=time.monotonic()-start
    assert .07 <= elapsed < 3,elapsed
    with restricted_query(c,"SELECT 42",allowed_tables=set()) as r:
        assert r.fetchone()==(42,)
    try:
        with restricted_query(c,"SELECT * FROM range(100)",allowed_tables=set(),timeout_seconds=.1) as r:
            r.fetchmany(1)
            time.sleep(.2)
        raise AssertionError("Consumption escaped deadline")
    except TimeoutError:
        pass
    # Expiry while Python validation is running must prevent a later expensive
    # native query; a one-shot interrupt would otherwise be lost between calls.
    with patch('data_tools._validate_sql', side_effect=lambda *a:time.sleep(.15)):
        try:
            with restricted_query(c,"SELECT sum(i) FROM range(100000000000) t(i)",allowed_tables=set(),timeout_seconds=.05) as r:
                r.fetchall()
            raise AssertionError("Expired validation admitted native execution")
        except TimeoutError:
            pass
    assert {t.ident for t in threading.enumerate()}==baseline
    with restricted_query(c,"SELECT 7",allowed_tables=set()) as r:
        assert r.fetchone()==(7,)
    print(json.dumps({"timeout_seconds":elapsed,"recovery":True,"consumption_deadline":True}))
'''
        result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["recovery"])

    def test_validation_is_included_in_deadline(self):
        original = data_tools._validate_sql

        def slow(*args):
            time.sleep(.08)
            return original(*args)

        with patch.object(data_tools, "_validate_sql", side_effect=slow), self.assertRaises(TimeoutError):
            self.query("SELECT 1", timeout_seconds=.03)
        self.assertEqual(self.query("SELECT 1"), [(1,)])

    def test_all_trusted_loaders_and_calendar_work_on_locked_connection(self):
        (self.root / "a.csv").write_text("ID,Value\n0001,79k\n")
        workbook = Workbook()
        workbook.active.append(["ID", "Value"])
        workbook.active.append(["0002", 12])
        workbook.save(self.root / "b.xlsx")
        workbook.close()
        with closing(sqlite3.connect(self.root / "c.sqlite")) as original:
            original.execute("CREATE TABLE t(ID,Value)")
            original.execute("INSERT INTO t VALUES('0003',13)")
            original.commit()
        inventory = data_tools.build_inventory(self.root)
        loaded = data_tools.load_sources(self.connection, self.root, inventory,
            [{"source_ids": ["S001"], "header_rows": [1], "first_data_row": 2},
             {"source_ids": ["S002"], "header_rows": [1], "first_data_row": 2}, {"source_ids": ["S003"]}])
        self.assertEqual([m["row_count"] for m in loaded["source_catalog"]], [1, 1, 1])
        for metadata in loaded["tables"]:
            self.assertEqual(self.query(f"SELECT count(*) FROM {metadata['table']}", allowed={metadata["table"]}), [(1,)])
        calendar = self.root / "calendar.csv"
        calendar.write_text("date,weekday,lunar_day,lunar_month,lunar_year,lunar_leap,holiday\n2024-02-10,6,1,1,2024,false,Tết\n")
        data_tools.register_calendar(self.connection, calendar)
        self.assertEqual(self.query("SELECT holiday FROM calendar", allowed={"calendar"}), [("Tết",)])
        self.assertFalse(any(name.startswith('_') for (name,) in self.connection.execute('SHOW TABLES').fetchall()))


if __name__ == "__main__":
    unittest.main()
