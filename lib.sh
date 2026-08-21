# Shared setup helpers, sourced by every package's start.sh and by
# ./bootstrap.sh — kept in one place instead of copy-pasted per package,
# where the copies could drift out of sync with each other.
#
# Not executable / no shebang: this file is only ever `source`d, never
# run directly.

# Exports every KEY=VALUE line from $1 (if it exists) into the current
# shell. Parsed line-by-line instead of `source`d: a plain `source .env`
# runs each line through bash's normal command parsing, so any unquoted
# value containing spaces gets misread as a command invocation and
# aborts the script.
load_env_file() {
  local env_file="$1"
  [ -f "$env_file" ] || return 0
  local line key value
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ''|'#'*) continue ;;
    esac
    key="${line%%=*}"
    value="${line#*=}"
    if [[ "$value" == \"*\" && "$value" == *\" ]]; then
      value="${value#\"}"
      value="${value%\"}"
    elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
      value="${value#\'}"
      value="${value%\'}"
    fi
    export "$key=$value"
  done < "$env_file"
}

# Creates ./.venv (relative to the caller's cwd) if missing and installs
# ./requirements.txt into it. Fails with an actionable message instead of
# python3 -m venv's own cryptic "ensurepip is not available" error when
# the venv module isn't installed (e.g. Debian/Ubuntu's python3 package
# splits it out into python3-venv) — the actual failure mode fresh clones
# hit most often.
setup_venv() {
  if ! command -v python3 >/dev/null 2>&1; then
    echo "error: python3 not found — install Python 3 first." >&2
    exit 1
  fi
  if [ ! -d .venv ]; then
    local venv_err
    if ! venv_err=$(python3 -m venv .venv 2>&1); then
      echo "$venv_err" >&2
      echo "error: could not create .venv — on Debian/Ubuntu, install the venv module: sudo apt install python3-venv" >&2
      exit 1
    fi
  fi
  .venv/bin/pip install -q -r requirements.txt
}
