# Repository Guidelines

## Project Structure & Module Organization

This is an asynchronous Python BLE library using a `src/` layout. Public, reusable code lives in `src/govee_ble_client/`; shared discovery, GATT, framing, encryption, and exceptions are at the package root. Device-specific protocol profiles belong in `src/govee_ble_client/profiles/` (for example, `h7102.py`). Runnable inspection and verification tools live in `examples/`. PacketLogger captures may be kept at the repository root for protocol investigation; treat them as potentially identifying data.

## Build, Test, and Development Commands

- `uv sync --all-groups` installs the project and development dependencies from `uv.lock`.
- `uv build` builds source and wheel distributions.
- `uv run pytest` runs the pytest suite. There is currently no `tests/` directory; add focused tests as protocol behavior becomes testable.
- `PYTHONPATH=src python examples/watch_h7102_raw_ble.py` runs a local BLE monitor. Other `watch_*` and `verify_*` examples follow the same pattern. These may connect to or change a physical device; read each script’s prompts first.

## Coding Style & Naming Conventions

Use four spaces, type annotations, descriptive docstrings, and `snake_case` for modules, functions, and variables. Use `PascalCase` for classes and `UPPER_SNAKE_CASE` for constants. Keep shared transport behavior in package modules and model-specific command interpretation in profiles. Preserve raw payload bytes when their meaning is uncertain, and label observed facts separately from assumptions. No formatter or linter is configured in `pyproject.toml`.

## Testing Guidelines

Use pytest and name test modules `test_*.py`; organize tests to mirror `src/govee_ble_client/`. Cover packet framing, parsing, validation, and state updates with captured or constructed frames. Do not require live BLE hardware for routine tests. Before relying on a newly inferred command, verify it against a capture or device and record that evidence in its profile documentation.

## Commit & Pull Request Guidelines

Recent commits use short imperative subjects such as `Support H7102 BLE govee device`. Follow that style and keep each change focused. Pull requests should explain the affected device/profile, summarize protocol evidence and remaining uncertainty, list validation commands, and call out any commands that change physical devices. Never commit credentials or unredacted captures containing personal identifiers.

## Security & Device Safety

BLE captures and device identifiers can contain personal data; inspect and redact them before sharing or committing. The legacy Govee encryption transport is compatibility code, not modern authenticated encryption. Keep secrets and sensitive packet contents out of logs, and clearly identify experimental writes that may affect a device.
