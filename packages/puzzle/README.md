# PuzzleAI Python SDK

Thin Python client for the Puzzle API.

Puzzle routes document workflows through verified providers and returns normalized
results. The SDK follows the public API closely and gives Python developers a
clean way to upload files, pass routing options, use idempotency keys, poll jobs,
and handle Puzzle errors.

## Install

```sh
pip install puzzleai
```

The package name and import name are both `puzzleai`.

## Quickstart

```python
from puzzleai import Client

client = Client(api_key="YOUR_API_KEY", base_url="YOUR_ALPHA_BASE_URL")

with open("invoice.pdf", "rb") as file:
    result = client.documents.invoices.extract(
        file=file,
        line_items_mode="preferred",
        idempotency_key="invoice-demo-001",
    )

print(result["request_id"])
```

During alpha, pass your assigned `base_url` explicitly or set
`PUZZLE_BASE_URL`.

## Invoice extraction

Use invoice extraction when you want a normalized invoice object with fields
like vendor name, invoice number, dates, totals, tax, and line items.

```python
from puzzleai import Client

client = Client(api_key="YOUR_API_KEY", base_url="YOUR_ALPHA_BASE_URL")

with open("invoice.pdf", "rb") as file:
    result = client.documents.invoices.extract(
        file=file,
        filename="invoice.pdf",
        content_type="application/pdf",
        line_items_mode="preferred",
        required_fields=["total"],
        strategy="balanced",
        idempotency_key="invoice-2026-001",
    )

invoice = result["invoice"]
print(invoice["total"])
print(result["quality"])
```

Common invoice options:

- `line_items_mode`: `"preferred"`, `"required"`, or `"disabled"`.
- `required_fields`: invoice fields that must be present for an accepted result.
- `strategy`: routing preference, such as `"balanced"`, `"cheapest"`, or
  `"highest_quality"`.
- `provider`: optional explicit provider ID approved for your account.
- `service_id`: optional explicit provider service.
- `provider_set`: optional provider set name assigned to your account.
- `provider_options`: provider-specific options approved for your alpha account.
- `features`: optional routing hints approved for your account; most requests can omit this.
- `idempotency_key`: required for billable requests.

## Async invoice jobs

Use async submit for larger files or workflows that should run in the
background.

```python
from puzzleai import Client

client = Client(api_key="YOUR_API_KEY", base_url="YOUR_ALPHA_BASE_URL")

with open("invoice.pdf", "rb") as file:
    job = client.documents.invoices.submit(
        file=file,
        line_items_mode="preferred",
        idempotency_key="invoice-job-2026-001",
    )

status = client.jobs.get(job["job_id"])
print(status["status"])
```

## General document processing

Use document processing for lower-level document tasks, such as text, fields,
and tables, without invoice-specific output rules.

```python
from puzzleai import Client

client = Client(api_key="YOUR_API_KEY", base_url="YOUR_ALPHA_BASE_URL")

with open("document.pdf", "rb") as file:
    result = client.documents.process(
        file=file,
        task="parse",
        required_capabilities=["documents.text"],
        optional_capabilities=["documents.layout", "documents.tables"],
        strategy="balanced",
        idempotency_key="document-parse-001",
    )

print(result["full_text"])
```

The async version is:

```python
with open("document.pdf", "rb") as file:
    job = client.documents.submit(
        file=file,
        task="parse",
        idempotency_key="document-job-001",
    )
```

## Async client

```python
from puzzleai import AsyncClient

async with AsyncClient(api_key="YOUR_API_KEY", base_url="YOUR_ALPHA_BASE_URL") as client:
    with open("invoice.pdf", "rb") as file:
        result = await client.documents.invoices.extract(
            file=file,
            idempotency_key="async-invoice-001",
        )

    print(result["request_id"])
```

## Error handling

Puzzle maps API errors to Python exceptions.

```python
from puzzleai import (
    Client,
    PuzzleAuthenticationError,
    PuzzleIdempotencyConflictError,
    PuzzleProviderUnavailableError,
    PuzzleRateLimitError,
    PuzzleValidationError,
)

client = Client(api_key="YOUR_API_KEY", base_url="YOUR_ALPHA_BASE_URL")

try:
    with open("invoice.pdf", "rb") as file:
        result = client.documents.invoices.extract(
            file=file,
            idempotency_key="invoice-error-demo-001",
        )
except PuzzleAuthenticationError:
    print("Check your API key.")
except PuzzleRateLimitError:
    print("Slow down and retry later.")
except PuzzleIdempotencyConflictError:
    print("Use a new idempotency key or replay the exact same request.")
except PuzzleProviderUnavailableError:
    print("No eligible provider was available for this request.")
except PuzzleValidationError as exc:
    print(exc)
```

Every `PuzzleError` includes:

- `code`
- `request_id`
- `details`

## API keys and idempotency

Pass your API key with `Client(api_key=...)`. Billable operations should include
an `idempotency_key`. If you retry the exact same request with the same key,
Puzzle returns the stored result instead of creating a second billable attempt.

## Alpha access

- Use the base URL, API key, and workflows enabled for your alpha account.
- Review extracted results before connecting them to automated financial actions.
- Only upload documents covered by your alpha agreement.
- Do not share API keys in support messages.
