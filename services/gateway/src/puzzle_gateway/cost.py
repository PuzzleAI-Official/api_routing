from __future__ import annotations


def normalize_mock_cost(*, provider_cost_units: int, quantity: int = 1) -> int:
    return max(provider_cost_units, 0) * max(quantity, 1)


def charge_units_for_cost(cost_units: int) -> int:
    return max(cost_units, 0) * 2
