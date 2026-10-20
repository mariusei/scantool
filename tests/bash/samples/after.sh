#!/usr/bin/env bash
# Deploy the release artifacts to the staging host.
#
# Usage: basic.sh [--dry-run] <version>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly DEFAULT_HOST="releases.example.com"
export LOG_LEVEL="${LOG_LEVEL:-info}"

# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

# Print the usage text and exit with the given status.
usage() {
  echo "Usage: basic.sh [--dry-run] <version>"
  exit "${1:-0}"
}

# Copy one artifact, retrying on failure.
_upload_artifact() {
  local file="$1"
  local attempt
  for attempt in 1 2 3; do
    if scp "$file" "$DEFAULT_HOST:/srv/releases/"; then
      log_info "uploaded $file"
      return 0
    fi
    log_warn "attempt $attempt failed for $file"
  done
  return 1
}

function deploy {
  local version="$1"
  cleanup() {
    rm -rf "$workdir"
    log_info "removed $workdir"
  }
  trap 'cleanup' EXIT
  workdir="$(mktemp -d)"
  build_artifacts "$version" "$workdir"
  for artifact in "$workdir"/*; do
    _upload_artifact "$artifact" || exit 1
  done
}

# Report which host a release goes to.
target_host() {
  echo "$DEFAULT_HOST"
}

main() {
  [[ $# -ge 1 ]] || usage 1
  deploy "$1"
}

if [[ "${1:-}" == "--help" ]]; then
  usage
fi

main "$@"
