# Host integration

This page is for Meridian PR-3c. It gives the Meridian host side of each
value that this plug-in owned in-tree. The rules come from Meridian issue
#147, parts (b), (d) and (e). The host code is in Meridian `server/plugins/`.

## Registry entry

`meridian-backend plugin install` builds the registry entry from `plugin.json`
of the release archive (`release/build_archive.py`). The table gives the
`plugin.json` values. The installer changes `command[0]` to an absolute path
in the version directory.

| `plugin.json` field | Value |
| --- | --- |
| `id` | `meridian-provider-sharadar` |
| `family` | `market-data` |
| `binding` | `{"provider_id": "sharadar"}` |
| `contract_version` | `1.0.0` |
| `contract_revision` | The `revision` of `contracts/market-data/v1/contract.json` |
| `command` | `["bin/meridian-provider-sharadar/meridian-provider-sharadar", "plugin.json"]` |
| `grants` | `["plugin_cache"]` |
| `credentials` | `{"api_key": {"secret": "sharadar_api_key", "env": "SHARADAR_API_KEY"}}` |

The binding keeps the persisted Meridian provider id `sharadar` valid.

## API key ownership

The host owns the key. The registry entry names it as the credential
`api_key`, with the secret `sharadar_api_key` and the environment variable
`SHARADAR_API_KEY`. These are the names of today, so the owner does not enter
the key again. The host reads `SHARADAR_API_KEY` first and then
`sharadar_api_key` in `.secrets.json` of the data directory. The host is the
only writer of that file. It sends the key in the stdin handshake as
`secrets.api_key`. The plug-in keeps the key in memory only.

After each key write or delete, the host stops the plug-in. The next request
starts it with the new value. When no key exists, the host starts the plug-in
without `secrets.api_key`, so that `validateCredentials` can check a first
key.

## Status values of `useSharadarStatus.js`

The Meridian hook `src/hooks/useSharadarStatus.js` read
`GET /api/config/sharadar-status` and returned `configured`, `source`,
`loading` and `refresh`. The hook started with `configured` false and kept
it false when the status request failed. When `loading` was false and
`configured` was false, `src/App.jsx` ran with `yfinance` for a persisted
`sharadar` choice. The persisted choice stayed. PR-3c replaces the hook with a
generic hook. The plug-in has no user interface, so this repository holds no
JavaScript.

| Hook value | Host source after PR-3c |
| --- | --- |
| `configured` | `configured` of `GET /api/plugins/meridian-provider-sharadar/credentials`. It is `true` when a key is present and `false` when no key is present. It is `null` when the registry has no entry for the provider. #147 gives no value for a failed credential request, so PR-3c decides it. The old hook kept its last value, which is `false` at the start: after a failed first request the fallback to `yfinance` ran, and after a good read of `true` a failed refresh kept `sharadar`. |
| `source` | `source` of the same route: `env`, `file`, `developer` (a `url` entry) or `null`. |
| `loading` | `true` until the credential request completes. |
| `refresh` | Reads the credential route again. |
| none (new value) | `state` of the entry in `GET /api/plugins`, for example `confirmed`, `ready`, `unconfirmed` or `crashed`. That route reads the registry and starts no plug-in. |

Rules for the generic hook:

- `configured` keeps its meaning: a credential is present. The plug-in state
  is a separate value and never changes `configured`.
- The `App.jsx` fallback to `yfinance` applies only when `configured` is
  `false`. A `null` or a plug-in fault never changes the
  selected provider. A `sharadar` request then gets the 422 (no key) or the
  503 (plug-in missing or not ready) of the host.
- `ApiKeyModal.jsx` writes with `PUT` and deletes with `DELETE` on the same
  credential route. The host checks a new key with `validateCredentials`
  before it stores the key. A delete of an environment key gets 409, as
  today. A write or a delete for a `url` entry gets 409.

## Operations

The manifest lists six operations of `contracts/market-data/v1`:
`prices`, `statements`, `info`, `validate`, `dividends` and
`validateCredentials`. `fund_fundamentals` belongs to `meridian-provider-fii`.
`max_concurrency` is 8, the Sharadar worker limit of Meridian
(`SHARADAR_TICKER_WORKERS`, `SHARADAR_DOWNLOAD_WORKERS`).

Each data operation makes a new provider object with
`cache_ttl_hours = max_age_hours`, so the request sets the age limit of the
SF1 and TICKERS cache entries. Price windows keep the fixed 24 hours of the
provider. The raw cache is in the `plugin_cache` grant, at
`<plugin_cache>/cache/sharadar/`.

| Operation | Provider call | Result for the host |
| --- | --- | --- |
| `prices` | One `_fetch_tickers_meta` read with `require_price_currency`, which gives the currency and the SEP or SFP table, then `price_frame` | `price_currency` and the table `Date`, `Open`, `High`, `Low`, `Close`, `Adj Close`, `Volume`. The price columns have the unit `price_currency`. |
| `statements` | `_build_df` over `_fetch_sf1` with the map of `kind` | One row for each fiscal period, newest first: `period_end` and one column for each contract label. |
| `info` | `get_ticker_info(include_beta=beta)` | The 12 contract fields. A field that the provider drops today is `null`. `is_etf` is `quoteType == "ETF"`, which equals `is_etf()`. |
| `validate` | `validate_ticker` | `valid` and `name` |
| `dividends` | `get_dividends` | One row for each fiscal period, oldest first: `period_end` and `dps`. A period with missing inputs has `dps` `null`. |
| `validateCredentials` | One TICKERS query for `AAPL` with the candidate in the `X-Api-Token` header | `valid` and `code` |

## Failures

The problem types follow `contracts/market-data/v1`: `source_unavailable` for
a failed query, `source_invalid` for data that fails a check.

| Problem | HTTP status | When | Today's result that the host keeps |
| --- | --- | --- | --- |
| `source_unavailable` | 503 | A query that failed before Nasdaq Data Link returned an HTTP answer, a Nasdaq Data Link error answer, or a local cache file error. | `statements`: an empty frame. `dividends`: an empty series. `validate`: `{valid: false, name: null}`. `prices`: a raise in the engine path and an empty frame in the screening history. `info`: an empty dict, the host mapping of #147 (b). The in-tree provider raised for a cache file error before the TICKERS query. |
| `source_invalid` | 502 | An answer that the client cannot parse, or that fails a provider check: SF1 or TICKERS rows, a metadata conflict, no TICKERS row or no valid currency for `prices`, and price rows (a date outside the window, a duplicate date, a missing or non-positive close). | A raise, except in the four cases below |
| `credential_missing` | 503 | The handshake has no `api_key`. | The 422 of the host comes first, because the host checks the key. |
| `invalid_ticker` | 422 | The Meridian ticker rules reject the ticker. | A raise, as `sharadar_provider_ticker` does |
| `invalid_request` | 422 | The body does not match the contract. | A host fault |

A `prices` query with no price rows gives HTTP 200 with `price_currency` and a
table with no rows, as the contract says for a query with no rows. The in-tree
`download_prices_csv` raised "no price data" for it. #147 (b) gives that raise
to the host: the engine download raises for a table with no rows, and the
screening history keeps an empty frame.

`info` keeps the partial results of today: a failed beta removes only `beta`,
and a failed metadata read gives the fallback fields. A metadata conflict
gives `source_invalid`, because the in-tree provider raised it too. Each
problem has a fixed title and no provider text.

Four results differ from the in-tree provider. The contract decides them, and
PR-3c decides the host result:

1. SF1 rows that fail a check give `source_invalid`. The in-tree provider
   caught the error and returned an empty frame or series
   (`test_sf1_rows_that_fail_a_check_are_invalid`).
2. TICKERS rows that fail a check give `source_invalid`. The in-tree
   `validate_ticker` returned `{valid: false, name: null}`
   (`test_tickers_rows_that_fail_a_check_are_invalid`).
3. `prices` needs a valid TICKERS row with a currency (#147 (b)). The
   in-tree screening history called `is_etf()` and `_fetch_prices` with no
   currency check. It gave SEP rows for a ticker without a currency, and also
   when the TICKERS query failed or gave no row, because `is_etf()` then
   returned `False`. The plug-in gives `source_invalid` or
   `source_unavailable` in these cases
   (`test_prices_need_a_currency_where_the_in_tree_screening_history_did_not`).
4. An HTTP 200 answer that the client cannot parse (a body that is not JSON,
   a body without the datatable shape, a row that the client cannot convert)
   gives `source_invalid`. For SF1 and TICKERS, the in-tree provider caught
   each such error and returned an empty result. For prices, it raised a
   `ValueError` of the client (a body that is not JSON, a row with an invalid
   date) and returned an empty frame for another parse error, for example `{}`
   (`test_an_answer_that_the_client_cannot_parse_is_invalid`).

`validateCredentials` gives `valid: false` with `key_rejected` (Nasdaq HTTP
400, or a candidate that is not printable ASCII, with no request),
`key_invalid_or_expired` (403) or `no_subscription_data` (200 with no rows). The in-tree route gave HTTP 401 for these three. Another status, a
transport failure, a body that is not JSON, and a `data` value that is present
and not a list give `source_unavailable`. The in-tree route gave HTTP 502 for another
status and a transport failure. It gave an unhandled error for a body that is
not JSON, and it accepted any `data` value that is not empty. `secret_name`
is the credential name `api_key`, the same name as in the handshake.

## Transport

The Nasdaq Data Link client follows no redirect, because it sends the key in
the `x-api-token` header and Requests keeps that header on a redirect to
another host. The in-tree provider followed redirects. The plug-in imports
the client without `NASDAQ_DATA_LINK_API_KEY`, `NASDAQ_DATA_LINK_BASE_DOMAIN`
and `~/.nasdaq/data_link_apikey`, and pins the endpoint
`https://data.nasdaq.com/api/v3`.

## Decode rules for the host adapter

The parity tests in `tests/test_parity.py` decode each response as the host
adapter must:

- Parse each decimal string with Python `float()`. `float()` gives the
  nearest double. `pandas.to_numeric` does not, so it can change the last
  digit of a value.
- Treat a column as integer when no cell has a `.` and no cell is `null`.
  Otherwise treat it as float. The encoding of a float with an integer value
  of `1e16` or more has no `.`, so the rule then gives an integer column. A
  price column of the parity tests has no such value.
- Write the price CSV with the columns in the order
  `Date,Open,High,Low,Volume,Close,Adj Close`. The in-tree provider wrote this
  order. The table has the contract order, so the host must change the order.
  With these rules, the CSV of the parity tests is byte for byte the CSV of
  the in-tree provider.
- Decode each observation: `identity` is the canonical JSON text of the
  identity object (`json.loads` gives the object back), and
  `source_fetched_at` ends with `Z` where the stored text ends with `+00:00`.
  The decoded observation is the schema version 2 observation of the in-tree
  provider, byte for byte.
- `provenance.identity` is the canonical JSON of the primary query: SF1 for
  `statements` and `dividends`, TICKERS for `info` and `validate`, the price
  table and start for `prices`. `provenance.source_fetched_at` is `null` when
  that query wrote no cache entry, for example for zero rows.
