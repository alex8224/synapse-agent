---
name: python-testing
description: How to run and write Python tests in this project with uv and pytest.
license: MIT
compatibility: Requires uv and pytest in a Python 3.12+ project.
metadata:
  version: "1.1.0"
  owner: coding-agent
allowed-tools: execute run_tests read_file write_file edit_file
---

# Python testing skill

## Run tests
Prefer:

```bash
uv run pytest
```

Narrowest useful check first:

```bash
uv run pytest path/to/test_file.py -q
uv run pytest -k test_name -q
```

## Writing tests
- Put unit tests under `tests/`.
- Prefer fast pure-python tests without network.
- For coding-agent E2E demos, use `tests/fixtures/sample_repo`.

## After code changes
1. Run the focused test.
2. If green, optionally run broader suite.
3. Report command + exit code in the final summary.
