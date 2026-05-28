# PuzzleAI Python SDK

Thin Python client for the Puzzle API.

Install:

```bash
pip install puzzleai
```

Use:

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

The package name and import name are both `puzzleai`.

During alpha, pass your assigned `base_url` explicitly or set `PUZZLE_BASE_URL`.
