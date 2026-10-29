# DataMate

A local, SQL-first agent harness for the Zalo AI Challenge DataMate task.

T1–T11 are implemented: local model loading, question validation, business context,
source inspection, CSV/Excel/SQLite loading, and atomic batch loading with provenance.
The agent loop and answer-quality evaluation are still under development.

- [Progress and implementation plan](docs/datamate-plan.md)
- `solver.py`: command-line entry points, context preparation, and model adapter.
- `data_tools.py`: trusted source access, inspection, and loaders.
- `tests/`: deterministic host tests; currently 149 passing cases.

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
