#!/usr/bin/env bash
set -euo pipefail

gate="${1:?usage: tools/security.sh secrets|staged|outgoing|text|dependencies}"
cd "$(dirname "$0")/.."
scratch="$(mktemp -d)"
cache="$(git rev-parse --git-common-dir)/security-downloads"
mkdir -p "$cache"
trap 'rm -rf "$scratch"' EXIT

case "$(uname -s):$(uname -m)" in
  Linux:x86_64)
    gitleaks_asset="gitleaks_8.30.1_linux_x64.tar.gz"
    gitleaks_sha="551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
    osv_asset="osv-scanner_linux_amd64"
    osv_sha="bc98e15319ed0d515e3f9235287ba53cdc5535d576d24fd573978ecfe9ab92dc"
    ;;
  Darwin:arm64)
    gitleaks_asset="gitleaks_8.30.1_darwin_arm64.tar.gz"
    gitleaks_sha="b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5"
    osv_asset="osv-scanner_darwin_arm64"
    osv_sha="a8cd6507b06239f463a7642430cfd2d154882f150f6e30cdc0653e28dfc34216"
    ;;
  *)
    echo "security: unsupported host $(uname -s) $(uname -m)" >&2
    exit 1
    ;;
esac

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

fetch() {
  local url="$1" sha="$2" out="$3" cached
  cached="$cache/$(basename "$url")"
  if [[ ! -f "$cached" || "$(sha256 "$cached")" != "$sha" ]]; then
    curl -fsSL "$url" -o "$cached"
    [[ "$(sha256 "$cached")" == "$sha" ]] || { rm -f "$cached"; echo "security: checksum mismatch for $(basename "$url")" >&2; exit 1; }
  fi
  cp "$cached" "$out"
}

run_gitleaks() {
  fetch "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/$gitleaks_asset" "$gitleaks_sha" "$scratch/gitleaks.tar.gz"
  tar -xzf "$scratch/gitleaks.tar.gz" -C "$scratch" gitleaks
  "$scratch/gitleaks" "$@" --config .gitleaks.toml --redact --no-banner
}

case "$gate" in
  secrets)
    run_gitleaks git .
    ;;
  staged)
    run_gitleaks git . --pre-commit --staged
    ;;
  outgoing)
    run_gitleaks git . --log-opts="${2:?usage: tools/security.sh outgoing <git log range>}"
    ;;
  text)
    run_gitleaks dir "${2:?usage: tools/security.sh text <folder>}"
    ;;
  dependencies)
    fetch "https://github.com/google/osv-scanner/releases/download/v2.3.8/$osv_asset" "$osv_sha" "$scratch/osv-scanner"
    chmod +x "$scratch/osv-scanner"
    "$scratch/osv-scanner" scan \
      --lockfile="requirements.txt:requirements.lock" \
      --lockfile="requirements.txt:requirements-dev.lock"
    ;;
  *)
    echo "security: unknown gate $gate" >&2
    exit 2
    ;;
esac
