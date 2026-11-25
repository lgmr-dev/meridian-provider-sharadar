# Meridian plug-in contracts

The files in this directory are under the Apache License 2.0 (`LICENSE`).
They define how the Meridian host and a plug-in work together. A plug-in is a
separate process that serves HTTP on the loopback interface.

| Family           | Files                            | Content                                                               |
| ---------------- | -------------------------------- | --------------------------------------------------------------------- |
| `plugin-runtime` | `plugin-runtime/v1/openapi.json` | Handshake, ready line, manifest, health, `plugin.json`, problems      |
| `tax`            | `tax/v1/openapi.json`            | The 15 tax operations and the typed presentation that the host shows |
| `market-data`    | `market-data/v1/openapi.json`    | The 7 market-data operations and the table envelope                  |

The design is in issue #147 of the Meridian repository.

## Status and revision

Each family has `v1/contract.json` with its status and its revision id.

- Status `draft`: the contract text can change. The host accepts a plug-in
  only when the `contract_revision` of the plug-in equals the revision id of
  the host for that family.
- Status `frozen`: the major version must be equal, and the minor version
  must be at least the host minimum. A frozen contract keeps the revision id
  of its last draft.

The revision id is the SHA-256 (lowercase hex) of the contract text with LF
line ends:

- `plugin-runtime`: the hash of `plugin-runtime/v1/openapi.json`.
- `tax` and `market-data`: the hash of `plugin-runtime/v1/openapi.json`
  followed by `<family>/v1/openapi.json`, because a plug-in implements both
  files.

The status is not in the hashed text, so the freeze does not change the id.
The examples in `examples/` are not part of the hashed text.

## Trust

A plug-in runs with the rights of the user. The bearer token and the loopback
interface control the session. They are not a sandbox. The owner confirms
each registry entry on a terminal before the host starts it.

## Artifact

A release asset is `<id>-<version>-<target>.tar.gz`. The targets are
`aarch64-apple-darwin` and `x86_64-unknown-linux-gnu`. The archive members are
relative to the version directory:

- `plugin.json` (schema `PluginJson` of the runtime contract).
- The OpenAPI files that the plug-in implements.
- The payload, for example a PyInstaller one-folder build.
- `SHA256SUMS`.

`SHA256SUMS` lists each file and each link of the version directory, except
itself, in byte order of the path. A file line is the lowercase SHA-256, two
spaces and the relative path. A link line is `link:`, the link target, two
spaces and the relative path. A link must stay inside the version directory.
The file ends with a newline. The registry stores the SHA-256 of
`SHA256SUMS` as `inventory_sha256`.

## Registry entry hash

`entry_sha256` is the SHA-256 of the canonical JSON of the registry entry
without `confirmed`: sorted keys, the separators `,` and `:` with no spaces,
and ASCII escapes for other characters.
