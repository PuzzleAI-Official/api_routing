# PuzzleAI Python SDK

Thin Python client for the Puzzle API.

Install:

```bash
pip install puzzleai
```

Use:

```python
from puzzle import Client

client = Client(api_key="YOUR_API_KEY", base_url="YOUR_ALPHA_BASE_URL")

with open("invoice.pdf", "rb") as file:
    result = client.documents.invoices.extract(
        file=file,
        line_items_mode="preferred",
        idempotency_key="invoice-demo-001",
    )

print(result["request_id"])
```

The package name is `puzzleai`; the import name is `puzzle`.

During alpha, pass your assigned `base_url` explicitly or set `PUZZLE_BASE_URL`.
