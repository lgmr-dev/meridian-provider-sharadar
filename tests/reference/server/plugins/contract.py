from dataclasses import dataclass

PROTOCOL_VERSION = 1
SHORT_SECONDS = 30.0
MEDIUM_SECONDS = 120.0
LONG_SECONDS = 900.0


@dataclass(frozen=True)
class FamilyContract:
    family: str
    status: str
    revision: str
    minimum_major_minor: tuple[int, int]


FAMILY_CONTRACTS = {
    "tax": FamilyContract(
        family="tax",
        status="draft",
        revision="635282bf732a767781c9576b0c514a5100acc0db9b037c6f8521e5383282d9ea",
        minimum_major_minor=(1, 0),
    ),
    "market-data": FamilyContract(
        family="market-data",
        status="draft",
        revision="172718857d1cefa4f74970482d8675c67271328f985fc38ddf3ac913ef2731a5",
        minimum_major_minor=(1, 0),
    ),
}

RUNTIME_REVISION = "201754544129e8497d55a26ea46cd8476da69e6045fb418a435b9d9d82ee8582"

OPERATIONS: dict[str, dict[str, float]] = {
    "tax": {
        "listSources": SHORT_SECONDS,
        "putSource": MEDIUM_SECONDS,
        "deleteSource": SHORT_SECONDS,
        "getSourceContent": SHORT_SECONDS,
        "analysisForm": SHORT_SECONDS,
        "discoverAnalyses": MEDIUM_SECONDS,
        "computeChain": LONG_SECONDS,
        "computeBatch": LONG_SECONDS,
        "export": LONG_SECONDS,
        "referenceTree": SHORT_SECONDS,
        "referenceDoc": SHORT_SECONDS,
        "referenceSearch": SHORT_SECONDS,
        "listOverrides": SHORT_SECONDS,
        "putOverride": SHORT_SECONDS,
        "deleteOverride": SHORT_SECONDS,
    },
    "market-data": {
        "prices": MEDIUM_SECONDS,
        "statements": MEDIUM_SECONDS,
        "info": SHORT_SECONDS,
        "validate": SHORT_SECONDS,
        "dividends": MEDIUM_SECONDS,
        "fund_fundamentals": MEDIUM_SECONDS,
        "validateCredentials": SHORT_SECONDS,
    },
}


def operation_timeout(family: str, operation: str, body: object) -> float:
    if family == "market-data" and operation == "info" and isinstance(body, dict) and body.get("beta") is True:
        return MEDIUM_SECONDS
    return OPERATIONS[family][operation]


def operation_path(operation: str) -> str:
    return f"/v1/{operation}"


def contract_matches(family: str, contract_revision: str) -> bool:
    return contract_revision == FAMILY_CONTRACTS[family].revision
