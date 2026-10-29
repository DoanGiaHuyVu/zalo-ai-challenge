# DataMate: a small offline agent harness

Status: implementation in progress; T1–T11 complete, with T3 retaining memory-headroom concerns. Updated 2026-10-29. The model adapter, question validator, source inventory, initial context preparation, calendar registration, raw-row inspection, all three source-format loaders and atomic combined-source loading exist; restricted model SQL, answer evaluation and the agent loop remain planned. This preserves the approved single-agent, SQL-first design and makes its runtime, memory, tracing and evaluation responsibilities explicit.

## Goal and fixed scope

Answer Vietnamese questions using the released business files and the current split's rules. Return a typed answer, `unanswerable` when the released evidence cannot support the question, or `abstain` when the solver cannot establish a reliable answer within its limits. Optimize penalized accuracy, PA = (correct - wrong) / total, on unfamiliar businesses.

One local model, four exposed tools, three source modules. No UI, network calls during inference, vector database, agent framework, arbitrary model-written Python, subagents, fine-tuning pipeline, or handlers keyed to business/question IDs in v1. The model chooses actions; host code controls permissions, context, execution, deadlines and final output. That host code is the agent harness.

The inspection covered train's 30 businesses, 20 CSVs, 107 XLSX files, one SQLite database and 302 worksheets; public test's 14 businesses, 10 CSVs, 69 XLSX files, one SQLite database and 136 worksheets. Header positions, monthly splits, row meanings, date/money representations and unit conventions vary. No Excel formulas were found in those files. Answer accuracy has not been measured; initial Mac runtime/context measurements are recorded below.

## Delivery milestones

The submission phases are separate, following the organizer notice quoted by the user:

- **M1: Public result file.** After the end-to-end baseline and T22 development pilot, run the released public test and prepare a validated JSONL result for the portal's Submission section. Record the code/model/configuration version used. Final Docker/RTX 5090 qualification is not a prerequisite for preparing this first public result. Continue development improvements without using public question-answer labels for training.
- **M2: November 16 solution handoff.** Prepare the complete Docker solution for the Solution Submission form, including inference code, required training code/data, model weights, supporting assets and reproduction instructions. The current baseline uses pretrained weights without fine-tuning; document that workflow and satisfy the official training-code/data requirements rather than assuming an exemption. Prepare packaging ahead of the handoff; confirm the exact cutoff/timezone and invocation contract from the official solution guide.
- **Final execution readiness.** T24 checks the NVIDIA path; T26/T27 qualify and freeze the packaged artifact against organizer constraints. A matching RTX 5090 environment or organizer validation service is needed for target-hardware evidence. This remains distinct from the immediate public-JSONL milestone.

## Runtime: the inner loop

One run means one question through its final submission record. A model turn is one generation/action inside that run.

```text
question + as_of_date + current split + current business
                         |
host prepares rules, README, source inventory and calendar schema
                         |
rebuild bounded context -> model emits one JSON action
                         |
                 host validates action
                   /              \
             execute tool       return error
                   \              /
                  record observation
                         |
             inspect / load / compute / check
                         |
         validated finish -> typed submission record
         unresolved error or exhausted budget -> abstain
```

Before exposing tools to the model, the host reads the current rulebook and business README, validates the question and inventories files, worksheets and SQLite tables. It registers the supplied calendar as a queryable table and supplies its schema. Rulebook and README remain in context. Source documents and data cells cannot authorize commands or override the host's tool policy. The current question and rules determine business semantics; generic model knowledge is not evidence of absent business facts.

The first action includes a short task specification: metric, period, intended grain, unit, expected answer type and rule references. It accompanies the first tool action and consumes no additional planning turn. Subsequent actions include a short purpose. There is one JSON action per model turn.

| Tool | Inputs | Result |
|---|---|---|
| `inspect` | Source IDs, start row and row count | Raw previews, column positions and source metadata |
| `load` | Source IDs, explicit header rows and first data row | Registered table names, schemas, row counts and source mapping |
| `sql` | One SELECT/CTE, purpose, and candidate reference for checks | Question-local result reference, schema, row count and bounded preview |
| `finish` | Answer type, result projection and check reference, or decline reason | Host-validated submission record |

Batch inspect and load requests can cover all relevant monthly sources in one turn. The model selects Excel layouts after seeing raw previews. The loader validates matching header layouts before combining sources, rejects incompatible batches without publishing partially loaded tables, and returns a source-specific error for repair. Different layouts load separately.

Use positional identifiers such as `c001` and expose original header labels, including multiple header rows. Preserve source values, leading-zero IDs, duplicate rows and blanks. Do not globally infer dates/money, fill blanks or deduplicate. Represent actual Excel date objects as ISO text and retain workbook epoch metadata for serial dates. Each record retains a source ID and source-row reference; the source catalog maps these to file, worksheet or SQLite table. Business-specific conversion, grouping and deduplication happen in SQL under the released rules.

A usual five-turn run is inspect -> batch load -> answer SQL -> diagnostic SQL -> finish. Three turns remain for repairs. This is an example, not a fixed workflow that executes irrelevant tools.

Every computed answer requires an executed diagnostic query bound to that candidate result. Checks cover source/date coverage, parsing of required nonempty values, and aggregation/join grain. Return named checks, observed values/details and Boolean pass values. A failed, missing or stale check blocks finishing that candidate. The same model writes the answer and check queries; this is inspectable evidence, not proof of correct business interpretation.

The host stores complete results outside model context, labels previews as previews, and constructs answers directly from selected stored cells. `finish` supports scalar (one cell), column list (query row order), row list (selected column order), and table (selected column names and rows). SQL performs business rounding and ordering. The serializer enforces type/shape, rejects nonfinite values and invalid scalar nulls, and rejects references from another question. Percent values are percentage points, not fractions; dates use the challenge's ISO formats. Preserve original source names in string answers.

For `unanswerable`, cite a missing required source/field or demonstrated coverage gap. A period wholly outside released coverage differs from a supported period with zero qualifying records; partial coverage follows the current rulebook. Tool failures, uncertainty, malformed actions and exhausted budgets lead to repair or `abstain`, never automatically to `unanswerable`. Unstructured model prose is not a valid finish. There is no extra ninth generation after the turn limit.

## Harness state and memory

| State | Contents and lifetime | Access |
|---|---|---|
| Procedural instructions | Frozen host policy, action schemas and workflow guidance | Supplied by host; model cannot rewrite them |
| Released business facts | Current rulebook, README, calendar and original business data | Current split/business only; facts remain traceable to sources |
| Working state | Question, short task specification, action ledger, observations, query results and checks | Fresh for each question |
| Raw-data cache | Immutable loaded source tables and their explicit layouts | Reused within one business; released on business change |
| Episode records | Timestamped run events and outcomes | Developer diagnostics; not retrieved into later questions |

Source tables are facts/views of the released input, not previously generated answers. Derived results, candidate checks and conversation state never become another question's context. Previous predictions are not consolidated into business facts. Traces do not become an answer lookup table. General improvements are reviewed development changes to the frozen prompt, tools or model configuration.

Rebuild context from mandatory documents/question, compact action history, loaded schemas and recent observations. Older previews may leave context without deleting their host-side results. Tokenize before generation and reserve response capacity. Never silently truncate mandatory documents; an oversized prefix fails the model feasibility checkpoint.

## Tracing and the development feedback loop

Implement tracing inside `solver.py`, not as another service. Write a local JSONL event stream and a run manifest. The manifest records model artifact/revision/hash/license, runtime and decoding settings, prompt/tool-schema/source-code hashes, hardware/backend, question-set identity, current rules/document hashes, and hashes identifying source files actually read. Hash work counts toward the batch deadline. Record missing hardware measurements as unavailable rather than guessing.

Each event has a schema version, run/question/business IDs, sequence and parent IDs, event kind, timestamps/duration, outcome/error code, and relevant structured payload. Record context assembly/compaction, model token counts and action, tool arguments and returned observations, candidate/check references, finish rejection, final answer type and stop reason. A model call, tool call and finish attempt are distinguishable events. Record requested structured actions and brief purposes, not hidden reasoning. Submission JSONL remains separate from traces and evaluation labels.

The model adapter is injectable: the same question runner accepts the real local model or recorded/scripted actions. Replaying actions re-executes tools on the same source files and tests host behavior without invoking a model. It does not prove that fresh Metal/CUDA generations will match. Complete model runs must be evaluated separately.

```text
development questions -> frozen harness -> traces + predictions
                                              |
                         deterministic scorer + failure report
                                              |
                      diagnose -> edit one prompt/config/tool cause
                                              |
                       regression checks + comparable development run
                                              |
                  reviewed version -> held-out audit -> offline package
```

This outer loop runs during development. Inference does not edit its prompt, skills, model weights or configuration. Keep evaluation answers outside inference inputs and accessible tool paths. Compare runs using their manifests and the same selected development question IDs; report PA, answer coverage, correct/wrong/abstain/unanswerable counts, latency, token/turn usage, failure categories and measured memory. No LLM judge or online observability service is needed for v1. Correctness uses the challenge's type/value matching rules, including aliases, ordering and table semantics.

Promotion evidence consists of passing required regressions, comparable development results and the hardware/offline/reproducibility checks at their planned milestones. This clarification adds no new numeric PA promotion threshold and no automatic deployment system. Held-out businesses remain an audit set rather than an iterative prompt-tuning loop. Freeze before private evaluation.

## Implementation and limits

| Module | Responsibility |
|---|---|
| `solver.py` | CLI, local-model adapter, context/state lifecycle, loop, limits, typed output, manifest and trace events |
| `data_tools.py` | Inventory, previews, explicit loaders, source catalog, restricted DuckDB and stored query results |
| `evaluate.py` | Frozen split/pilot, deterministic scoring, comparable-run reports and error summaries |

Use Python 3.11, pandas, openpyxl, sqlite3, DuckDB, llama-cpp-python, standard-library unittest, JSONL and Docker. Initial model candidate: Qwen3-8B Q4_K_M GGUF, Metal locally and CUDA later. Its memory, speed and accuracy remain feasibility gates, not promises. No framework or additional agent is introduced without measured development evidence and a separate scope decision.

Keep eight turns/question including finish and repairs; 1,024 generated tokens/turn; 16,384-token context with response reserve; ten-second SQL timeout; DuckDB 1 GB/two threads; local question deadline 300 seconds, NVIDIA deadline 60 seconds. Batch cutoff is 5 hours 45 minutes including loading, with remaining questions emitted as abstentions. These are provisional measured-pilot defaults. Cancellation applies to generation, tool execution and batch processing, with cleanup on exit.

Parse exactly one SELECT/CTE. Disable external access, extension loading and configuration changes. Loaders constrain resolved paths to the current business and supplied calendar. Read SQLite in read-only mode and copy selected tables through trusted host code. Model SQL cannot directly read files/network resources. Treat malformed actions and tool errors as observations for bounded repair.

Meet the organizer's offline model eligibility/licensing/aggregate-parameter rules, required CUDA/GPU compatibility, read-only `/data`, `predict.sh`, `/result/submission.jsonl` and diagnostic logs. Include source, dependencies, pinned weights and README in the package. Kaggle validates the NVIDIA execution path; it does not by itself validate the organizer's RTX 5090/Docker environment. Obtain and follow the organizer's exact interface/build guidance before finalizing the image. Final organizer rerun, six-hour ceiling and PA reproducibility tolerance remain authoritative.

## Small implementation checkpoints

Sequential implementation, no parallelization opportunity: the three modules share the tool/action/result contracts. Checkpoints are individually verifiable; do not implement them as a single opaque task.

| Task | Build | Completion evidence |
|---|---|---|
| T1 (done) | Pin dependencies/configuration | Locked imports, configuration and Metal build verified |
| T2 (done) | Load local model through Metal | Pinned weights verified; 37/37 layers offloaded; expected JSON returned |
| T3 (done with concerns) | Measure document tokens and model memory | All 4,000 contexts fit with reservation; eight GPU probes passed; system-wide swap growth needs follow-up |
| T4 (done) | Validate question inputs | 4,000 released questions passed; invalid records report file/line errors without loading a model |
| T5 (done) | Inventory files/sheets/tables | 44 businesses verified: 208 files and 502 selectable sources; original T3 inventory hashes match |
| T6 (done) | Build initial document/calendar context | All 4,000 contexts verified; calendar registered; token audit passed with 1,660 reserved tokens remaining at worst |
| T7 (done) | Preview raw rows | All 502 sources passed raw previews; titles/blanks/headers preserved; maximum response 6,572 bytes |
| T8 (done) | Load CSV with explicit layout | All 30 CSVs round-trip exactly: 1,095,465 rows and 11,676,468 business cells; source references preserved |
| T9 (done) | Load Excel with explicit layout | All 438 worksheets passed exact canonical value/type/reference checks: 3,657,520 rows and 36,452,425 cells |
| T10 (done) | Load SQLite read-only | All 34 tables passed: 571,711 rows and 5,922,645 cells; ordinals/keys preserved and database hashes unchanged |
| T11 (done) | Batch load and track provenance | 22 groups / 299 worksheets passed exact value/type/provenance checks; batch rollback and catalog mappings verified |
| T12 | Restrict DuckDB | SELECT allowed; writes/external reads denied |
| T13 | Store full results and show previews | Preview bounds never change the answer |
| T14 | Validate four action schemas | Unknown/malformed actions rejected |
| T15 | Run loop with fake/recorded model | Success, repair, limits and replay verified |
| T16 | Connect real model | One training question completes through tools |
| T17 | Enforce candidate-bound diagnostics | Failed/missing/stale checks block finish |
| T18 | Serialize typed projections | All supported answer shapes validated |
| T19 | Separate decline paths | Unsupported evidence differs from execution failure |
| T20a | Record immutable run manifest | Prediction bound to prompt/model/tool/data versions |
| T20b | Record model/tool/finish events | Trace reconstructs action order, errors and stop reason |
| T20c | Enforce deadlines/cache cleanup | No cross-question derived state or unbounded execution |
| T21a | Implement scorer and split | Gold PA=1, all-abstain PA=0 |
| T21b | Report comparable-run diagnostics | PA/coverage/latency changes and failure categories visible |
| T22 | Run 32-question development pilot | Accuracy, runtime and memory measured |
| M1 | Prepare first public-test JSONL | Baseline output validated and version recorded for portal Submission |
| T23 | Fix largest measured development failure | Focused regression and comparable rerun |
| T24 | Run unchanged solver offline on Kaggle | No network/dependency downloads required |
| T25 | Audit untouched validation businesses | PA/coverage/latency by type recorded |
| T26 | Prepare complete organizer-compatible Docker for November 16 handoff | Required code/data/assets included; read-only mounts and outputs verified |
| T27 | Repeat, freeze and export for Solution Submission | Versioned artifact, offline/reproducibility evidence, required form handoff |

T20 and T21 were split into smaller checkpoints to make their already approved observability and evaluation work concrete. Other task scope is preserved.

## Verification and acceptance

The implemented host/runtime code has 149 passing unittest cases: nine T2, eleven T3, sixteen T4, eleven T5, sixteen T6, eighteen T7, seventeen T8, sixteen T9, sixteen T10 and nineteen T11 cases. Run them with `.venv/bin/python -m unittest discover -s tests -v`. The T2 bounded smoke command returned expected JSON through Metal using the configured 16,384-token context. Native GPU access was required; the initial sandbox command-queue failure is retained under `artifacts/`. Additional coverage remains planned for file-tool/SQL restrictions, cache/question isolation, actions, loop termination, replay, traces, candidate-bound checks, typed answers and scoring. Run real-model evals separately from deterministic host tests. There is no mobile application in this task.

T4 adds `load_questions(questions_path, data_root)` in `solver.py` and reuses it in the feasibility scan. It preserves the four question fields and their original order/text, requires strict calendar dates and unique IDs, rejects unexpected fields (including answer labels), and confines business directories and README symlinks to the selected data root. Invalid JSON, duplicate keys, blank records, missing files and bad fields fail at the first error with a filename and one-based line number. Input errors remain separate from answerability or solver abstention. Validation reads question records and checks business/README paths; it does not read business data rows or load a model/configuration.

Both validation commands passed at T4 completion:

```sh
.venv/bin/python solver.py --validate-questions train/questions.jsonl --data-root train/data
# {"status": "passed", "question_count": 3000, "business_count": 30}
.venv/bin/python solver.py --validate-questions public_test/questions.jsonl --data-root public_test/data
# {"status": "passed", "question_count": 1000, "business_count": 14}
```

The validation CLI exits 0 with the JSON summary or 1 with an input error on stderr; incorrect CLI arguments exit 2. `--validate-questions` and `--data-root` must be supplied together and cannot be combined with other modes. Tests cover the actual subprocess CLI, execution without a configuration file, preserved Vietnamese text/leading-zero IDs, future and leap-day dates, malformed inputs, directory traversal and escaping symlinks. T4 required no GPU rerun, downloads, dependencies or model-setting changes.

T5 adds `build_inventory(business_dir)` in `data_tools.py`. Its JSON-compatible catalog contains `files` (relative POSIX `file`, `format`, `size_bytes`) and `sources` (`source_id`, `file`, `format`, plus `sheet` or `table` where applicable). Formats are `csv`, `xlsx` and `sqlite`. Each CSV, worksheet or user table receives a business-local ID starting at `S001`; ordering follows sorted relative filenames, workbook worksheet order and sorted table names. IDs are reproducible for an unchanged inventory. Files with no selectable sources remain listed.

The discovery includes ordinary nested directories and hidden worksheets, rejects directory symlinks and supported file links escaping the business root, and excludes unsupported files, SQLite internal tables and views. Excel reads only workbook-level ZIP XML; SQLite uses `mode=ro` and schema queries. CSV contents, worksheet cells and business table rows remain unread. Corrupt/unreadable supported files produce filename-specific errors instead of a partial catalog. Tests verify these boundaries, resource cleanup on success/failure, and CLI execution without configuration/model initialization.

Inspect one business with `.venv/bin/python solver.py --inventory train/data/biz_101`. This mutually exclusive mode prints the complete catalog and exits 0, or exits 1 with an inventory error on stderr; invalid CLI combinations exit 2. The complete T5 scan and catalogs are saved locally in `artifacts/t5-inventory.json`:

| Split | Businesses | Structured files | Worksheets | SQLite tables | Selectable sources |
|---|---:|---:|---:|---:|---:|
| Train | 30 | 128 | 302 | 17 | 339 |
| Public test | 14 | 80 | 136 | 17 | 163 |

Every catalog source was checked against its original file and worksheet/table. The feasibility scanner now adapts this shared inventory to its original T3 prompt shape; all 44 inventory hashes match the saved T3 report, whose bytes remain unchanged. No GPU diagnostic was rerun. Previews, row loading and row-level provenance remain T7–T11.

T6 adds `prepare_business_context(split_root, business_id, connection)` and `build_initial_messages(prepared, question)` in `solver.py`. Preparation selects the explicitly requested split's rulebook, business README and source catalog, registers that split's calendar on a caller-owned fresh connection, and records input hashes. Messages contain a fixed host-policy system message and a JSON `reference_data` payload. Full UTF-8 documents, including original line endings, remain intact. Question-field and business-path checks reuse T4 helpers; documents escaping the selected split fail. Each question gets fresh messages, its own `as_of_date`, and a matching business; document contents cannot replace the host-policy message. These structural tests do not establish model resistance to prompt injection or answer accuracy.

`register_calendar(connection, calendar_path)` in `data_tools.py` validates the expected columns, real ISO dates, integer fields/ranges, lowercase Boolean strings and unique dates, then creates `calendar` through parameterized inserts in a transaction. Its schema is `date DATE`, `weekday/lunar_day/lunar_month/lunar_year INTEGER`, `lunar_leap BOOLEAN`, and `holiday VARCHAR`. It preserves holiday text and empty strings, derives coverage from the selected file, rejects malformed values without publishing a partial table, and leaves connection ownership with the caller. The model receives schema/count/coverage instead of calendar rows. Calendar coverage does not automatically constrain valid questions or establish business-data coverage.

Review the actual initial messages without model loading:

```sh
.venv/bin/python solver.py --context q_001883280e83 --split-root train
```

The CLI validates the split's questions, selects the requested ID, closes its temporary in-memory connection on success/failure, and emits JSON messages. It exits 0 on success, 1 for input/context errors, or 2 for invalid argument combinations. `--context` and `--split-root` must be supplied together and cannot be combined with other modes. Real CLI checks passed for one training and one public-test question while pointing `--config` at a nonexistent file, confirming context preparation needs no model configuration.

The T6 token-only audit passed for all 4,000 questions across 44 businesses in 85.8 seconds under a 300-second subprocess timeout. Every prepared context was checked against its selected split/business documents, and every connection contained the selected calendar's 1,461 rows covering 2024-01-01 through 2027-12-31. Maximum initial prompt sizes were 10,628 tokens for train and 9,398 for public test. At the overall maximum, the configured 16,384-token context retains 4,732 tokens after reserving 1,024 response tokens, or 1,660 after also reserving 3,072 working tokens. No mandatory or working-reserve overflow occurred. Documents and model settings were unchanged.

The report `artifacts/t6-context.json` contains per-business maxima, input/code/configuration hashes, all overflow cases (none), and selection-verification counts. `audit_contexts(split_roots, config)` loads the verified artifact's vocabulary once on CPU and uses the existing non-thinking formatter/token counter; it generates no answers. The native log is `artifacts/t6-tokenizer.log`; it reports `n_ctx_train(0)` under vocabulary-only loading. T6 classification compares exact counts against the configured context budget; T2/T3 retain the full-model loading evidence. Existing T3 artifacts were verified unchanged. T6 does not requalify full-model memory or accuracy. Recheck actual prompt fit after T14 adds action schemas.

T7 adds `inspect_sources(business_dir, inventory, source_ids, *, start_row=1, row_count=12)` in `data_tools.py`. It accepts the trusted current-business inventory and returns previews in requested source order. Each preview includes its original file/sheet/table, positional columns (`c001`, etc.), source-row references, pagination and format metadata. Arguments are validated before readers run. Selected paths, catalog file sizes and sheet/table identities are checked again; these metadata checks are not full content fingerprints. Full data fingerprints remain part of later run manifests.

Inspection bounds are 1–8 unique source IDs, 1–50 rows per source, and 16 KiB for the complete response serialized with `json.dumps(..., ensure_ascii=False, allow_nan=False)`. Oversized previews fail with an instruction to request fewer rows or sources; values and requested sources are never silently truncated. The CLI uses the same serialization. The later loop must also count actual model-context tokens; a byte limit alone does not establish context fit.

CSV uses strict UTF-8 with optional BOM and comma delimiters. It preserves strings, leading zeros, whitespace, empty fields, blank logical records and quoted newlines. Excel uses read-only openpyxl with formulas preserved and external-link retention disabled. All 438 released worksheets lack dimensions; the reader resets dimensions and streams through the requested window plus one lookahead row. It preserves physical row positions and displays structural blank cells as nulls across the observed preview width, without forward-filling merged headings or business values. The observed column width is local to that window, not a claim about the entire source. Workbook epoch metadata is included.

SQLite opens with `mode=ro`, reports declared column metadata and uses a bounded SELECT ordered by an accessible rowid. `WITHOUT ROWID` tables use declared primary-key columns. Previews include the actual row key and ordering; their ordinal `source_row` is only the position in that ordering. Identifiers are quoted and row limits/offsets are bound parameters. Dates/times use explicit ISO/type markers, numeric serials remain numeric, BLOBs use base64/type markers, and formulas/errors remain labeled source values. No business tables are registered or header layouts inferred. Files, iterators, workbooks, cursors and connections close on success and failure.

Inspect raw rows without model configuration or loading:

```sh
.venv/bin/python solver.py --inspect train/data/biz_101 --source-ids S001 --start-row 1 --row-count 12
```

This mutually exclusive mode exits 0 with JSON previews, 1 for inspection errors, or 2 for invalid CLI arguments. Inspection flags are valid only with `--inspect`. Tests cover raw CSV values and multiline records, missing/incorrect Excel dimensions, two-row headers, merged/interior blanks, typed Excel values and formulas, SQLite rowid gaps/composite keys/quoted identifiers/generated columns, pagination beyond EOF, source validation, batch ordering, UTF-8 response limits, and resource cleanup. The real training CSV CLI also passed with a nonexistent `--config` path.

The complete T7 compatibility scan passed in 399.4 seconds with zero errors: 339 train sources (20 CSVs, 302 worksheets, 17 SQLite tables) and 163 public-test sources (10 CSVs, 136 worksheets, 17 SQLite tables). Each source returned up to its first 12 rows; identities, consecutive row references, pagination, positional columns and strict JSON serialization were verified. The baby-shop two-header-row layout was explicitly checked. The maximum response was 6,572 bytes, below the 16,384-byte limit. This is compatibility-check runtime, not an inference-throughput measurement.

`artifacts/t7-inspection.json` records per-source results, sizes, preview hashes, durations, representative previews, code/configuration hashes and preserved T3/T6 evidence hashes. Business-file sizes and modification times were unchanged. Model settings and previous evidence were unchanged; no model calls or business answers were generated. Excel/SQLite loading remains T9/T10, combined-source provenance T11, and action schemas T14.

T8 adds `load_csv(connection, business_dir, inventory, source_id, *, header_rows, first_data_row)` in `data_tools.py`. It reuses inspection's source-path/catalog validation through `_resolve_sources`. Layout selection is mandatory: header record numbers must be positive, increasing and unique, with the first data record after them. Missing headers or a zero-column layout fail. Positional columns retain separate original header labels, including duplicates, empty labels and missing positions; no business schema is invented.

The loader creates business columns as `VARCHAR`, plus `__source_id VARCHAR` and `__source_row BIGINT`. CSV logical record numbers include headers, blanks and multiline fields, matching T7. It preserves leading zeros, whitespace, money/date strings, `NA`/`NULL` literals, explicit empty strings, duplicate records and all records after the selected data start. Fully blank records become rows with null business cells; nonblank records of the wrong width fail with source/record details. Footers, repeated headers and totals are not removed automatically.

CSV records stream in chunks of 4,096 through temporary pandas registrations and trusted `INSERT … SELECT` statements with explicit types. Data is not loaded into one whole-file DataFrame or passed through CSV type inference. A deterministic unused table name (`raw_001`, etc.) avoids overwriting existing tables or staging views. The public loader owns a transaction on an idle caller connection and leaves that connection open. Late parsing, width and insertion failures roll back the new table; cleanup after rollback removes the staging registration even when DuckDB's transaction was aborted. `_load_csv_into_table` keeps ingestion separate from transaction ownership so T11 can use one outer batch transaction and perform rollback/cleanup itself. The public loader does not support nested transactions.

Run one model-free loading check:

```sh
.venv/bin/python solver.py --load-csv train/data/biz_101 --source-id S001 --header-rows 1 --first-data-row 2
```

The CLI requires layout flags, prints table/schema/count/source/layout metadata, and closes its temporary in-memory connection; its printed table is not persisted after exit. It uses a 1 GB/two-thread DuckDB connection with external access disabled. Loading flags are accepted only with this mode. Exit codes are 0 for success, 1 for loading errors, and 2 for invalid CLI arguments. The real `biz_101` command loaded 56,501 rows successfully while `--config` pointed at a nonexistent file, confirming that no model configuration or initialization is required.

All 30 released CSVs were loaded individually using header record 1 and data start 2. Independent strict CSV iteration was compared against bounded DuckDB fetches, ordered by source record. Every business cell, source ID and logical record number matched. Schema/header metadata, row counts, staging cleanup and source-file content hashes were also checked:

| Split | CSVs | Data rows verified | Business cells verified |
|---|---:|---:|---:|
| Train | 20 | 851,699 | 7,008,891 |
| Public test | 10 | 243,766 | 4,667,577 |
| Total | 30 | 1,095,465 | 11,676,468 |

The combined loading/comparison check passed in 12.0 seconds. This measures CSV compatibility and integrity, not inference throughput or answer accuracy. `artifacts/t8-csv-loading.json` records each source, loaded metadata, row/cell counts, durations, input/code/settings hashes and preserved earlier-evidence hashes. Dependencies, model settings, prior evidence and CSV content hashes were unchanged; no business answers were generated. Tests also cover synthetic irregular layouts, string preservation, blank/duplicate/multiline records, failure after an inserted chunk, aborted-transaction cleanup, repeated loads, caller connection ownership, and model-free CLI behavior.

T9 adds `load_excel(connection, business_dir, inventory, source_id, *, header_rows, first_data_row)` in `data_tools.py`. The source ID selects a worksheet, and the caller supplies its explicit physical-row layout. Shared helpers validate sources/layouts and allocate unused raw-table names. Separate header labels, blank positions, workbook epoch, row counts and original source identity are returned with the schema. `__source_id` and `__source_row` retain business-local source IDs and physical worksheet row numbers.

Excel business columns use the pinned DuckDB runtime's `VARIANT` type to retain mixed native numbers, strings and Booleans. Date/time objects become ISO text; unformatted numeric serial dates stay numeric. Formula/error cells retain tagged literal values, never cached or calculated results. Other supported exceptional values use T7's explicit encodings. Load metadata includes the SQL hint `CAST(c001 AS VARCHAR)` for text parsing and `TRY_CAST(CAST(c001 AS VARCHAR) AS DOUBLE)` for numeric parsing. CSV columns retain their existing `VARCHAR` representation.

The worksheet reader owns its file handle, uses read-only openpyxl with `data_only=False` and `keep_links=False`, resets dimensions, and streams physical rows through EOF. It preserves sparse/blank/duplicate/continuation rows and pads absent cells within header-defined width with nulls. Nonempty cells outside that width fail with worksheet/cell coordinates. Chunks contain at most 4,096 records; temporary pandas registrations feed explicit JSON-to-VARIANT casts, preserving true SQL nulls and numeric-looking strings. The insertion path passes with external access and extension installation/autoloading disabled. Atomic rollback and post-rollback staging cleanup follow T8's pattern, while a transaction-neutral internal helper supports T11. Files, workbooks and iterators close on all exit paths.

The model-free CLI shares source/layout flags with CSV loading:

```sh
.venv/bin/python solver.py --load-excel train/data/biz_102 --source-id S001 --header-rows 1 --first-data-row 2
```

It prints metadata and closes its temporary connection. The real command loaded 77 conversion-table rows successfully even with a nonexistent `--config` path. Tests cover row-4/two-row headers, missing/incorrect dimensions, mixed types and SQL casts, both epochs, formula/error literals, sparse rows, rollback after an inserted chunk, cleanup, path/source validation, existing tables/views, transaction ownership and CLI behavior. No model initialization, inference or training is performed.

`artifacts/t9-layouts.json` contains 438 reviewed verification-only layouts. Its 116 grouped raw-header previews were reviewed, including 32 two-row-header worksheets across the handwritten-sales and baby-shop workbook groups. Runtime inference never reads this manifest or applies business-ID-specific layout handlers. `artifacts/t9-verify.py` performs independent streaming cell/type/row-reference comparison and persists checkpoints to `artifacts/t9-excel-loading-progress.json`. Run it with `.venv/bin/python artifacts/t9-verify.py` to repeat the offline integrity check.

The complete check passed all 438 worksheets across 176 workbooks with zero errors. Every loaded canonical value, its Python value type, and its physical source-row reference matched an independent openpyxl stream using bounded DuckDB fetches. Header labels, schemas, epochs, row counts and staging cleanup were checked too:

| Split | Workbooks | Worksheets | Data rows verified | Business cells verified |
|---|---:|---:|---:|---:|
| Train | 107 | 302 | 2,876,432 | 24,642,025 |
| Public test | 69 | 136 | 781,088 | 11,810,400 |
| Total | 176 | 438 | 3,657,520 | 36,452,425 |

The exhaustive loading/comparison run took 1,634.8 seconds (27.2 minutes). This includes separate source parsing for loading and verification and is not an inference-throughput or model-memory qualification. No formulas were found; 30 existing Excel error cells were preserved as tagged literals rather than replaced or calculated. Those are source values, separate from the zero verification errors. `artifacts/t9-excel-loading.json` contains per-source results, metadata, input/code/settings hashes and preserved earlier-evidence hashes; its source-cell totals are derived from the recorded per-source metadata. All workbook content hashes, earlier evidence, dependency files and model settings were unchanged. This was data loading/verification only, with no model training or question-answer inference.

T10 adds `load_sqlite(connection, business_dir, inventory, source_id)` in `data_tools.py`. The source ID selects a database/table through the trusted catalog. SQLite requires no header layout. Original column names, declared types, primary-key positions and ordering are returned as metadata; actual storage values are preserved in positional DuckDB `VARIANT` columns rather than coerced from SQLite declarations. BLOBs use reversible base64/type markers and exceptional values reuse T7's explicit encodings. Text and numeric SQL-casting hints match Excel's approach.

SQLite inspection and loading now share schema, ordering and record/key helpers. Each source connection opens with URI `mode=ro` and starts a read transaction before metadata/data access. Queries consist of fixed metadata reads and a safely quoted SELECT; no source DDL/DML, attached databases, enabled extensions or DuckDB SQLite extension are used. Rows stream through one cursor in batches of at most 4,096. An accessible rowid determines ordering for ordinary tables; `WITHOUT ROWID` tables use declared primary-key columns. Sources without reliable ordering fail clearly.

Copied records retain `__source_id VARCHAR`, `__source_row BIGINT` and `__source_key VARIANT`. The ordinal is the position in the selected ordering, not a physical SQLite row number. The actual rowid or composite key is preserved in `__source_key` and identifies a record within its input snapshot. Public loading owns a transaction on an idle caller DuckDB connection and leaves that connection open. Late source reads, conversion errors and aborted inserts roll back the new table, remove staging registrations and preserve existing tables. Transaction-neutral ingestion is available for T11, and source connections/cursors always close.

Use the model-free CLI without layout flags:

```sh
.venv/bin/python solver.py --load-sqlite train/data/biz_123 --source-id S001
```

It prints metadata and closes its temporary DuckDB connection. A real run copied the one-row `chi_nhanh` table successfully with a nonexistent `--config` path. Layout flags are rejected in SQLite mode; exit codes remain 0/1/2. Tests cover mixed storage classes, signed integers, nulls, duplicates, BLOBs, rowid gaps/shadowing, composite keys, generated columns, quoted identifiers, actual read-only write rejection, source hashes, a concurrent WAL writer, late read/conversion/insertion failures, staging cleanup, transaction ownership, repeated loads and CLI behavior. A writer committing during a load does not change the loader's active read snapshot. All earlier CSV/Excel/inspection tests still pass.

The full released-data check copied every table and compared every canonical value/type, source ID, ordinal and actual key against an independent read-only SQLite reader. All 17 copied tables coexisted in one target connection per business, with no staging views left behind:

| Split | Database | Tables | Rows verified | Business cells verified |
|---|---|---:|---:|---:|
| Train | `biz_123/pos.sqlite` | 17 | 268,911 | 2,785,744 |
| Public test | `dev_006/pos.sqlite` | 17 | 302,800 | 3,136,901 |
| Total | | 34 | 571,711 | 5,922,645 |

The check passed in 55.4 seconds; it measures loading integrity, not model inference or answer accuracy. `artifacts/t10-sqlite-loading.json` stores metadata, value-type counts, source keys/ranges, timings, database hashes and preserved code/settings/evidence hashes. Repeat it with `.venv/bin/python artifacts/t10-verify.py`. Both database content hashes, previous evidence, dependencies and model settings were unchanged. No model training or question-answer inference was performed. T11 extends these loaders with combined-source loading and catalog integration; model-facing restricted SQL remains T12.

T11 adds `load_sources(connection, business_dir, inventory, requests)`. Each request creates one table; its explicit `source_ids` select the sources to combine, and CSV/Excel requests also supply `header_rows` and `first_data_row`. SQLite requests accept only `source_ids`. Nonempty request shapes, all IDs, uniqueness across requests, layouts, formats and source paths are validated before ingestion. Each group must use one format. The internal loaders now support appending directly to the first source's table after checking compatibility; rows still stream in chunks of at most 4,096 with no intermediate whole-source copies.

CSV/Excel compatibility requires equal positional columns and exact header-label values and types, preserving duplicate, empty and missing labels. Excel epochs must match. SQLite requires matching original names, declared types, primary-key metadata and ordering. Mismatches identify the source and differing metadata; they never trigger column alignment or business normalization. CSV remains `VARCHAR`; Excel/SQLite remain `VARIANT`. Every row retains its original source ID and source-row reference, with SQLite's actual key retained too. Ordinals restart within each source and SQL must explicitly order results.

The return value contains `tables` with combined metadata/counts/source IDs and a `source_catalog` entry for each original source, including its target table, individual count, column labels, layout and format-specific metadata. This complete JSON catalog belongs to the host and the current business/connection; it is not a SQL table and is not automatically inserted verbatim into model context. The eventual loop must retain it while presenting bounded observations. The current inventory still describes unloaded sources.

One DuckDB transaction covers all requests. A failure in a later source or table rolls back every table created by the call, preserves previous tables and removes temporary registrations after rollback. Metadata is returned only after commit. The caller owns an idle connection and the loader leaves it open. Repeated calls create new unused table names; cache reuse and business-lifetime isolation remain T20. Single-source public API behavior remains unchanged.

The model-free command is:

```sh
.venv/bin/python solver.py --load-sources train/data/biz_102 --requests-file /path/to/requests.json
```

The JSON file contains an array such as `[{"source_ids":["S002","S003"],"header_rows":[1],"first_data_row":2},{"source_ids":["S001"],"header_rows":[1],"first_data_row":2}]`. The CLI rejects duplicate JSON keys, nonfinite JSON constants and invalid requests, requires the file flag only in this mode, and rejects single-source/layout flags. It uses the same restricted in-memory DuckDB settings and exits 0/1/2 for success, input/loading failure or invalid CLI arguments.

Nineteen T11 tests cover multi-format requests as separate tables; CSV/Excel/SQLite combinations; type-exact headers and epoch/schema/ordering mismatches; duplicate and composite-key preservation; complete prevalidation; late rollback across sources and requests; native insertion failures for all three formats; staging cleanup; bounded append chunks; empty sources; repeated calls and existing-table preservation; model-free CLI success and malformed inputs. All 149 tests passed in 31.4 seconds; the output is retained in `artifacts/t11-tests.log`.

All 22 reviewed compatible groups passed epoch checks and exact canonical value/type/source-reference comparisons against independent bounded readers. Every source catalog entry maps to its original inventory source and target table; combined counts equal the sum of individual source counts. No staging views remain. Requests are explicit verification fixtures in `artifacts/t11-requests.json`, never consumed by inference.

| Split | Combined groups | Worksheets | Rows verified | Business cells verified |
|---|---:|---:|---:|---:|
| Train | 14 | 200 | 1,114,049 | 10,271,361 |
| Public test | 8 | 99 | 498,640 | 9,853,258 |
| Total | 22 | 299 | 1,612,689 | 20,124,619 |

Released examples additionally verified one CSV from each split (50 rows each), and two separate SQLite tables in one call on each split (four rows each). A real CLI subprocess passed with a nonexistent model configuration. The primary verification took 1,387.1 seconds (23.1 minutes), including source loading and independent comparisons; it is not an inference-throughput measurement. `artifacts/t11-batch-loading.json` records results, metadata, code/settings hashes, 67 unchanged source-file hashes and preserved earlier-evidence hashes. Repeat with `.venv/bin/python artifacts/t11-verify.py`.

The worksheet containing the 30 existing error cells is outside the 22 compatible groups, so a supplemental mixed-format call loaded it and a separate CSV table. All 220 worksheet rows / 3,740 cells and 50 CSV rows matched independent readers; all 30 errors remained tagged literal values. `artifacts/t11-existing-errors.json` records this check and unchanged source hashes; repeat with `.venv/bin/python artifacts/t11-errors-verify.py`. Earlier evidence, dependencies and model settings were preserved. No model training or question-answer inference was performed. T12 restricted model-facing SQL is next; runtime caching remains T20.

T3's `solver.py --feasibility` command counted all 4,000 questions across 44 businesses using complete rulebooks, READMEs and metadata inventories. The maximum prompt was 9,347 tokens, leaving 2,941 tokens after the 1,024-token response allowance and 3,072-token diagnostic reservation. All eight uncached synthetic development-only requests passed with exact prompt-count agreement, including 128 integer ones (260 output tokens). Requests took 40.3-82.3 seconds; the complete diagnostic took 509.1 seconds. Model/configuration settings were unchanged. Final tool instructions and loaded data tables are not yet included; repeat context qualification after T6/T14 and memory qualification with real tool workloads.

T3 is classified `passed_with_concerns`: system-wide swap was about 15.1 GiB before model loading and 21.5 GiB after the workload, an increase of 6.45 GiB. These counters include background applications and do not establish model-only causality. Peak process RSS was 2.63 GiB; native GPU allocations are recorded separately and must not be blindly added to RSS or treated as complete physical-memory headroom. The saved report is `artifacts/t3-feasibility.json`; `t3-metal.log` retains native initialization/allocation logs. Its final assessment includes swap-growth analysis from the saved snapshots; the original unannotated result is retained as `t3-before-memory-assessment.json`.

Regression cases include titles before headers, two-row headers, monthly schema mismatches, repeated invoice totals, legitimate duplicate ledger rows, continuation payments, Vietnamese/serial dates, mixed money, chained units, embedded items, multiple jobs per visit, grouped missing fields, revenue/debt/as-of rules, partial coverage, unsupported history, rounding and ordering. Use synthetic or training examples. Public inspection is for input compatibility; do not collect public answers or use public/private question-answer examples for training.

Keep 2,370 development and 630 validation questions. Held-out businesses: biz_107, biz_111, biz_115, biz_119, biz_122 and biz_124. Select the first 32-question pilot only from development businesses, covering eight reference answer types and CSV/XLSX/SQLite. Inference never receives gold answers. Run unchanged frozen versions to check reproducibility; retain the organizer's absolute PA-difference tolerance of 0.03 and final authoritative rerun.

Acceptance requires measured PA/coverage, correct output records, bounded termination, source-grounded traces, required runtime/memory feasibility, offline container execution and reproducibility. Successful tool execution alone never demonstrates answer correctness.

## Research references

- User's video: https://www.youtube.com/watch?v=GrNbuWWJYiI . The video was read via its timestamped transcript, not watched: https://www.usetranscribe.io/yt/GrNbuWWJYiI/ai-agent-harness-loop . Relevant sections: memory at 1:41-10:30, stopping at 10:30-14:01, and tracing/evaluation/development feedback at 14:01-19:59.
- Creator's primary companion architecture: https://github.com/ShenSeanChen/waku-agent/blob/main/docs/architecture.md . Confirms a harness can use a plain Python loop and local state. Reference only; no source or diagrams copied into this project.
- Creator's evaluation documentation: https://github.com/ShenSeanChen/waku-agent/blob/main/docs/evals.md . Distinguishes deterministic checks from subjective judged scores. DataMate uses its supplied reference answers and deterministic scoring.

The memory mapping, trace contract and competition-specific freeze boundary above are this project's engineering interpretation. They are not claims that the video requires a particular library, cloud service or memory database.
