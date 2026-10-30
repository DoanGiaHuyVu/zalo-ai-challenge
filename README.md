# DataMate

A SQL-first agent harness for Vietnamese business-data questions in Zalo AI Challenge DataMate.

T1–T15 are implemented: local model loading, question validation, business context,
source inspection, CSV/Excel/SQLite loading, atomic batch loading with provenance,
restricted SELECT-only SQL execution with cancellation, complete question-local
result storage with bounded previews, strict four-action contracts, and a bounded
injectable loop verified with scripted/replayed synthetic episodes.
T16 adds the real local-model provider and a bounded diagnostic command. Its live
qualification **failed**: Metal, token accounting and business SQL worked, but
repeated rejected finishes exhausted eight turns. The separate OpenRouter 14B
experiment completed in six turns but answered incorrectly. Production checks,
typed finishing, declines and scoring remain unfinished. **Next: T17.**

Use OpenRouter as the provisional development backend for faster iteration.
Keep the local model adapter for the final offline solution and CUDA testing.
No final model choice or general answer accuracy has been established.

- [Progress and implementation plan](docs/datamate-plan.md)
- `solver.py`: command-line entry points, context/action preparation, bounded runner, replay, and model adapter.
- `data_tools.py`: trusted source access, inspection, loaders, SQL permissions, and stored results.
- `openrouter_probe.py`: separate hosted-model experiment with an unverified numeric finisher.
- `evaluate.py`: planned scorer; not implemented yet.
- `tests/`: **278 passing cases**, including 16 OpenRouter cases. Passing tests are not an accuracy score.

## Next checkpoints

1. **T17:** enforce candidate-bound coverage, parsing and grain checks over complete stored results.
2. **T18–T19:** construct typed answers and implement production finishing/declines.
3. **Early T21a:** build the scorer and preserve the frozen business split.
4. **P1 / M0:** preserve stable prompt prefixes, then attempt and score one production run.
5. **Early K0:** test the pinned local 8B on Kaggle CUDA, starting with one T4.
6. Complete T20 and reporting, then run the frozen 32-question development pilot.

Kaggle diagnostics will have an explicit 300-second profile; the NVIDIA target
remains 60 seconds. These profiles and the future production `--solve` command
are planned, not implemented. Full offline Kaggle/Docker qualification remains
later work. Embeddings, fine-tuning and additional agents are deferred until
measured failures justify them.

## Local setup

Use Python 3.11 and the pinned dependencies in `uv.lock`. On macOS with Metal:

```sh
CMAKE_ARGS="-DGGML_METAL=ON" uv sync --locked --no-binary-package llama-cpp-python
.venv/bin/python -m unittest discover -s tests -q
```

Place the organizer datasets in `train/` and `public_test/` when running data commands.
The model artifact identity and local path are pinned in `config.json`.
Datasets, model weights, the virtual environment, and local verification artifacts
are excluded from Git. Deterministic tests use synthetic temporary fixtures.

## Real-model diagnostic

```sh
.venv/bin/python solver.py --config config.json --model-probe q_0cb7c9b93048 --split-root train
```

This development-only probe uses no gold answers or finish callback. Native GPU
access is required on macOS. It saves `artifacts/t16-probe.json`, `t16-progress.json`
and `t16-metal.log`; repeat attempts archive earlier reports automatically.
The current report records integration progress, not answer accuracy. See the
progress document for both attempts, verification evidence and remaining failure.

## OpenRouter development experiment

Set `OPENROUTER_API_KEY` in the shell environment, then run:

```sh
.venv/bin/python openrouter_probe.py \
  --question-id q_0cb7c9b93048 --split-root train
```

If your key is in a trusted local `.env`, export it first in your shell:

```sh
set -a
source .env
set +a
.venv/bin/python openrouter_probe.py --split-root train
```

The script does not automatically load `.env`. Its default question is the same
known development case shown above.

This sends the selected business context and tool previews to OpenRouter's
`qwen/qwen3-14b`. Data access and SQL still run through the existing local harness.
It uses the local 8B vocabulary on CPU for estimated token admission; it does not
run the local model or require Metal. Provider-reported usage is saved separately.

The script-only finish callback can project one stored numeric cell (`number` or
`percent`) after checking compute/check references and their association. It does
not enforce T17 diagnostic semantics or establish answer correctness. Every result
is labeled experimental and unverified. Gold answers are never sent.

Reports and partial progress go to a fresh `artifacts/openrouter/<timestamp>/`
directory. Use `--output-dir` to choose a new directory; existing directories are
never overwritten. HTTP failures stop without automatic retries. Unsupported
provider parameters/schema support are reported without model or format fallback.
The wire schema omits XGrammar's unsupported `uniqueItems` keyword; the original
local validator still enforces uniqueness. Both schema hashes are recorded.
The script retains the eight-turn and 300-second question limits.

The completed live experiment took **25.53 seconds / six turns** and returned
`0`; the independently computed and reference-confirmed answer is **37**. Its
SQL compared raw Vietnamese dates such as `23 thg 9 2025` with `2025-09-23` without
parsing them. The check repeated that filter, and the experiment-only finisher
accepted the candidate. This demonstrates integration, not correct answering.
The case is now a regression example; broader quality belongs to the frozen pilot.

Keep the key out of source files and chat. This experiment is separate from the
offline competition baseline and does not replace T16 or production finishing.
