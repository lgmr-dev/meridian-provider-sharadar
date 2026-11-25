import json
from pathlib import Path

import pytest
from server import sharadar_provider as reference

from meridian_provider_sharadar import provider

CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "market-data" / "v1" / "openapi.json"
MAPS = [("income", "INCOME_STMT_MAP"), ("balance", "BALANCE_SHEET_MAP"), ("cashflow", "CASHFLOW_MAP")]


@pytest.mark.parametrize(("kind", "name"), MAPS)
def test_statement_map_equals_meridian_and_has_the_contract_labels(kind: str, name: str) -> None:
    mapping = getattr(provider, name)
    labels = json.loads(CONTRACT.read_text(encoding="utf-8"))["components"]["schemas"]["StatementLabels"]["properties"]

    assert list(mapping.items()) == list(getattr(reference, name).items())
    assert list(mapping) == labels[kind]["const"]
