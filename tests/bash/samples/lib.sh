# Logging and build helpers shared by the deploy scripts.

log_info() {
  printf '[info] %s\n' "$*"
}

log_warn() {
  printf '[warn] %s\n' "$*" >&2
}

build_artifacts() {
  local version="$1" out="$2"
  log_info "building $version"
  bash "$(dirname "${BASH_SOURCE[0]}")/build.sh" "$version" "$out"
}
