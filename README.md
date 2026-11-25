# meridian-provider-sharadar

This repository holds the Sharadar (Nasdaq Data Link) market-data
provider of Meridian. Meridian moves the provider out of its core and runs it
as a plug-in process. The design is in Meridian issue #147, part (b).

The provider code comes from Meridian `aa5d0cbc4d0baef662e91cc087d8a15e97a76e3e`:
the 11 files of the `market` group of the private-path manifest (Meridian
issue #150).

## Layout

| Path | Content |
| --- | --- |
| `meridian_provider_sharadar/server.py` | The process runtime: stdin handshake, lock, ready line and the HTTP server on `127.0.0.1`. |
| `meridian_provider_sharadar/operations.py` | The six `market-data/v1` operations. |
| `meridian_provider_sharadar/encoding.py` | Decimal strings, dates, timestamps and canonical JSON of the contract. |
| `meridian_provider_sharadar/credentials.py` | The key check of `validateCredentials`. |
| `meridian_provider_sharadar/provider.py` | The Sharadar provider. It was `server/sharadar_provider.py` in Meridian. |
| `meridian_provider_sharadar/identity.py` | The ticker and security-name validators that the provider imports from Meridian `server/stock_selection_contract.py`. |
| `contracts/` | Copies of the Meridian contracts `plugin-runtime/v1` and `market-data/v1` (Apache-2.0). |
| `tests/reference/` | Copies of the Meridian files that the tests compare with. |
| `tests/` | The provider tests from Meridian and the tests of this repository. |
| `tools/` | The privacy and security checks, the Git hooks and the feature-map checker. |
| `notices/` | The CPython license document and the notices of the uv Python builds that the archives bundle. |
| `release/` | The archive build, the launch proof and the host acceptance. |

`contracts/SOURCE.json` and `tests/reference/SOURCE.json` record the Git blob
ID of each copied file at the source commit. `tests/test_source_pins.py` fails
when a copied file and its `SOURCE.json` entry differ. The test cannot see a
change to both, so a reviewer compares each change of a `SOURCE.json` file
with Meridian:

```sh
git -C <meridian checkout> ls-tree -r aa5d0cbc4d0baef662e91cc087d8a15e97a76e3e
```

## Where the other Meridian files went

Four of the 11 files are tests of Meridian core:
`test_sharadar_consumers.py`, `test_sharadar_cross_source.py`,
`test_sharadar_pipeline.py` and `test_sharadar_api_integration.py`. They
import Meridian modules that stay in Meridian, so this repository does not
hold them. Meridian keeps the same files. Meridian PR-3c moves their coverage
to its fixture provider (#147, part (b)). The cases of these files that test
only the provider maps are in `tests/test_statement_maps.py`: each map must
equal the map of the Meridian provider and have the contract labels.

`server/platform/sharadar_key.py` and `src/hooks/useSharadarStatus.js` are
not in the tree. The key check of `sharadar_key.py` is the reference of the
`validateCredentials` operation. The other parts of the file are host code
that Meridian PR-3c replaces. The plug-in has no user interface, so the hook
stays with Meridian. [Host integration](docs/host-integration.md) gives the
host mapping of its status values.

## Install and test

You need Python 3.12 and Node 22 or later.

```sh
make install      # creates .venv and installs requirements-dev.lock with hashes
make hooks        # uses the Git hooks of tools/hooks in this clone
make check        # privacy check, Ruff, pytest and the feature-map check
make secrets      # gitleaks on the Git history
make dependencies # osv-scanner on both lock files
```

The tests make no network connection and use only synthetic data.
`tests/test_parity.py` runs the Meridian provider of `tests/reference/` and
the plug-in over HTTP on the same synthetic tables, and compares each value.
[Host integration](docs/host-integration.md) gives the operation mapping and
the rules that the Meridian host adapter must follow.

## Privacy checks

This repository is public. `tools/privacy.sh` has three modes:

- `index` (`make lint`, the `lint` CI job and the `pre-commit` hook): it
  fails when the Git index holds a symbolic link, or a home folder path such
  as `/home/<user>` or `/Users/<user>` in a file or a path name. It prints
  only the file names.
- `commits <range>` (the `pre-push` hook and the `secrets` CI job on a pull
  request): it does the same for each commit, and also fails on a home
  folder path in a message, and on an author, committer or `-by:` trailer
  address that is not a GitHub noreply address.
- `tag <object>` (the `pre-push` hook): it checks the tagger address and the
  message of an annotated tag.

The gitleaks config has the same home-path rule, so the `secrets` CI job
checks each commit of the history.

Run `make hooks` once in each clone. The `pre-commit` hook runs the index
check and gitleaks on the staged changes. The `pre-push` hook reads the
`main` of the remote with `git ls-remote`, and rejects a ref whose history
does not start at its root commit. Then it runs the tag check, the commit
check and gitleaks on each commit that the push sends, and gitleaks on
their messages and on the tag message. For a new ref it checks the full
history. The hooks keep the
pinned gitleaks download in the Git folder. GitHub makes a pushed commit
public at once, so the `pre-push` hook is the last check before
publication. The CI jobs are the gate for `main`.

## Build and prove an archive

`VERSION=<version> make archive` builds the archive of this platform in
`dist/` and runs two proofs on it:

1. `release/launch_proof.py` extracts the archive, checks `SHA256SUMS`, and
   starts the frozen payload with a handshake. It checks the ready line, the
   manifest, the health, the five data operations on a synthetic raw cache,
   the lock and the exit on stdin end of file. Each other request goes to a
   proxy on a loopback port of the proof that closes each connection at once,
   so the proof makes no Sharadar call.
2. `release/host_acceptance.py` installs the archive with the Meridian CLI of
   `tests/reference/server/plugins/` on a pseudo-terminal and starts it with
   the Meridian `PluginHost`. It checks the state `ready`, the
   `credential_missing` problem without a key, and that the host sends a
   stored key.

The `archive-linux` CI job runs it in a Debian 12 Python image on
each pull request. The Linux archive then needs glibc 2.36 or later. The macOS
archive needs macOS 14 or later, because the NumPy wheel of the lock needs it.
The build excludes `pytest` and `setuptools`, which NumPy and pandas import
only when they are present, so the payload holds no module of these two
packages. It also excludes `readline`, so the Linux archive holds no GPL
`libreadline`.

Each archive holds the license notices of what it bundles:

- `LICENSE`: the AGPL-3.0 of this plug-in.
- `licenses/<package>/<path>`: each license file of each installed package
  of `requirements.lock` and of PyInstaller (for its bootloader), at its path
  in the package. A package whose lock marker excludes the platform is not in
  the archive.
- `licenses/python/`: the CPython license document
  `notices/cpython-<version>/license.rst`, and the `LICENSE.txt` of the
  Python of the Debian image, or for a uv Python the notices of its build in
  `notices/python-build-standalone/<build>-<target>/`.
- `licenses/debian/<package>/copyright`: the Debian copyright file of each
  bundled system library that no Python package holds, and in
  `licenses/debian/common-licenses/` each license text that these files name.

The build stops when a notice is missing. The Linux archive holds the NumPy
copies of OpenBLAS, `libgfortran` and `libquadmath` (LGPL-2.1-or-later). The
archive keeps them unchanged and as separate files, and the NumPy notice in
`licenses/numpy/` gives their source location. The archive also holds the
LGPL-2.1 text as `licenses/LGPL-2.1.txt`.

After a change of the Python patch, add `Doc/license.rst` of that CPython tag
as `notices/cpython-<version>/license.rst`. After a change of the uv Python
build, copy the `python/licenses/` folder of the matching
`cpython-<version>+<build>-<target>-pgo+lto-full.tar.zst` of
python-build-standalone into `notices/`.

## Release

Push a tag `v<version>` on a commit of `main`, for example `v1.0.0-rc.2`. The
workflow does not check that the commit is on `main`, so this rule is a step
of the release procedure. The `release` workflow runs Ruff and the tests, and
builds and proves each archive:

- Linux x64: in the Debian 12 Python image on GitHub-hosted `ubuntu-26.04`.
- macOS ARM64: on GitHub-hosted `macos-15`.

Then the workflow creates a draft GitHub release with both archives and their
SHA-256 digests. A version with a `-` part is a prerelease. Download both
archives of the draft, check their digests and their `licenses/` folder, and
then publish the draft:

```sh
gh release edit v<version> -R lgmr-dev/meridian-provider-sharadar --draft=false
```
 A manual run
builds and proves both archives and creates no release. A pull request that
changes `release/`, `notices/`, the workflow, `Makefile` or a lock file does
the same.

The macOS job does not use the Python of the runner, and it installs nothing
in the home folder of the runner user. It makes a new folder in
`/private/tmp`, downloads uv 0.13.9 into it and checks the SHA-256 of uv. Then
it installs the uv Python 3.12.16 and puts the uv and PyInstaller caches in
the same folder. The job turns off the pip cache, and the last step removes
the folder. The checkout, `.venv` and `dist/` stay in the runner workspace.

The archive keeps the path of the uv Python in its build data, so that path
must not contain a user name. The uv Python needs macOS 11 or later, so the
NumPy wheel sets the macOS 14 limit.

Both archives bundle CPython 3.12.16. The Linux job gets it from the digest of
the Debian Python image, and the macOS job from the `uv python install` line.
The `linux` job fails when its Python is not 3.12.16, so change the two pins
together.

## Install a release in Meridian

1. Quit Meridian.
2. Download the archive of your platform:

   ```sh
   gh release download v<version> -R lgmr-dev/meridian-provider-sharadar -p '*aarch64-apple-darwin.tar.gz'
   ```

3. Read the digests of the release:

   ```sh
   gh release view v<version> -R lgmr-dev/meridian-provider-sharadar
   gh api repos/lgmr-dev/meridian-provider-sharadar/releases/tags/v<version> \
     --jq '.assets[] | .name + " " + .uploader.login + " " + .digest'
   shasum -a 256 meridian-provider-sharadar-<version>-aarch64-apple-darwin.tar.gz
   ```

   The release notes give the digest as 64 hexadecimal characters. The API
   gives the digest that GitHub calculated at the upload, as `sha256:<hex>`.
   The two values and the `shasum` value must be equal. Use the value without
   `sha256:` in step 5.

4. Check the provenance. The releases have no GitHub artifact attestations.
   Run these checks:

   ```sh
   gh api repos/lgmr-dev/meridian-provider-sharadar/commits/v<version> --jq .sha
   gh api repos/lgmr-dev/meridian-provider-sharadar/compare/main...v<version> --jq .status
   gh run list -R lgmr-dev/meridian-provider-sharadar --workflow release.yml \
     --branch v<version> --json databaseId,event,conclusion,headSha
   gh run view <databaseId> -R lgmr-dev/meridian-provider-sharadar --log | grep <digest>
   ```

   - The uploader of each asset in step 3 is `github-actions[bot]`.
   - The compare status is `identical` or `behind`, so the tag commit is on
     `main`.
   - One run has the event `push`, the conclusion `success` and the tag
     commit as `headSha`.
   - The log of that run prints the digest of your archive.

   These checks show that a `release` run built an archive with this digest
   from a commit on `main`. They do not prove which job uploaded the asset.
   Another workflow with write permission also uploads as
   `github-actions[bot]`, and a user with write access can replace an asset.
   GitHub keeps the run logs for 90 days. After that time, the log check is
   not possible, but the other checks still apply.

5. Install the archive and type `yes` when the command asks:

   ```sh
   /Applications/Meridian.app/Contents/MacOS/meridian-backend plugin install meridian-provider-sharadar-<version>-aarch64-apple-darwin.tar.gz --sha256 <digest>
   ```

   From a Meridian checkout, use `python -m server.plugins install <archive> --sha256 <digest>`.
6. Start Meridian.

## Change a dependency

1. Edit `requirements.txt` or `requirements-dev.txt`.
2. Run `make lock`. It needs `uv`.
3. Run `make check` and `make dependencies`.

`requirements.txt` pins each runtime package to its version in the Meridian
`requirements.lock` at the source commit, so that the numbers stay the same.

Ruff uses `target-version = "py310"`, as Meridian does, so that the provider
code stays as Meridian wrote it.

## License

Copyright (c) Luis Ribeiro.

This plug-in is under the GNU Affero General Public License, version 3 only
(`LICENSE`). The files in `contracts/` are copies of the Meridian plug-in
contracts and are under the Apache License, version 2.0
(`contracts/LICENSE`).
