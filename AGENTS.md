# meridian-provider-sharadar

This public repository holds the Sharadar market-data plug-in of Meridian.
Meridian is live financial software. A wrong number can change the tax
filings and retirement decisions of a real household. The rules below are the
applicable rules of the Meridian `AGENTS.md` and of the plug-in design in
Meridian issue #147.

## Commands

| Command             | Result                                                       |
| ------------------- | ------------------------------------------------------------ |
| `make install`      | Creates `.venv` and installs `requirements-dev.lock`.        |
| `make hooks`        | Uses the Git hooks of `tools/hooks` in this clone.           |
| `make check`        | Runs the privacy check, Ruff, pytest and the feature-map check. |
| `make secrets`      | Runs gitleaks on the Git history.                            |
| `make dependencies` | Runs osv-scanner on both lock files.                         |
| `make lock`         | Writes the hash-pinned lock files with `uv`.                 |
| `VERSION=<version> make archive` | Builds the archive of this platform and runs its two proofs. |

## Data and privacy

- The repository, tests, docs, CI logs and releases contain only generated
  synthetic data. Real data stays in the external Meridian data directory.
- Do not open a file in the Meridian data directory. Do not test on owner data.
- Do not commit statements, screenshots, cloud paths or local config.
- This repository is public. Each file, commit, branch name, PR, issue,
  comment, CI log, artifact and release is public. Write no owner value,
  home folder path, machine name or private Meridian text in them.
- Copy no value from the owner data into a test or a fixture. Make a
  synthetic value.
- Run `make hooks` in each new clone before the first commit. Do not use
  `--no-verify`.
- Do not commit a symbolic link. `tools/privacy.sh` fails on a symbolic link
  and on a home folder path.

## Money rules

- Missing financial input is unavailable. Do not replace it with zero, a
  default or an estimate. A value that is not finite fails closed.
- Decimals on the wire are plain decimal strings, and a missing value is
  `null` (`contracts/plugin-runtime/v1/openapi.json`, schema `Decimal`).
- The provider results must stay equal to the Meridian in-tree path at
  `aa5d0cbc4d0baef662e91cc087d8a15e97a76e3e`, for each decimal. A change
  that changes a financial value needs owner approval.
- Do not edit `tests/reference/` or `contracts/`. They are byte copies of
  Meridian files, and `tests/test_source_pins.py` checks them. A copy from
  another Meridian commit changes the `meridian_commit` of both `SOURCE.json`
  files, the `SOURCE_COMMIT` of `tests/test_source_pins.py`, and each text
  that names the source commit. A reviewer compares the new blob IDs with
  Meridian.

## Secrets

- Give the provider the API key from the stdin handshake of the Meridian
  host. Do not add code that reads `.secrets.json` or an environment variable
  for it. `SharadarProvider` reads `SHARADAR_API_KEY` only when it gets no
  key, so never make a provider without a key.
- Never log the API key or a candidate key. Never put a key in a file, a
  URL, a response or an error message.
- Tests use synthetic keys only.

## Security

- Bind every port to `127.0.0.1`. Do not bind to a LAN address.
- Tickers become file names. The provider validates them with the
  Meridian ticker rules in `meridian_provider_sharadar/identity.py`.
- Do not use `eval`, `exec` or `pickle` on untrusted data. Do not pass input
  to `shell=True`.
- Do not send provider exception text or a URL with a secret to a client.

## Code rules

- Python 3.12. Give each new function full type hints.
- `featuremap.json` sets which part may import which part. `make featuremap`
  fails on a new break. The baseline only shrinks.

## Workflow

- Use a branch and a pull request. Do not push to `main`.
- Each pull request is one squashed commit. Its subject is the pull request
  title, and it has a `Signed-off-by` line.
- CI runs on GitHub-hosted `ubuntu-26.04`: `lint`, `test`, `featuremap`,
  `secrets`, `dependencies` and `archive-linux`. All six must pass on the
  latest commit before a merge.
- A release is a tag `v<version>` on `main`. The `release` workflow builds,
  proves and publishes the Linux and macOS archives on GitHub-hosted runners.
  It runs on pull requests only when they change `release/`, `notices/`,
  `Makefile`, a lock file or the workflow.
