import inspect

import pytest
from server import stock_selection_contract as reference

from meridian_provider_sharadar import identity

FUNCTIONS = [
    "validated_stock_selection_ticker_identity",
    "sharadar_provider_ticker",
    "is_valid_security_name",
    "validated_security_name",
]
CONSTANTS = ["MAX_SECURITY_NAME_CHARACTERS", "_STOCK_SELECTION_TICKER_RE", "_B3_FII_OR_ETF_CANDIDATE_RE"]


@pytest.mark.parametrize("name", FUNCTIONS)
def test_function_is_the_meridian_source(name: str) -> None:
    assert inspect.getsource(getattr(identity, name)) == inspect.getsource(getattr(reference, name))


@pytest.mark.parametrize("name", CONSTANTS)
def test_constant_is_the_meridian_value(name: str) -> None:
    assert getattr(identity, name) == getattr(reference, name)
