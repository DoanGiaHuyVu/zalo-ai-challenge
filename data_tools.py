"""Trusted data access: inventories, raw inspection, and explicit data loaders."""

from __future__ import annotations

from contextlib import closing, contextmanager, suppress
import base64
import csv
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import hashlib
import io
from itertools import islice
import json
import math
import os
from pathlib import Path
import pickle
import re
import sqlite3
import threading
import time as clock
from typing import Any
from uuid import UUID, uuid4
from weakref import WeakKeyDictionary
import xml.etree.ElementTree as ET
import zipfile

import duckdb
from openpyxl import load_workbook
import pandas as pd


_FORMATS = {".csv": "csv", ".xlsx": "xlsx", ".sqlite": "sqlite", ".sqlite3": "sqlite", ".db": "sqlite"}
_CALENDAR_SCHEMA = [("date", "DATE"), ("weekday", "INTEGER"), ("lunar_day", "INTEGER"),
                    ("lunar_month", "INTEGER"), ("lunar_year", "INTEGER"),
                    ("lunar_leap", "BOOLEAN"), ("holiday", "VARCHAR")]
INSPECT_MAX_SOURCES = 8
INSPECT_MAX_ROWS = 50
INSPECT_MAX_BYTES = 16 * 1024
CSV_CHUNK_ROWS = 4096
EXCEL_CHUNK_ROWS = 4096
SQLITE_CHUNK_ROWS = 4096
RESULT_CHUNK_ROWS = 4096
RESULT_MAX_BYTES = 32 * 1024**2
RESULT_PREVIEW_ROWS = 12
RESULT_PREVIEW_BYTES = 16 * 1024

_SQL_SECURITY = {
    "enable_external_access": False, "autoinstall_known_extensions": False,
    "autoload_known_extensions": False, "allow_unsigned_extensions": False,
    "allow_community_extensions": False, "python_enable_replacements": False,
    "python_scan_all_frames": False, "allowed_paths": "[]", "allowed_directories": "[]",
    "temp_directory": "",
}
_SQL_TABLE_FUNCTIONS = frozenset({"range", "generate_series", "unnest", "json_each", "json_tree"})
_SQL_MACROS = frozenset({"if", "nullif", "date_add"})
_SQL_QUERY_NODES = frozenset({"SELECT_NODE", "SET_OPERATION_NODE", "RECURSIVE_CTE_NODE"})
_SQL_TABLE_NODES = frozenset({"BASE_TABLE", "EMPTY", "JOIN", "SUBQUERY", "TABLE_FUNCTION", "EXPRESSION_LIST"})
_SQL_EXPRESSIONS = frozenset({"CONSTANT", "COLUMN_REF", "STAR", "FUNCTION", "WINDOW", "CAST", "CASE",
                            "COMPARISON", "CONJUNCTION", "OPERATOR", "SUBQUERY", "LAMBDA", "BETWEEN",
                            "TYPE", "POSITIONAL_REFERENCE", "COLLATE"})
_SQL_WRAPPERS = frozenset({"ORDER_MODIFIER", "DISTINCT_MODIFIER", "LIMIT_MODIFIER", "LIMIT_PERCENT_MODIFIER",
                         "ORDER_DEFAULT", "ASCENDING", "DESCENDING"})
_SQL_CATALOGS = WeakKeyDictionary()


def create_connection(*, memory_limit: str = "1GB", threads: int = 2):
    """Caller-owned in-memory database; only trusted Python code may ingest data.

    Settings are defense in depth, not an OS sandbox. Do not install host UDFs,
    custom casts, macros or extensions on a connection exposed to model SQL.
    """
    if duckdb.__version__ != "2.0.0":
        raise ValueError("SQL policy requires the pinned DuckDB 2.0.0 parser/catalog")
    if not isinstance(memory_limit, str) or not memory_limit.strip() or type(threads) is not int or threads < 1:
        raise ValueError("Expected a memory-limit string and positive integer threads")
    startup = {key: value for key, value in _SQL_SECURITY.items()
               if key not in {"allowed_paths", "allowed_directories", "enable_external_access"}}
    connection = duckdb.connect(":memory:", config={**startup, "memory_limit": memory_limit, "threads": threads})
    try:
        # Starting with external access off adds pseudo database/WAL paths to
        # DuckDB's allowlist. Clear the lists before disabling access and locking;
        # no source/model SQL runs during this private bootstrap sequence.
        connection.execute("SET allowed_paths = []")
        connection.execute("SET allowed_directories = []")
        connection.execute("SET enable_external_access = false")
        connection.execute("SET lock_configuration = true")
        # Python UDFs also claim internal=True in this runtime. Pin identities
        # before host registrations; later additions/overrides are not built-ins.
        _SQL_CATALOGS[connection] = frozenset(connection.execute(
            "SELECT function_name, function_type, function_oid FROM duckdb_functions()").fetchall())
        return connection
    except Exception:
        connection.close()
        raise


def _check_sql_connection(connection: Any) -> None:
    settings = dict(connection.execute("SELECT name, value FROM duckdb_settings()").fetchall())
    for name, expected in {**_SQL_SECURITY, "lock_configuration": True}.items():
        value = str(expected).lower() if isinstance(expected, bool) else expected
        if settings.get(name) != value:
            raise ValueError(f"SQL requires a secured connection: {name} must be {value!r}; use create_connection()")


def _sql_functions(connection: Any, baseline: frozenset) -> tuple[set[str], set[str]]:
    rows = connection.execute("SELECT function_name, function_type, function_oid, internal, has_side_effects FROM duckdb_functions()").fetchall()
    scalar, table = {}, {}
    for name, kind, oid, internal, effects in rows:
        original = (name, kind, oid) in baseline
        name = name.casefold()
        if kind == "table":
            table[name] = table.get(name, True) and original and internal and name in _SQL_TABLE_FUNCTIONS
        elif kind == "table_macro":
            table[name] = False
        elif kind in {"scalar", "aggregate", "window", "macro"}:
            pure = effects is False if kind != "macro" else name in _SQL_MACROS
            scalar[name] = scalar.get(name, True) and original and internal and pure
    return {name for name, allowed in scalar.items() if allowed}, {name for name, allowed in table.items() if allowed}


def _validate_sql_tree(tree: Any, allowed_tables: set[str], scalar_functions: set[str], table_functions: set[str]) -> None:
    """Walk the pinned parser tree, retaining lexical CTE visibility."""
    if not isinstance(tree, dict) or tree.get("type") not in _SQL_QUERY_NODES:
        raise ValueError("Unsupported SQL query tree")
    def function(node, permitted):
        name = node.get("function_name", "").casefold()
        if node.get("catalog") or node.get("schema", "") not in ("", "main") or name not in permitted:
            raise ValueError(f"SQL function is not permitted: {node.get('function_name')!r}")

    def walk(value, scope=frozenset(), table_function=False):
        if isinstance(value, list):
            for item in value:
                walk(item, scope)
            return
        if not isinstance(value, dict):
            return
        kind, expression = value.get("type"), value.get("class")
        if kind in _SQL_QUERY_NODES:
            local = set(scope)
            definitions = value.get("cte_map", {}).get("map", [])
            for entry in definitions:
                # An ordinary CTE does not see itself: DuckDB may otherwise bind
                # that name to a real table, including one outside our allowlist.
                walk(entry["value"]["query_node"], frozenset(local))
                local.add(entry["key"].casefold())
                for key, item in entry["value"].items():
                    if key != "query_node":
                        walk(item, frozenset(local))
            local = frozenset(local)
            if kind == "RECURSIVE_CTE_NODE":
                # Only the recursive arm sees the CTE's own name, not its anchor.
                walk(value["left"], local)
                walk(value["right"], local | {value["cte_name"].casefold()})
            for key, item in value.items():
                if key != "cte_map" and not (kind == "RECURSIVE_CTE_NODE" and key in {"left", "right"}):
                    walk(item, local)
            return
        if expression:
            if expression not in _SQL_EXPRESSIONS:
                raise ValueError(f"Unsupported SQL expression: {expression}")
            if expression in {"FUNCTION", "WINDOW"}:
                function(value, table_functions if table_function else scalar_functions)
            if expression == "TYPE" and (value.get("catalog") or value.get("schema")):
                raise ValueError("Qualified/custom SQL types are not permitted")
        elif kind in _SQL_TABLE_NODES:
            if kind == "BASE_TABLE":
                name = value["table_name"].casefold()
                schema, catalog = value.get("schema_name", ""), value.get("catalog_name", "")
                cte = not schema and not catalog and name in scope
                if not cte and (catalog or schema not in ("", "main") or name not in allowed_tables):
                    raise ValueError(f"SQL table is not permitted: {value['table_name']!r}")
                if value.get("at_clause") is not None:
                    raise ValueError("Temporal table references are not permitted")
            if kind == "TABLE_FUNCTION":
                if value.get("function", {}).get("class") != "FUNCTION":
                    raise ValueError("Expected a permitted named table function")
                walk(value["function"], scope, table_function=True)
        elif kind is not None and kind not in _SQL_WRAPPERS:
            raise ValueError(f"Unsupported SQL structure: {kind}")
        for key, item in value.items():
            if not (kind == "TABLE_FUNCTION" and key == "function"):
                walk(item, scope)

    walk(tree)


def _validate_sql(connection: Any, sql: str, allowed_tables: Any, baseline: frozenset) -> None:
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("SQL must be a nonempty string")
    if not isinstance(allowed_tables, (set, frozenset, list, tuple)) or any(not isinstance(name, str) or not name for name in allowed_tables):
        raise ValueError("allowed_tables must contain host-selected physical table names")
    allowed = {name.casefold() for name in allowed_tables}
    physical = {name.casefold() for (name,) in connection.execute(
        "SELECT table_name FROM duckdb_tables() WHERE schema_name='main' AND NOT internal AND NOT temporary").fetchall()}
    if not allowed <= physical:
        raise ValueError(f"Allowed tables must be physical main tables, not views/staging objects: {sorted(allowed - physical)}")
    try:
        statements = connection.extract_statements(sql)
    except duckdb.Error as exc:
        raise ValueError(f"Cannot parse SQL: {exc}") from exc
    if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
        raise ValueError("SQL must contain exactly one SELECT statement (CTEs are allowed)")
    if statements[0].named_parameters:
        raise ValueError("SQL must not contain unbound parameters")
    serialized = json.loads(connection.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0])
    if serialized.get("error") or len(serialized.get("statements", [])) != 1:
        raise ValueError(f"Unsupported SQL syntax: {serialized.get('error_message', 'cannot inspect syntax tree')}")
    scalar, table = _sql_functions(connection, baseline)
    try:
        _validate_sql_tree(serialized["statements"][0]["node"], allowed, scalar, table)
    except (KeyError, TypeError, RecursionError) as exc:
        raise ValueError("Unsupported SQL syntax tree for pinned runtime") from exc


@contextmanager
def restricted_query(connection: Any, sql: str, *, allowed_tables: Any, timeout_seconds: float = 10):
    """Yield a host-only cursor; validate/execute/consume inside one deadline.

    Requires an idle caller-owned connection and serial access. A dedicated
    cursor owns the read transaction: closing it early leaves the caller open.
    Publish results only after this context exits successfully. Cancellation is
    cooperative; the later process watchdog remains the hard failure boundary.
    """
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be a finite positive number")
    baseline = _SQL_CATALOGS.get(connection)
    if baseline is None:
        raise ValueError("SQL requires a secured connection from create_connection()")
    cursor = connection.cursor()
    expired = threading.Event()
    finished = threading.Event()
    deadline = clock.monotonic() + timeout_seconds

    def interrupt():
        expired.set()
        # A one-shot interrupt can arrive between validation and execute, when
        # no native query is active. Keep interrupting until cleanup acknowledges
        # cancellation, closing that race without leaving a late timer behind.
        while not finished.is_set():
            with suppress(duckdb.Error):
                cursor.interrupt()
            finished.wait(.01)

    timer = threading.Timer(timeout_seconds, interrupt)
    timer.daemon = True
    started = False
    timer_started = False
    try:
        timer.start()
        timer_started = True
        _check_sql_connection(cursor)
        cursor.execute("BEGIN TRANSACTION READ ONLY")
        started = True
        _validate_sql(cursor, sql, allowed_tables, baseline)
        if expired.is_set() or clock.monotonic() >= deadline:
            raise TimeoutError(f"SQL exceeded {timeout_seconds:g}-second deadline")
        cursor.execute(sql)
        yield cursor
    except Exception as exc:
        if expired.is_set() or clock.monotonic() >= deadline:
            raise TimeoutError(f"SQL exceeded {timeout_seconds:g}-second deadline") from exc
        raise
    finally:
        finished.set()
        timer.cancel()
        if timer_started:
            timer.join()
        if started:
            with suppress(duckdb.Error):
                cursor.execute("ROLLBACK")
        cursor.close()
    if expired.is_set() or clock.monotonic() >= deadline:
        raise TimeoutError(f"SQL exceeded {timeout_seconds:g}-second deadline")


class ResultStore:
    """Question-attempt-local, host-only typed payloads; never load external pickle.

    The byte budget counts serialized metadata/chunks, not total process RSS or
    temporary decoded objects. Use serial access and publish only complete SQL
    results. Stored immutable bytes isolate results from caller mutations.
    """
    def __init__(self, question_id: str, *, max_bytes: int = RESULT_MAX_BYTES):
        if type(question_id) is not str or not question_id.strip():
            raise ValueError("question_id must be a nonempty string")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        self._question_id = question_id
        self._max_bytes = max_bytes
        self._used_bytes = 0
        self._entries: dict[str, tuple[bytes, tuple[bytes, ...]]] = {}
        self._identity = uuid4().hex
        self._sequence = 0
        self._closed = False

    @property
    def question_id(self):
        return self._question_id

    @property
    def max_bytes(self):
        return self._max_bytes

    @property
    def used_bytes(self):
        return self._used_bytes

    def _ensure_open(self):
        if self._closed:
            raise ValueError("Result store is closed")

    def _new_reference(self):
        self._ensure_open()
        self._sequence += 1
        return f"R_{self._identity}_{self._sequence:03d}"

    def _lookup(self, result_ref):
        self._ensure_open()
        if not isinstance(result_ref, str) or result_ref not in self._entries:
            raise ValueError("Unknown, stale, or other-attempt result reference")
        return self._entries[result_ref]

    def _check_capacity(self, size):
        self._ensure_open()
        if size > self._max_bytes - self._used_bytes:
            raise ValueError("Result exceeds remaining per-question stored-payload budget; select fewer columns or aggregate in SQL")

    def _publish(self, result_ref, metadata, chunks):
        self._ensure_open()
        if not result_ref.startswith(f"R_{self._identity}_") or result_ref in self._entries:
            raise ValueError("Result scope changed before publication")
        size = len(metadata) + sum(map(len, chunks))
        self._check_capacity(size)
        self._entries[result_ref] = (metadata, chunks)
        self._used_bytes += size

    def describe(self, result_ref):
        metadata, chunks = self._lookup(result_ref)
        description = pickle.loads(metadata)
        description["stored_bytes"] = len(metadata) + sum(map(len, chunks))
        return description

    def iter_rows(self, result_ref):
        self._lookup(result_ref)

        def rows():
            index = 0
            while True:
                entry = self._lookup(result_ref)
                if index == len(entry[1]):
                    return
                chunk = pickle.loads(entry[1][index])
                del entry
                index += 1
                for row in chunk:
                    self._lookup(result_ref)  # Clear/close also invalidates active iterators.
                    yield row

        return rows()

    def clear(self):
        self._ensure_open()
        self._entries.clear()
        self._used_bytes = 0
        self._identity = uuid4().hex
        self._sequence = 0

    def close(self):
        if not self._closed:
            self.clear()
            self._closed = True

    def __enter__(self):
        self._ensure_open()
        return self

    def __exit__(self, *_):
        self.close()


def _validate_result_value(value: Any) -> None:
    kind = type(value)
    if value is None or kind in {str, bool, int, float, Decimal, date, datetime, time, timedelta, UUID, bytes}:
        return
    if kind in {list, tuple}:
        for item in value:
            _validate_result_value(item)
        return
    if kind is dict:
        for key, item in value.items():
            _validate_result_value(key)
            _validate_result_value(item)
        return
    raise ValueError(f"Unsupported SQL result value type: {kind.__name__}; cast it explicitly in SQL")


def _result_preview_value(value: Any) -> Any:
    if type(value) is Decimal:
        return {"type": "decimal", "value": str(value)}
    if type(value) is UUID:
        return {"type": "uuid", "value": str(value)}
    if type(value) is list:
        return [_result_preview_value(item) for item in value]
    if type(value) is tuple:
        return {"type": "tuple", "items": [_result_preview_value(item) for item in value]}
    if type(value) is dict:
        if all(type(key) is str for key in value):
            return {key: _result_preview_value(item) for key, item in value.items()}
        return {"type": "map", "entries": [[_result_preview_value(key), _result_preview_value(item)] for key, item in value.items()]}
    return _preview_value(value)


def _result_observation(result_ref, schema, row_count, preview, stored_bytes, row_limit, byte_limit):
    observation = {"result_ref": result_ref, "schema": schema, "row_count": row_count,
                   "is_preview": True, "preview_rows": preview, "preview_row_count": len(preview),
                   "preview_truncated": len(preview) < row_count, "stored_bytes": stored_bytes}
    while True:
        observation["preview_row_count"] = len(preview)
        observation["preview_truncated"] = len(preview) < row_count
        if not preview and row_count:
            observation["preview_omitted_reason"] = "disabled_by_host" if row_limit == 0 else "first_row_exceeds_byte_budget"
            if row_limit:
                observation["instruction"] = "Full result retained; select fewer columns or narrower expressions in SQL to inspect it."
        size = len(json.dumps(observation, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        if size <= byte_limit:
            return observation
        if not preview:
            raise ValueError("Result observation metadata exceeds preview byte budget; select fewer columns or shorter aliases")
        preview.pop()  # Whole leading rows survive; individual cell values never change.


def execute_sql(connection: Any, sql: str, *, store: ResultStore, allowed_tables: Any, purpose: str,
                timeout_seconds: float = 10, preview_rows: int = RESULT_PREVIEW_ROWS,
                preview_bytes: int = RESULT_PREVIEW_BYTES) -> dict[str, Any]:
    """Consume a restricted query completely, then atomically publish its reference.

    Row/byte preview limits are host-owned and can be lowered. This function
    creates no SQL result tables, final answers, check bindings or model actions.
    """
    if not isinstance(store, ResultStore):
        raise ValueError("store must be a question-local ResultStore")
    store._ensure_open()
    if type(sql) is not str or not sql.strip():
        raise ValueError("sql must be a nonempty string")
    if type(purpose) is not str or not purpose.strip():
        raise ValueError("purpose must be a nonempty string")
    if type(preview_rows) is not int or not 0 <= preview_rows <= RESULT_PREVIEW_ROWS:
        raise ValueError(f"preview_rows must be an integer from 0 to {RESULT_PREVIEW_ROWS}")
    if type(preview_bytes) is not int or not 1 <= preview_bytes <= RESULT_PREVIEW_BYTES:
        raise ValueError(f"preview_bytes must be an integer from 1 to {RESULT_PREVIEW_BYTES}")
    result_ref = store._new_reference()
    chunks, preview = [], []
    count = payload_bytes = 0
    with restricted_query(connection, sql, allowed_tables=allowed_tables, timeout_seconds=timeout_seconds) as cursor:
        schema = [{"position": i, "name": column[0], "type": str(column[1])} for i, column in enumerate(cursor.description, 1)]
        while batch := cursor.fetchmany(RESULT_CHUNK_ROWS):
            try:
                for row in batch:
                    for value in row:
                        _validate_result_value(value)
                    if len(preview) < preview_rows:
                        preview.append([_result_preview_value(value) for value in row])
                chunk = pickle.dumps(tuple(batch), protocol=5)
            except (TypeError, OverflowError, RecursionError, pickle.PickleError) as exc:
                raise ValueError(f"Cannot serialize SQL result: {exc}; cast unsupported values in SQL") from exc
            payload_bytes += len(chunk)
            store._check_capacity(payload_bytes)
            chunks.append(chunk)
            count += len(batch)
        try:
            metadata = pickle.dumps({"result_ref": result_ref, "question_id": store.question_id, "sql": sql,
                                     "purpose": purpose, "schema": schema, "row_count": count}, protocol=5)
        except (TypeError, OverflowError, pickle.PickleError) as exc:
            raise ValueError(f"Cannot serialize SQL result metadata: {exc}") from exc
        stored_bytes = payload_bytes + len(metadata)
        store._check_capacity(stored_bytes)
        observation = _result_observation(result_ref, schema, count, preview, stored_bytes, preview_rows, preview_bytes)
        pending_chunks = tuple(chunks)
    store._publish(result_ref, metadata, pending_chunks)
    return observation


def _preview_value(value: Any, cell_type: str | None = None) -> Any:
    """Encode exceptional source types explicitly, without business conversion."""
    if cell_type in {"f", "e"}:
        if not isinstance(value, str):
            raise ValueError(f"Unsupported Excel {cell_type!r} value type: {type(value).__name__}")
        return {"type": "formula" if cell_type == "f" else "error", "value": value}
    if isinstance(value, (datetime, date, time)):
        return {"type": type(value).__name__, "value": value.isoformat()}
    if isinstance(value, timedelta):
        return {"type": "timedelta", "days": value.days, "seconds": value.seconds, "microseconds": value.microseconds}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"type": "bytes", "encoding": "base64", "value": base64.b64encode(value).decode("ascii")}
    if isinstance(value, float) and not math.isfinite(value):
        return {"type": "float", "value": repr(value)}
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ValueError(f"Unsupported cell value type: {type(value).__name__}")


def _position_columns(width: int) -> list[dict[str, Any]]:
    return [{"column_id": f"c{position:03d}", "position": position} for position in range(1, width + 1)]


def _inspect_csv(path: Path, start_row: int, row_count: int) -> dict[str, Any]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        records = list(islice(csv.reader(stream, strict=True), start_row - 1, start_row + row_count))
    rows = [{"source_row": number, "values": values}
            for number, values in enumerate(records[:row_count], start_row)]
    return {"columns": _position_columns(max((len(row["values"]) for row in rows), default=0)),
            "rows": rows, "has_more": len(records) > row_count, "row_reference": "csv_record",
            "encoding": "utf-8-sig", "delimiter": ","}


def _inspect_excel(path: Path, sheet_name: str, start_row: int, row_count: int) -> dict[str, Any]:
    # Own the file handle even if openpyxl fails before returning a workbook.
    with path.open("rb") as stream:
        workbook = load_workbook(stream, read_only=True, data_only=False, keep_links=False)
        try:
            if sheet_name not in workbook.sheetnames:
                raise ValueError(f"Worksheet no longer exists: {sheet_name!r}")
            sheet = workbook[sheet_name]
            sheet.reset_dimensions()
            iterator = sheet.iter_rows(min_row=start_row)
            try:
                records = list(islice(iterator, row_count + 1))
            finally:
                close = getattr(iterator, "close", None)
                if close is not None:
                    close()
            width = max((len(cells) for cells in records[:row_count]), default=0)
            # Display structural empty cells within the observed window width;
            # never forward-fill a merged heading or infer a business value.
            rows = [{"source_row": number,
                     "values": [_preview_value(cell.value, cell.data_type) for cell in cells] + [None] * (width - len(cells))}
                    for number, cells in enumerate(records[:row_count], start_row)]
            return {"columns": _position_columns(width), "column_scope": "preview_window",
                    "rows": rows, "has_more": len(records) > row_count, "row_reference": "worksheet_row",
                    "epoch": workbook.epoch.date().isoformat(), "dimensions": "not_assumed"}
        finally:
            workbook.close()


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@contextmanager
def _sqlite_snapshot(path: Path):
    """Own one read-only connection and a metadata/data-consistent read snapshot."""
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as database:
        database.execute("BEGIN").close()
        yield database


def _sqlite_table_info(database: Any, table: str) -> dict[str, Any]:
    with closing(database.execute(
        "SELECT wr FROM pragma_table_list WHERE schema='main' AND name=? AND type IN ('table', 'virtual')", (table,)
    )) as cursor:
        definition = cursor.fetchone()
    if definition is None:
        raise ValueError(f"User table no longer exists: {table!r}")
    with closing(database.execute("SELECT name, type, pk FROM pragma_table_xinfo(?) WHERE hidden != 1 ORDER BY cid", (table,))) as cursor:
        schema = cursor.fetchall()
    columns = [{**position, "name": name, "declared_type": kind, "primary_key_position": pk}
               for position, (name, kind, pk) in zip(_position_columns(len(schema)), schema)]
    if definition[0]:
        keys = [name for name, _, pk in sorted(schema, key=lambda field: field[2]) if pk]
        if not keys:
            raise ValueError("WITHOUT ROWID table has no declared primary key")
        ordering = {"kind": "primary_key", "columns": keys}
        prefix = ""
    else:
        names = {name.casefold() for name, _, _ in schema}
        key = next((alias for alias in ("_rowid_", "rowid", "oid") if alias not in names), None)
        if key is None:
            raise ValueError("Table shadows all rowid aliases; stable row identity is unavailable")
        keys = [key]
        ordering = {"kind": "rowid", "columns": keys}
        prefix = f"src.{_quote_identifier(key)}, "
    order_by = ", ".join(f"src.{_quote_identifier(key)}" for key in keys)
    return {"columns": columns, "ordering": ordering,
            "key_positions": [next(i for i, column in enumerate(schema) if column[0] == name) for name in keys] if definition[0] else [],
            "select_sql": f"SELECT {prefix}src.* FROM {_quote_identifier(table)} AS src ORDER BY {order_by}"}


def _sqlite_record(info: dict[str, Any], record: tuple) -> tuple[tuple, dict[str, Any]]:
    if info["ordering"]["kind"] == "rowid":
        return record[1:], {info["ordering"]["columns"][0]: record[0]}
    return record, {name: record[index] for name, index in zip(info["ordering"]["columns"], info["key_positions"])}


def _inspect_sqlite(path: Path, table: str, start_row: int, row_count: int) -> dict[str, Any]:
    with _sqlite_snapshot(path) as database:
        info = _sqlite_table_info(database, table)
        with closing(database.execute(info["select_sql"] + " LIMIT ? OFFSET ?", (row_count + 1, start_row - 1))) as cursor:
            records = cursor.fetchall()
        rows = []
        for number, record in enumerate(records[:row_count], start_row):
            values, row_key = _sqlite_record(info, record)
            rows.append({"source_row": number, "row_key": {key: _preview_value(value) for key, value in row_key.items()},
                         "values": [_preview_value(value) for value in values]})
        return {"columns": info["columns"], "rows": rows, "has_more": len(records) > row_count,
                "row_reference": "ordered_sqlite_record", "ordering": info["ordering"]}


def _resolve_sources(business_dir: str | Path, inventory: dict[str, Any], source_ids: list[str]) -> list[tuple[dict[str, Any], Path]]:
    """Shared host-catalog validation for inspection and loaders; no row reads."""
    if not isinstance(source_ids, list) or not source_ids:
        raise ValueError("source_ids: expected a nonempty list")
    if any(not isinstance(source_id, str) or not source_id for source_id in source_ids) or len(set(source_ids)) != len(source_ids):
        raise ValueError("source_ids: expected unique nonempty strings")
    sources = {source["source_id"]: source for source in inventory["sources"]}
    for source_id in source_ids:
        if source_id not in sources:
            raise ValueError(f"{source_id}: unknown source ID in current business inventory")
    try:
        root = Path(business_dir).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Expected a business directory")
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"Cannot access business sources in {business_dir}: {exc}") from exc
    files = {file["file"]: file for file in inventory["files"]}
    selected = []
    for source_id in source_ids:
        source = sources[source_id]
        try:
            relative = Path(source["file"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Source path must be relative to business directory")
            path = (root / relative).resolve(strict=True)
            if not path.is_relative_to(root):
                raise ValueError("Source resolves outside business directory")
            file = files[source["file"]]
            if not path.is_file() or path.stat().st_size != file["size_bytes"]:
                raise ValueError("Source changed since inventory; rebuild the current business catalog")
            if source["format"] != file["format"] or _FORMATS.get(relative.suffix.lower()) != file["format"]:
                raise ValueError("Source format does not match inventory")
            selected.append((source, path))
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            raise ValueError(f"{source_id} ({source['file']}): {exc}") from exc
    return selected


def inspect_sources(business_dir: str | Path, inventory: dict[str, Any], source_ids: list[str], *,
                    start_row: int = 1, row_count: int = 12) -> dict[str, Any]:
    """Inspect a bounded batch from the trusted current-business catalog.

    No headers are inferred and no business tables are registered. Source paths,
    catalog sizes and sheet/table identities are rechecked; full content hashes
    are left to run manifests. Errors never return a partial batch.
    """
    if not isinstance(source_ids, list) or not 1 <= len(source_ids) <= INSPECT_MAX_SOURCES:
        raise ValueError(f"source_ids: supply 1–{INSPECT_MAX_SOURCES} unique source IDs")
    if type(start_row) is not int or start_row < 1:
        raise ValueError("start_row: expected a positive integer")
    if type(row_count) is not int or not 1 <= row_count <= INSPECT_MAX_ROWS:
        raise ValueError(f"row_count: expected an integer from 1 to {INSPECT_MAX_ROWS}")
    selected = _resolve_sources(business_dir, inventory, source_ids)
    previews = []
    for source, path in selected:
        try:
            if source["format"] == "csv":
                preview = _inspect_csv(path, start_row, row_count)
            elif source["format"] == "xlsx":
                preview = _inspect_excel(path, source["sheet"], start_row, row_count)
            else:
                preview = _inspect_sqlite(path, source["table"], start_row, row_count)
            previews.append({**source, "start_row": start_row, "requested_rows": row_count, **preview,
                             "next_start_row": start_row + len(preview["rows"]) if preview["has_more"] else None})
        except (OSError, RuntimeError, ValueError, OverflowError, KeyError, csv.Error, sqlite3.Error, zipfile.BadZipFile, ET.ParseError) as exc:
            raise ValueError(f"{source['source_id']} ({source['file']}): cannot inspect source: {exc}") from exc
    result = {"previews": previews}
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")) > INSPECT_MAX_BYTES:
        raise ValueError(f"Preview exceeds {INSPECT_MAX_BYTES} UTF-8 JSON bytes; request fewer rows or sources")
    return result


def _validate_layout(header_rows: list[int], first_data_row: int) -> None:
    if not isinstance(header_rows, list) or not header_rows or any(type(row) is not int or row < 1 for row in header_rows):
        raise ValueError("header_rows: expected a nonempty increasing list of positive integers")
    if header_rows != sorted(set(header_rows)):
        raise ValueError("header_rows: expected increasing unique record numbers")
    if type(first_data_row) is not int or first_data_row <= header_rows[-1]:
        raise ValueError("first_data_row: expected an integer after all selected headers")


def _next_raw_table(connection: Any) -> str:
    used = {row[0].casefold() for row in connection.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    number = 1
    while any(name in used for name in (f"raw_{number:03d}", f"_csv_chunk_raw_{number:03d}", f"_excel_chunk_raw_{number:03d}", f"_sqlite_chunk_raw_{number:03d}")):
        number += 1
    return f"raw_{number:03d}"


def _csv_records(stream: Any):
    reader = csv.reader(stream, strict=True)
    number = 1
    while True:
        try:
            values = next(reader)
        except StopIteration:
            return
        except (csv.Error, UnicodeError) as exc:
            raise ValueError(f"while reading logical record {number}: {exc}") from exc
        yield number, values
        number += 1


def _check_append(expected: dict[str, Any] | None, columns: list[dict[str, Any]], *,
                  epoch: str | None = None, ordering: dict[str, Any] | None = None) -> bool:
    """Check raw metadata exactly, including JSON scalar types; never align columns."""
    if expected is None:
        return False
    if len(columns) != len(expected["columns"]):
        raise ValueError(f"Incompatible column count: expected {len(expected['columns'])}, found {len(columns)}")
    for actual, original in zip(columns, expected["columns"]):
        if json.dumps(actual, ensure_ascii=False, sort_keys=True, allow_nan=False) != json.dumps(original, ensure_ascii=False, sort_keys=True, allow_nan=False):
            raise ValueError(f"Incompatible column {actual['column_id']}: expected {original!r}, found {actual!r}")
    for name, value in (("epoch", epoch), ("ordering", ordering)):
        if value != expected.get(name):
            raise ValueError(f"Incompatible {name}: expected {expected.get(name)!r}, found {value!r}")
    return True


def _insert_csv_chunk(connection: Any, table: str, column_ids: list[str], source_id: str, records: list[list[Any]]) -> None:
    view = f"_csv_chunk_{table}"
    frame = pd.DataFrame(records, columns=["__source_row", *column_ids], dtype=object)
    connection.register(view, frame)
    try:
        values = ", ".join(f"CAST({_quote_identifier(column)} AS VARCHAR)" for column in column_ids)
        connection.execute(f"INSERT INTO {_quote_identifier(table)} SELECT {values}, ?, "
                           f"CAST(__source_row AS BIGINT) FROM {_quote_identifier(view)}", [source_id])
    finally:
        # An aborted DuckDB transaction rejects unregister. The transaction
        # owner repeats cleanup after rollback to release the Python binding.
        with suppress(duckdb.Error):
            connection.unregister(view)


def _load_csv_into_table(connection: Any, path: Path, source: dict[str, Any], table: str,
                         header_rows: list[int], first_data_row: int, *, append_to: dict[str, Any] | None = None) -> dict[str, Any]:
    """Transaction-neutral ingestion for a validated source/layout.

    The transaction owner must rollback on failure and unregister
    _csv_chunk_<table> afterwards. T11 can reuse this inside a batch transaction.
    """
    with path.open(encoding="utf-8-sig", newline="") as stream:
        records = _csv_records(stream)
        headers = {number: values for number, values in islice(records, first_data_row - 1) if number in header_rows}
        missing = [number for number in header_rows if number not in headers]
        if missing:
            raise ValueError(f"Missing header records: {missing}")
        width = max(len(header) for header in headers.values())
        if not width:
            raise ValueError("Selected headers have zero columns")
        columns = [{**position, "header_labels": [headers[number][i] if i < len(headers[number]) else None for number in header_rows]}
                   for i, position in enumerate(_position_columns(width))]
        column_ids = [column["column_id"] for column in columns]
        schema = [{"name": column, "type": "VARCHAR"} for column in column_ids]
        schema += [{"name": "__source_id", "type": "VARCHAR"}, {"name": "__source_row", "type": "BIGINT"}]
        definition = ", ".join(f"{_quote_identifier(column)} VARCHAR" for column in column_ids)
        if not _check_append(append_to, columns):
            connection.execute(f"CREATE TABLE {_quote_identifier(table)} ({definition}, __source_id VARCHAR NOT NULL, __source_row BIGINT NOT NULL)")
        count = 0
        chunk = []
        for number, values in records:
            if values and len(values) != width:
                raise ValueError(f"logical record {number}: expected {width} fields, found {len(values)}")
            chunk.append([number, *(values if values else [None] * width)])
            count += 1
            if len(chunk) == CSV_CHUNK_ROWS:
                _insert_csv_chunk(connection, table, column_ids, source["source_id"], chunk)
                chunk = []
        if chunk:
            _insert_csv_chunk(connection, table, column_ids, source["source_id"], chunk)
    return {"table": table, "schema": schema, "row_count": count, "source": dict(source),
            "columns": columns, "layout": {"header_rows": list(header_rows), "first_data_row": first_data_row},
            "row_reference": "csv_record"}


def load_csv(connection: Any, business_dir: str | Path, inventory: dict[str, Any], source_id: str, *,
             header_rows: list[int], first_data_row: int) -> dict[str, Any]:
    """Atomically load one CSV on an idle caller-owned connection; leave it open.

    Use the transaction-neutral ingestion helper when a batch owner already
    holds a transaction. DuckDB does not support nested BEGIN statements.
    """
    _validate_layout(header_rows, first_data_row)
    source, path = _resolve_sources(business_dir, inventory, [source_id])[0]
    if source["format"] != "csv":
        raise ValueError(f"{source_id} ({source['file']}): expected a CSV source")
    table = None
    try:
        table = _next_raw_table(connection)
        connection.execute("BEGIN TRANSACTION")
        try:
            result = _load_csv_into_table(connection, path, source, table, header_rows, first_data_row)
            connection.execute("COMMIT")
            return result
        except Exception:
            connection.execute("ROLLBACK")
            raise
    except (OSError, RuntimeError, ValueError, OverflowError, duckdb.Error) as exc:
        raise ValueError(f"{source_id} ({source['file']}): cannot load CSV: {exc}") from exc
    finally:
        if table is not None:
            with suppress(duckdb.Error):
                connection.unregister(f"_csv_chunk_{table}")


def _excel_value(cell: Any) -> Any:
    """Canonical storage value: dates as ISO text, other special values tagged."""
    if isinstance(cell.value, (datetime, date, time)):
        return cell.value.isoformat()
    return _preview_value(cell.value, cell.data_type)


def _insert_excel_chunk(connection: Any, table: str, column_ids: list[str], source_id: str,
                        records: list[list[Any]]) -> None:
    view = f"_excel_chunk_{table}"
    frame = pd.DataFrame(records, columns=["__source_row", *column_ids], dtype=object)
    connection.register(view, frame)
    try:
        values = ", ".join(f"CAST(CAST({_quote_identifier(column)} AS JSON) AS VARIANT)" for column in column_ids)
        connection.execute(f"INSERT INTO {_quote_identifier(table)} SELECT {values}, ?, "
                           f"CAST(__source_row AS BIGINT) FROM {_quote_identifier(view)}", [source_id])
    finally:
        with suppress(duckdb.Error):
            connection.unregister(view)


def _load_excel_into_table(connection: Any, path: Path, source: dict[str, Any], table: str,
                           header_rows: list[int], first_data_row: int, *, append_to: dict[str, Any] | None = None) -> dict[str, Any]:
    """Transaction-neutral worksheet ingestion; owner cleans staging after rollback."""
    with path.open("rb") as stream:
        workbook = load_workbook(stream, read_only=True, data_only=False, keep_links=False)
        try:
            if source["sheet"] not in workbook.sheetnames:
                raise ValueError(f"Worksheet no longer exists: {source['sheet']!r}")
            sheet = workbook[source["sheet"]]
            sheet.reset_dimensions()
            iterator = sheet.iter_rows()
            try:
                records = enumerate(iterator, 1)
                headers = {number: cells for number, cells in islice(records, first_data_row - 1) if number in header_rows}
                missing = [number for number in header_rows if number not in headers]
                if missing:
                    raise ValueError(f"Missing header rows: {missing}")
                width = max(len(cells) for cells in headers.values())
                if not width:
                    raise ValueError("Selected headers have zero columns")
                columns = [{**position, "header_labels": [_preview_value(headers[number][i].value, headers[number][i].data_type)
                            if i < len(headers[number]) else None for number in header_rows]}
                           for i, position in enumerate(_position_columns(width))]
                column_ids = [column["column_id"] for column in columns]
                schema = [{"name": column, "type": "VARIANT"} for column in column_ids]
                schema += [{"name": "__source_id", "type": "VARCHAR"}, {"name": "__source_row", "type": "BIGINT"}]
                definition = ", ".join(f"{_quote_identifier(column)} VARIANT" for column in column_ids)
                if not _check_append(append_to, columns, epoch=workbook.epoch.date().isoformat()):
                    connection.execute(f"CREATE TABLE {_quote_identifier(table)} ({definition}, __source_id VARCHAR NOT NULL, __source_row BIGINT NOT NULL)")
                count, formula_count, error_count = 0, 0, 0
                chunk = []
                for number, cells in records:
                    outside = next((cell for cell in cells[width:] if cell.value is not None), None)
                    if outside is not None:
                        raise ValueError(f"{sheet.title}!{outside.coordinate}: nonempty cell beyond selected header width {width}")
                    values = []
                    for i in range(width):
                        if i >= len(cells):
                            values.append(None)
                            continue
                        cell = cells[i]
                        try:
                            value = _excel_value(cell)
                            values.append(json.dumps(value, ensure_ascii=False, allow_nan=False) if value is not None else None)
                        except (ValueError, TypeError, OverflowError) as exc:
                            raise ValueError(f"{sheet.title}!{getattr(cell, 'coordinate', f'column {i+1} row {number}')}: {exc}") from exc
                        formula_count += cell.data_type == "f"
                        error_count += cell.data_type == "e"
                    chunk.append([number, *values])
                    count += 1
                    if len(chunk) == EXCEL_CHUNK_ROWS:
                        _insert_excel_chunk(connection, table, column_ids, source["source_id"], chunk)
                        chunk = []
                if chunk:
                    _insert_excel_chunk(connection, table, column_ids, source["source_id"], chunk)
            finally:
                close = getattr(iterator, "close", None)
                if close is not None:
                    close()
            return {"table": table, "schema": schema, "row_count": count, "source": dict(source), "columns": columns,
                    "layout": {"header_rows": list(header_rows), "first_data_row": first_data_row},
                    "row_reference": "worksheet_row", "epoch": workbook.epoch.date().isoformat(),
                    "formula_cells": formula_count, "error_cells": error_count,
                    "sql_hint": "Use CAST(c001 AS VARCHAR) before text parsing and TRY_CAST(CAST(c001 AS VARCHAR) AS DOUBLE) for numeric parsing. Formula/error objects contain literal values, not calculated results."}
        finally:
            workbook.close()


def load_excel(connection: Any, business_dir: str | Path, inventory: dict[str, Any], source_id: str, *,
               header_rows: list[int], first_data_row: int) -> dict[str, Any]:
    """Atomically load a worksheet on an idle caller-owned connection; leave it open."""
    _validate_layout(header_rows, first_data_row)
    source, path = _resolve_sources(business_dir, inventory, [source_id])[0]
    if source["format"] != "xlsx":
        raise ValueError(f"{source_id} ({source['file']}): expected an Excel worksheet source")
    table = None
    try:
        table = _next_raw_table(connection)
        connection.execute("BEGIN TRANSACTION")
        try:
            result = _load_excel_into_table(connection, path, source, table, header_rows, first_data_row)
            connection.execute("COMMIT")
            return result
        except Exception:
            connection.execute("ROLLBACK")
            raise
    except (OSError, RuntimeError, ValueError, OverflowError, KeyError, duckdb.Error, zipfile.BadZipFile, ET.ParseError) as exc:
        raise ValueError(f"{source_id} ({source['file']}/{source['sheet']}): cannot load Excel: {exc}") from exc
    finally:
        if table is not None:
            with suppress(duckdb.Error):
                connection.unregister(f"_excel_chunk_{table}")


def _insert_sqlite_chunk(connection: Any, table: str, column_ids: list[str], source_id: str,
                         records: list[list[Any]]) -> None:
    view = f"_sqlite_chunk_{table}"
    frame = pd.DataFrame(records, columns=["__source_row", "__source_key", *column_ids], dtype=object)
    connection.register(view, frame)
    try:
        values = ", ".join(f"CAST(CAST({_quote_identifier(column)} AS JSON) AS VARIANT)" for column in column_ids)
        connection.execute(f"INSERT INTO {_quote_identifier(table)} SELECT {values}, ?, CAST(__source_row AS BIGINT), "
                           f"CAST(CAST(__source_key AS JSON) AS VARIANT) FROM {_quote_identifier(view)}", [source_id])
    finally:
        with suppress(duckdb.Error):
            connection.unregister(view)


def _load_sqlite_into_table(connection: Any, path: Path, source: dict[str, Any], table: str, *,
                            append_to: dict[str, Any] | None = None) -> dict[str, Any]:
    """Transaction-neutral ingestion; owner rolls back and cleans staging on failure."""
    with _sqlite_snapshot(path) as database:
        info = _sqlite_table_info(database, source["table"])
        column_ids = [column["column_id"] for column in info["columns"]]
        schema = [{"name": column, "type": "VARIANT"} for column in column_ids]
        schema += [{"name": "__source_id", "type": "VARCHAR"}, {"name": "__source_row", "type": "BIGINT"},
                   {"name": "__source_key", "type": "VARIANT"}]
        definition = ", ".join(f"{_quote_identifier(column)} VARIANT" for column in column_ids)
        if not _check_append(append_to, info["columns"], ordering=info["ordering"]):
            connection.execute(f"CREATE TABLE {_quote_identifier(table)} ({definition}, __source_id VARCHAR NOT NULL, "
                               "__source_row BIGINT NOT NULL, __source_key VARIANT NOT NULL)")
        count = 0
        with closing(database.execute(info["select_sql"])) as cursor:
            while True:
                records = cursor.fetchmany(SQLITE_CHUNK_ROWS)
                if not records:
                    break
                chunk = []
                for number, record in enumerate(records, count + 1):
                    values, row_key = _sqlite_record(info, record)
                    try:
                        key = json.dumps({name: _preview_value(value) for name, value in row_key.items()}, ensure_ascii=False, allow_nan=False)
                    except (ValueError, TypeError, OverflowError) as exc:
                        raise ValueError(f"ordered record {number}, source key: {exc}") from exc
                    encoded = []
                    for column, value in zip(info["columns"], values):
                        try:
                            value = _preview_value(value)
                            encoded.append(json.dumps(value, ensure_ascii=False, allow_nan=False) if value is not None else None)
                        except (ValueError, TypeError, OverflowError) as exc:
                            raise ValueError(f"ordered record {number}, column {column['name']!r}: {exc}") from exc
                    chunk.append([number, key, *encoded])
                _insert_sqlite_chunk(connection, table, column_ids, source["source_id"], chunk)
                count += len(records)
    return {"table": table, "schema": schema, "row_count": count, "source": dict(source), "columns": info["columns"],
            "row_reference": "ordered_sqlite_record", "ordering": info["ordering"],
            "sql_hint": "Use CAST(c001 AS VARCHAR) before text parsing and TRY_CAST(CAST(c001 AS VARCHAR) AS DOUBLE) for numeric parsing. BLOBs and exceptional values use explicit type markers. __source_row is an ordinal; __source_key identifies the row in its input snapshot."}


def load_sqlite(connection: Any, business_dir: str | Path, inventory: dict[str, Any], source_id: str) -> dict[str, Any]:
    """Copy one table read-only into an idle caller-owned DuckDB connection."""
    source, path = _resolve_sources(business_dir, inventory, [source_id])[0]
    if source["format"] != "sqlite":
        raise ValueError(f"{source_id} ({source['file']}): expected a SQLite table source")
    table = None
    try:
        table = _next_raw_table(connection)
        connection.execute("BEGIN TRANSACTION")
        try:
            result = _load_sqlite_into_table(connection, path, source, table)
            connection.execute("COMMIT")
            return result
        except Exception:
            connection.execute("ROLLBACK")
            raise
    except (OSError, RuntimeError, ValueError, OverflowError, KeyError, sqlite3.Error, duckdb.Error) as exc:
        raise ValueError(f"{source_id} ({source['file']}/{source['table']}): cannot load SQLite: {exc}") from exc
    finally:
        if table is not None:
            with suppress(duckdb.Error):
                connection.unregister(f"_sqlite_chunk_{table}")


def load_sources(connection: Any, business_dir: str | Path, inventory: dict[str, Any],
                 requests: list[dict[str, Any]]) -> dict[str, Any]:
    """Publish an entire explicitly grouped batch atomically on an idle connection.

    The returned source_catalog belongs to the host and this business/connection;
    no SQL catalog or cache is installed. Rows require explicit SQL ordering.
    """
    if not isinstance(requests, list) or not requests:
        raise ValueError("requests: expected a nonempty list")
    source_ids = []
    for number, request in enumerate(requests, 1):
        if not isinstance(request, dict) or not isinstance(request.get("source_ids"), list) or not request["source_ids"]:
            raise ValueError(f"request {number}: expected an object with nonempty source_ids")
        source_ids.extend(request["source_ids"])
    # Resolve the complete batch, including uniqueness/path checks, before reads.
    resolved = {source["source_id"]: (source, path) for source, path in _resolve_sources(business_dir, inventory, source_ids)}
    for number, request in enumerate(requests, 1):
        formats = {resolved[source_id][0]["format"] for source_id in request["source_ids"]}
        if len(formats) != 1:
            raise ValueError(f"request {number}: cannot combine different source formats: {request['source_ids']}")
        expected_fields = {"source_ids"} if formats == {"sqlite"} else {"source_ids", "header_rows", "first_data_row"}
        if set(request) != expected_fields:
            raise ValueError(f"request {number}: expected exactly fields {sorted(expected_fields)}")
        if formats != {"sqlite"}:
            _validate_layout(request["header_rows"], request["first_data_row"])
    tables, catalog, staging = [], [], []
    try:
        connection.execute("BEGIN TRANSACTION")
        try:
            for request in requests:
                table = _next_raw_table(connection)
                first = None
                members = []
                for source_id in request["source_ids"]:
                    source, path = resolved[source_id]
                    kind = source["format"]
                    staging.append(f"_{'excel' if kind == 'xlsx' else kind}_chunk_{table}")
                    try:
                        if kind == "sqlite":
                            metadata = _load_sqlite_into_table(connection, path, source, table, append_to=first)
                        else:
                            loader = _load_csv_into_table if kind == "csv" else _load_excel_into_table
                            metadata = loader(connection, path, source, table, request["header_rows"], request["first_data_row"], append_to=first)
                    except Exception as exc:
                        raise ValueError(f"{source_id} ({source['file']}, {source.get('sheet', source.get('table', kind))}): {exc}") from exc
                    first = metadata if first is None else first
                    members.append(metadata)
                    catalog.append(metadata)
                combined = {key: value for key, value in first.items() if key != "source"}
                combined["source_ids"] = list(request["source_ids"])
                combined["row_count"] = sum(member["row_count"] for member in members)
                for field in ("formula_cells", "error_cells"):
                    if field in combined:
                        combined[field] = sum(member[field] for member in members)
                tables.append(combined)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    except (OSError, RuntimeError, ValueError, duckdb.Error) as exc:
        raise ValueError(f"Cannot load source batch: {exc}") from exc
    finally:
        for view in set(staging):
            with suppress(duckdb.Error):
                connection.unregister(view)
    return {"tables": tables, "source_catalog": catalog}


def register_calendar(connection: Any, calendar_path: str | Path) -> dict[str, Any]:
    """Validate calendar CSV, then atomically install it on a caller-owned connection.

    This trusted loader accepts the released calendar schema only. It performs
    no business-data conversions and exposes no SQL execution tool to the model.
    """
    path = Path(calendar_path)
    line_number = 1
    try:
        raw = path.read_bytes()
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8"), newline=""), strict=True)
        columns = [name for name, _ in _CALENDAR_SCHEMA]
        if not reader.fieldnames or len(reader.fieldnames) != len(columns) or set(reader.fieldnames) != set(columns):
            raise ValueError(f"Expected calendar columns: {', '.join(columns)}")
        rows = []
        dates = set()
        for record in reader:
            line_number = reader.line_num
            if None in record or any(value is None for value in record.values()):
                raise ValueError("Calendar row has missing or extra values")
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", record["date"]):
                raise ValueError("date: expected YYYY-MM-DD")
            day = date.fromisoformat(record["date"])
            if day in dates:
                raise ValueError(f"date: duplicate {day.isoformat()}")
            numbers = []
            for field, lower, upper in (("weekday", 1, 7), ("lunar_day", 1, 30),
                                        ("lunar_month", 1, 12), ("lunar_year", 1, 9999)):
                if not re.fullmatch(r"[0-9]+", record[field]):
                    raise ValueError(f"{field}: expected an integer")
                value = int(record[field])
                if not lower <= value <= upper:
                    raise ValueError(f"{field}: expected {lower}–{upper}")
                numbers.append(value)
            if record["lunar_leap"] not in {"true", "false"}:
                raise ValueError("lunar_leap: expected true or false")
            rows.append((day, *numbers, record["lunar_leap"] == "true", record["holiday"]))
            dates.add(day)
        if not rows:
            raise ValueError("Calendar contains no rows")
    except (OSError, ValueError, csv.Error) as exc:
        raise ValueError(f"{path}:{line_number}: {exc}") from exc

    try:
        connection.execute("BEGIN TRANSACTION")
        try:
            definition = ", ".join(f'"{name}" {kind} NOT NULL' for name, kind in _CALENDAR_SCHEMA)
            connection.execute(f"CREATE TABLE calendar ({definition}, PRIMARY KEY (date))")
            connection.executemany("INSERT INTO calendar VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    except duckdb.Error as exc:
        raise ValueError(f"{path}: cannot register calendar: {exc}") from exc
    return {"table": "calendar", "schema": [{"name": name, "type": kind} for name, kind in _CALENDAR_SCHEMA],
            "row_count": len(rows), "date_min": min(dates).isoformat(), "date_max": max(dates).isoformat(),
            "source_sha256": hashlib.sha256(raw).hexdigest()}


def _worksheet_names(path: Path) -> list[str]:
    """Read workbook-level XML only; never open worksheet cell contents."""
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        if workbook.tag.rsplit("}", 1)[-1] != "workbook":
            raise ValueError("Invalid workbook metadata")
        relationship_types = None
        if "xl/_rels/workbook.xml.rels" in archive.namelist():
            relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            relationship_types = {item.attrib["Id"]: item.attrib["Type"].rsplit("/", 1)[-1] for item in relationships}
        names = []
        for sheet in workbook.findall("{*}sheets/{*}sheet"):
            if relationship_types is not None:
                relationship_id = next((value for key, value in sheet.attrib.items() if key.endswith("}id")), None)
                if relationship_id not in relationship_types:
                    raise ValueError("Worksheet relationship is missing from workbook metadata")
                if relationship_types[relationship_id] != "worksheet":
                    continue
            names.append(sheet.attrib["name"])
        return names


def build_inventory(business_dir: str | Path) -> dict[str, list[dict[str, Any]]]:
    """Return files and selectable sources without reading CSV/cell/business rows.

    Formats are csv, xlsx and sqlite. Paths are relative POSIX strings; source
    IDs are local to this business and stable for an unchanged inventory.
    Any discovery/metadata failure raises ValueError instead of a partial catalog.
    """
    try:
        root = Path(business_dir).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Expected a business directory")
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"Cannot inventory {business_dir}: {exc}") from exc

    def walk_error(error: OSError):
        raise error

    paths = []
    try:
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
            for name in sorted(dirs):
                child = Path(directory) / name
                if child.is_symlink():
                    raise ValueError(f"{child.relative_to(root).as_posix()}: directory symlinks are not supported")
            paths.extend(Path(directory) / name for name in files if Path(name).suffix.lower() in _FORMATS)
    except OSError as exc:
        location = Path(exc.filename).relative_to(root).as_posix() if exc.filename else "."
        raise ValueError(f"{location}: cannot discover files: {exc}") from exc

    inventory: dict[str, list[dict[str, Any]]] = {"files": [], "sources": []}
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise ValueError("File resolves outside business directory")
            if not resolved.is_file():
                raise ValueError("Expected a regular file")
            file_format = _FORMATS[path.suffix.lower()]
            inventory["files"].append({"file": relative, "format": file_format, "size_bytes": resolved.stat().st_size})
            selections: list[dict[str, str]]
            if file_format == "csv":
                selections = [{}]
            elif file_format == "xlsx":
                selections = [{"sheet": name} for name in _worksheet_names(resolved)]
            else:
                with closing(sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True)) as database:
                    with closing(database.execute(
                        "SELECT name FROM sqlite_schema WHERE type='table' "
                        "AND name NOT GLOB 'sqlite_*' ORDER BY name COLLATE BINARY"
                    )) as cursor:
                        selections = [{"table": row[0]} for row in cursor]
            for selection in selections:
                inventory["sources"].append({
                    "source_id": f"S{len(inventory['sources']) + 1:03d}",
                    "file": relative, "format": file_format, **selection,
                })
        except (OSError, RuntimeError, ValueError, KeyError, sqlite3.Error, zipfile.BadZipFile, ET.ParseError) as exc:
            raise ValueError(f"{relative}: cannot inventory source: {exc}") from exc
    return inventory
