# PuzzleAI Python SDK Release

The public Python package distribution is `puzzleai`.

Users install:

```bash
pip install puzzleai
```

Users import:

```python
from puzzleai import Client
```

## What Gets Published

The release workflow builds a wheel only from `packages/puzzle`.

Allowed wheel contents:

```text
puzzleai/__init__.py
puzzleai/_client.py
puzzleai/_exceptions.py
puzzleai/_types.py
puzzleai/py.typed
puzzleai-*.dist-info/*
```

The wheel must not contain gateway, worker, telemetry, provider adapter, Terraform,
script, test, registry, secret, or environment files. The GitHub Actions release
workflow checks this before publishing.

Python packages are inspectable by users after installation. That is expected.
The boundary is that users can inspect the SDK wrapper, but not private backend
service source code because backend service code is not included in the package.

## Release Flow

1. Create a PyPI project named `puzzleai`.
2. Configure PyPI Trusted Publishing for this repository and the
   `Publish PuzzleAI Python SDK` workflow.
3. Configure TestPyPI Trusted Publishing the same way for test releases.
4. Run the workflow manually with `target=testpypi`.
5. Install from TestPyPI in a clean environment and verify import/use.
6. Run the workflow manually with `target=pypi`.

No long-lived PyPI token is required when Trusted Publishing is configured.

## Local Verification

From the repository root:

```bash
python -m pytest tests/test_sdk_client.py -q --no-cov
python -m mypy packages/puzzle/src/puzzleai tests/test_sdk_client.py
python -m ruff check packages/puzzle/src/puzzleai tests/test_sdk_client.py
```

Build the wheel:

```bash
cd packages/puzzle
python -m hatchling build -t wheel
```

Inspect the wheel:

```bash
python -c "import zipfile; print('\n'.join(zipfile.ZipFile('dist/puzzleai-0.1.0a2-py3-none-any.whl').namelist()))"
```
