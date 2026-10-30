from contextlib import closing, contextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import json
import math
from pathlib import Path
import pickle
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID

import duckdb
import data_tools


def json_size(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))


class ResultTests(unittest.TestCase):
    def setUp(self):
        self.connection = data_tools.create_connection()
        self.addCleanup(self.connection.close)
        self.store = data_tools.ResultStore("câu_hỏi_01")
        self.addCleanup(self.store.close)

    def execute(self, sql, *, store=None, **kwargs):
        return data_tools.execute_sql(self.connection, sql, store=self.store if store is None else store,
                                      allowed_tables=kwargs.pop("allowed_tables", set()), purpose="Kiểm tra dữ liệu", **kwargs)

    def rows(self, observation, store=None):
        return list((self.store if store is None else store).iter_rows(observation["result_ref"]))

    def test_complete_result_is_independent_of_default_preview(self):
        result = self.execute("SELECT i FROM range(10000) t(i) ORDER BY i DESC")
        self.assertEqual(result["row_count"], 10000)
        self.assertEqual(result["preview_row_count"], 12)
        self.assertEqual(result["preview_rows"], [[n] for n in range(9999, 9987, -1)])
        self.assertTrue(result["is_preview"])
        self.assertTrue(result["preview_truncated"])
        self.assertEqual(self.rows(result), [(n,) for n in range(9999, -1, -1)])
        self.assertLessEqual(json_size(result), 16 * 1024)
        self.assertEqual(self.store.used_bytes, result["stored_bytes"])

    def test_preview_size_changes_do_not_change_complete_rows(self):
        sql = "SELECT i,repeat('Tiếng Việt ',30) text FROM range(25) t(i) ORDER BY i"
        one = self.execute(sql, preview_rows=1)
        many = self.execute(sql, preview_rows=12)
        small = self.execute(sql, preview_bytes=1500)
        self.assertEqual(self.rows(one), self.rows(many))
        self.assertEqual(self.rows(one), self.rows(small))
        self.assertLess(small["preview_row_count"], many["preview_row_count"])
        for row in small["preview_rows"]:
            self.assertEqual(row[1], "Tiếng Việt " * 30)
        self.assertLessEqual(json_size(small), 1500)

    def test_duplicates_nulls_empty_strings_and_whitespace_are_preserved(self):
        result = self.execute("SELECT * FROM (VALUES (1,NULL,'0001',''),(1,NULL,'0001',''),(2,4,' 79k ','NA')) t(n,x,id,text) ORDER BY n")
        self.assertEqual(self.rows(result), [(1, None, "0001", ""), (1, None, "0001", ""), (2, 4, " 79k ", "NA")])
        self.assertFalse(result["preview_truncated"])

    def test_zero_rows_keep_schema_and_zero_row_preview(self):
        result = self.execute("SELECT 1::BIGINT n, 'abc' label WHERE false")
        self.assertEqual(result["schema"], [{"position": 1, "name": "n", "type": "BIGINT"}, {"position": 2, "name": "label", "type": "VARCHAR"}])
        self.assertEqual((result["row_count"], result["preview_row_count"], result["preview_rows"]), (0, 0, []))
        self.assertFalse(result["preview_truncated"])
        self.assertEqual(self.rows(result), [])

    def test_exact_native_types_and_duplicate_column_names(self):
        sql = """SELECT 12345678901234567890::HUGEINT same,123.40::DECIMAL(18,2) same,
            DATE '2024-02-10' d,TIMESTAMP '2024-02-10 12:34:56' dt,TIME '12:34:56' t,
            INTERVAL '2 days' span,UUID '12345678-1234-1234-1234-123456789abc' id,
            from_hex('0001') blob_value,[1,NULL,2] items,{'name':'Cà phê','active':true} obj,map([1],[2]) mapping"""
        result = self.execute(sql)
        expected = (12345678901234567890, Decimal("123.40"), date(2024, 2, 10), datetime(2024, 2, 10, 12, 34, 56),
                    time(12, 34, 56), timedelta(days=2), UUID("12345678-1234-1234-1234-123456789abc"), b'\x00\x01',
                    [1, None, 2], {"name": "Cà phê", "active": True}, {1: 2})
        actual = self.rows(result)[0]
        self.assertEqual(actual, expected)
        self.assertEqual([type(v) for v in actual], [type(v) for v in expected])
        self.assertEqual([c["name"] for c in result["schema"][:2]], ["same", "same"])
        self.assertEqual([c["position"] for c in result["schema"]], list(range(1, 12)))
        preview = result["preview_rows"][0]
        self.assertEqual(preview[1], {"type": "decimal", "value": "123.40"})
        self.assertEqual(preview[7], {"type": "bytes", "encoding": "base64", "value": "AAE="})
        self.assertEqual(preview[10], {"type": "map", "entries": [[1, 2]]})
        json.dumps(result, ensure_ascii=False, allow_nan=False)

    def test_nonfinite_values_stay_native_with_strict_json_previews(self):
        result = self.execute("SELECT 'NaN'::DOUBLE n,'Infinity'::DOUBLE p,'-Infinity'::DOUBLE m")
        native = self.rows(result)[0]
        self.assertTrue(math.isnan(native[0]))
        self.assertEqual(native[1:], (float('inf'), -float('inf')))
        self.assertEqual([v["type"] for v in result["preview_rows"][0]], ["float"] * 3)
        json.dumps(result, allow_nan=False)
        self.assertEqual(data_tools._result_preview_value(Decimal('NaN')), {"type": "decimal", "value": "NaN"})

    def test_description_preserves_sql_purpose_question_and_is_mutation_safe(self):
        sql = "SELECT 1 n"
        result = self.execute(sql)
        first = self.store.describe(result["result_ref"])
        self.assertEqual((first["sql"], first["purpose"], first["question_id"]), (sql, "Kiểm tra dữ liệu", "câu_hỏi_01"))
        first["schema"][0]["name"] = "changed"
        result["schema"][0]["name"] = "also_changed"
        self.assertEqual(self.store.describe(result["result_ref"])["schema"][0]["name"], "n")

    def test_native_retrieval_and_preview_mutations_do_not_change_store(self):
        result = self.execute("SELECT [1,2] items,{'nested':[3,4]} obj")
        rows = self.rows(result)
        rows[0][0].append(999)
        rows[0][1]["nested"].clear()
        result["preview_rows"][0][0].clear()
        self.assertEqual(self.rows(result), [([1, 2], {"nested": [3, 4]})])

    def test_payload_budget_exact_boundary_and_one_byte_below(self):
        sql = "SELECT '123456789' x"
        size = self.execute(sql)["stored_bytes"]
        with data_tools.ResultStore(self.store.question_id, max_bytes=size) as exact:
            result = self.execute(sql, store=exact)
            self.assertEqual(result["stored_bytes"], size)
            self.assertEqual(exact.used_bytes, size)
        with data_tools.ResultStore(self.store.question_id, max_bytes=size - 1) as too_small:
            with self.assertRaisesRegex(ValueError, "budget"):
                self.execute(sql, store=too_small)
            self.assertEqual(too_small.used_bytes, 0)

    def test_shared_budget_preserves_previous_result_after_capacity_failure(self):
        sql = "SELECT 'payload' x"
        size = self.execute(sql)["stored_bytes"]
        with data_tools.ResultStore(self.store.question_id, max_bytes=2 * size - 1) as limited:
            first = self.execute(sql, store=limited)
            with self.assertRaisesRegex(ValueError, "budget"):
                self.execute(sql, store=limited)
            self.assertEqual(limited.used_bytes, size)
            self.assertEqual(self.rows(first, limited), [("payload",)])

    def test_late_chunk_capacity_failure_does_not_publish_partial_result(self):
        first_chunk_size = self.execute("SELECT i FROM range(4096) t(i)")["stored_bytes"]
        with data_tools.ResultStore(self.store.question_id, max_bytes=first_chunk_size) as limited:
            original = data_tools.pickle.dumps
            chunks = []

            def dump(value, *args, **kwargs):
                if isinstance(value, tuple):
                    chunks.append(len(value))
                return original(value, *args, **kwargs)

            with patch.object(data_tools.pickle, "dumps", side_effect=dump), self.assertRaisesRegex(ValueError, "budget"):
                self.execute("SELECT i FROM range(6000) t(i)", store=limited)
            self.assertEqual(chunks, [4096, 1904])
            self.assertEqual(limited.used_bytes, 0)

    def test_preview_json_budget_exact_boundary_and_whole_row_reduction(self):
        sql = "SELECT i,repeat('ế',40) text FROM range(3) t(i) ORDER BY i"
        baseline = self.execute(sql)
        size = json_size(baseline)
        exact = self.execute(sql, preview_bytes=size)
        reduced = self.execute(sql, preview_bytes=size - 1)
        self.assertEqual(exact["preview_row_count"], 3)
        self.assertEqual(json_size(exact), size)
        self.assertEqual(reduced["preview_row_count"], 2)
        self.assertEqual(reduced["preview_rows"], [[0, 'ế' * 40], [1, 'ế' * 40]])
        self.assertLessEqual(json_size(reduced), size - 1)
        self.assertEqual(self.rows(exact), self.rows(reduced))

    def test_oversized_first_row_keeps_full_cell_and_returns_explanation(self):
        result = self.execute("SELECT repeat('Tiếng Việt ',3000) text")
        self.assertEqual(result["preview_rows"], [])
        self.assertEqual(result["preview_omitted_reason"], "first_row_exceeds_byte_budget")
        self.assertIn("select fewer columns", result["instruction"])
        self.assertTrue(result["preview_truncated"])
        self.assertEqual(self.rows(result), [("Tiếng Việt " * 3000,)])
        self.assertLessEqual(json_size(result), 16 * 1024)

    def test_metadata_alone_oversized_is_atomic(self):
        good = self.execute("SELECT 42")
        used = self.store.used_bytes
        for sql, budget in [("SELECT 1", 1), ('SELECT 1 AS "' + 'ế' * 6000 + '"', 16 * 1024)]:
            with self.subTest(budget=budget), self.assertRaisesRegex(ValueError, "metadata exceeds"):
                self.execute(sql, preview_bytes=budget)
        self.assertEqual(self.store.used_bytes, used)
        self.assertEqual(self.rows(good), [(42,)])

    def test_metadata_only_preview_still_stores_every_row(self):
        result = self.execute("SELECT * FROM range(15)", preview_rows=0)
        self.assertEqual(result["preview_rows"], [])
        self.assertEqual(result["preview_omitted_reason"], "disabled_by_host")
        self.assertEqual(len(self.rows(result)), 15)

    def test_cross_attempt_clear_close_and_active_iterator_invalidation(self):
        result = self.execute("SELECT * FROM range(3)")
        with data_tools.ResultStore(self.store.question_id) as other:
            with self.assertRaisesRegex(ValueError, "other-attempt"):
                other.describe(result["result_ref"])
            different = self.execute("SELECT 1", store=other)
            self.assertNotEqual(different["result_ref"], result["result_ref"])
        iterator = self.store.iter_rows(result["result_ref"])
        self.assertEqual(next(iterator), (0,))
        self.store.clear()
        self.assertEqual(self.store.used_bytes, 0)
        with self.assertRaises(ValueError):
            next(iterator)
        with self.assertRaises(ValueError):
            self.store.describe(result["result_ref"])
        fresh = self.execute("SELECT 7")
        self.assertNotEqual(fresh["result_ref"], result["result_ref"])
        self.store.close()
        self.store.close()
        for operation in (lambda:self.store.describe(fresh["result_ref"]), lambda:self.store.iter_rows(fresh["result_ref"]),
                          self.store.clear, lambda:self.execute("SELECT 1"), lambda:self.store.__enter__()):
            with self.assertRaises(ValueError):
                operation()

    def test_context_exit_closes_store_on_caller_error(self):
        store = data_tools.ResultStore("failure")
        with self.assertRaisesRegex(RuntimeError, "caller"):
            with store:
                self.execute("SELECT 1", store=store)
                raise RuntimeError("caller")
        self.assertEqual(store.used_bytes, 0)
        with self.assertRaises(ValueError):
            store.describe("anything")

    def test_results_survive_source_changes_and_database_closure(self):
        self.connection.execute("CREATE TABLE source(value INTEGER)")
        self.connection.execute("INSERT INTO source VALUES(1)")
        result = self.execute("SELECT * FROM source", allowed_tables={"source"})
        self.connection.execute("DELETE FROM source")
        self.connection.close()
        self.assertEqual(self.rows(result), [(1,)])

    def test_rejection_and_conversion_failures_leave_prior_result_unchanged(self):
        good = self.execute("SELECT 42")
        used = self.store.used_bytes
        for sql, error in [("DELETE FROM missing", ValueError), ("SELECT * FROM forbidden", ValueError),
                           ("SELECT CAST('bad' AS INTEGER)", duckdb.ConversionException)]:
            with self.subTest(sql=sql), self.assertRaises(error):
                self.execute(sql)
        self.assertEqual(self.store.used_bytes, used)
        self.assertEqual(self.rows(good), [(42,)])

    def test_unsupported_values_never_reach_pickle_even_outside_preview(self):
        called = []

        class Unsupported:
            def __reduce__(self):
                called.append(True)
                return (str, ("unsafe",))

        @contextmanager
        def fake_query(*args, **kwargs):
            batches = iter([[(1,), (Unsupported(),)], []])
            yield SimpleNamespace(description=[("value", "CUSTOM")], fetchmany=lambda n:next(batches))

        good = self.execute("SELECT 42")
        used = self.store.used_bytes
        with patch.object(data_tools, "restricted_query", fake_query), self.assertRaisesRegex(ValueError, "Unsupported SQL result"):
            self.execute("SELECT fixture", preview_rows=1)
        self.assertEqual(called, [])
        self.assertEqual(self.store.used_bytes, used)
        self.assertEqual(self.rows(good), [(42,)])

    def test_serialization_failures_are_atomic(self):
        good = self.execute("SELECT 42")
        used = self.store.used_bytes
        original = data_tools.pickle.dumps
        for fail_kind in (tuple, dict):
            def dump(value, *args, **kwargs):
                if type(value) is fail_kind:
                    raise pickle.PicklingError("synthetic serialization failure")
                return original(value, *args, **kwargs)
            with self.subTest(kind=fail_kind), patch.object(data_tools.pickle, "dumps", side_effect=dump), self.assertRaisesRegex(ValueError, "serialize SQL result"):
                self.execute("SELECT 1")
            self.assertEqual(self.store.used_bytes, used)
        self.assertEqual(self.rows(good), [(42,)])

    def test_scope_change_during_pending_query_prevents_publication(self):
        original = data_tools._result_observation

        def observation(*args):
            value = original(*args)
            self.store.clear()
            return value

        with patch.object(data_tools, "_result_observation", side_effect=observation), self.assertRaisesRegex(ValueError, "scope changed"):
            self.execute("SELECT 1")
        self.assertEqual(self.store.used_bytes, 0)

    def test_invalid_arguments_do_not_start_query(self):
        for kwargs in ({"preview_rows": True}, {"preview_rows": -1}, {"preview_rows": 13},
                       {"preview_bytes": 0}, {"preview_bytes": True}, {"preview_bytes": 16385}, {"purpose": ""}, {"store": None}):
            with self.subTest(kwargs=kwargs), patch.object(data_tools, "restricted_query", side_effect=AssertionError("query started")), self.assertRaises(ValueError):
                data_tools.execute_sql(self.connection, "SELECT 1", store=kwargs.get("store", self.store), allowed_tables=set(),
                    purpose=kwargs.get("purpose", "test"), **{k:v for k,v in kwargs.items() if k not in {"store", "purpose"}})
        for question in (None, "", "  "):
            with self.subTest(question=question), self.assertRaises(ValueError):
                data_tools.ResultStore(question)
        for budget in (True, 0, -1, 1.5):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                data_tools.ResultStore("q", max_bytes=budget)
        self.assertEqual(self.store.max_bytes, 32 * 1024**2)

    def test_timeouts_during_consumption_serialization_and_scope_exit_are_atomic(self):
        code = '''
from contextlib import closing
import time,json
from unittest.mock import patch
import data_tools
with closing(data_tools.create_connection()) as c,data_tools.ResultStore('timeout') as s:
    good=data_tools.execute_sql(c,'SELECT 42',store=s,allowed_tables=set(),purpose='keep')
    used=s.used_bytes
    original=data_tools.pickle.dumps
    def delayed(value,*args,**kwargs):
        time.sleep(.08)
        return original(value,*args,**kwargs)
    for target in ('serialization','consumption','preview'):
        if target=='serialization':
            patcher=patch.object(data_tools.pickle,'dumps',side_effect=delayed)
        elif target=='consumption':
            validate=data_tools._validate_result_value
            def slow(value):
                time.sleep(.08)
                return validate(value)
            patcher=patch.object(data_tools,'_validate_result_value',side_effect=slow)
        else:
            observe=data_tools._result_observation
            def slow_preview(*args):
                time.sleep(.08)
                return observe(*args)
            patcher=patch.object(data_tools,'_result_observation',side_effect=slow_preview)
        with patcher:
            try:
                data_tools.execute_sql(c,'SELECT 1',store=s,allowed_tables=set(),purpose='timeout',timeout_seconds=.04)
                raise AssertionError('Timeout did not fire')
            except TimeoutError:
                pass
        assert s.used_bytes==used
        assert list(s.iter_rows(good['result_ref']))==[(42,)]
    fresh=data_tools.execute_sql(c,'SELECT 7',store=s,allowed_tables=set(),purpose='recover')
    assert list(s.iter_rows(fresh['result_ref']))==[(7,)]
    print(json.dumps({'atomic_timeouts':3,'recovery':True}))
'''
        completed = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=8)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"atomic_timeouts": 3, "recovery": True})


if __name__ == "__main__":
    unittest.main()
