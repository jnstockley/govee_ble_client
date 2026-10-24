# Repository Guidelines

## Project Structure & Module Organization

This async Python BLE library uses `src/`. Discovery,
GATT transport, framing, encryption, and exceptions live in
`src/govee_ble_client/`. Device-specific protocols belong in `profiles/`
(for example, `h7102.py`). Library tests live in `tests/`; example tests are
in `tests/examples/`. Runnable monitors and verification tools live in
`examples/`. App documentation: `docs/APP_INTEGRATION.md` and `docs/API.md`.

## Build, Test, and Development Commands

- `uv sync --locked --all-groups`: install locked dependencies.
- `uv build`: build source and wheel distributions in `dist/`.
- `uv run pytest`: run offline library tests.
- `uv run pytest tests/examples`: run example tests explicitly; default collection excludes them.
- `uv run pytest --cov=govee_ble_client --cov-branch`: enforce the 95% library coverage minimum, including branches.
- `uv run ruff check`: lint Python; `uv run ruff format --check`: verify formatting.
- `PYTHONPATH=src python examples/watch_multiple_ble.py`: monitor devices using state queries.

If a moved virtual environment leaves a stale pytest launcher, use
`.venv/bin/python -m pytest`.

## Coding Style & Naming Conventions

Use four spaces, type annotations, docstrings, and `snake_case`
for modules, functions, and variables. Use `PascalCase` for classes and
`UPPER_SNAKE_CASE` for constants. Ruff is a development dependency;
`pyproject.toml` defines no repository-specific Ruff settings. Separate shared
transport from model-specific interpretation. Preserve
uncertain payload bytes and distinguish observed behavior from assumptions.

## Architecture & Integration

Keep I/O asynchronous and coordinate operations per connection while allowing
independent devices to progress concurrently. Preserve cancellation, bounded
buffers, and cleanup guarantees. Never automatically replay uncertain writes.
Support Home Assistant through external discovery and injected connection
setup without requiring Home Assistant or starting competing scanners.

## Testing Guidelines

Use pytest and name modules `test_*.py`. The suite blocks real Bluetooth;
use fake clients and synthetic peripherals. Cover exact frames, malformed
input, state updates, timeouts, cancellation, and reconnect races. Prefer
event-controlled concurrency tests over timing-sensitive assertions. Verify
new commands against captures or hardware and document evidence before
claiming support. See `docs/TESTING.md`.

## Commit & Pull Request Guidelines

Use short imperative subjects, such as `Fix linting` or `Add pytest`. Keep
changes focused. PRs should identify profiles, summarize behavior
and protocol evidence, list validation commands, and explain remaining
uncertainty and device-changing operations.

## Security & Device Safety

Treat captures and device identifiers as potentially identifying data. Redact
before sharing; never commit credentials. Keep secrets
and raw sensitive traffic out of logs. Legacy AES/RC4 is compatibility code,
not modern authenticated encryption. Read hardware-tool prompts before allowing writes.
