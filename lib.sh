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

# Minimum Python minor version this codebase needs: several packages use
# bare `X | None` type-hint syntax (PEP 604) in function/class annotations
# without `from __future__ import annotations`, which Python evaluates at
# def/class time and which raises TypeError below 3.10.
_MIN_PY_MINOR=10

# Finds the newest installed python3.<N> (N >= $_MIN_PY_MINOR) on PATH,
# falling back to plain `python3` if it already satisfies the minimum.
# Prefers versioned binaries over `python3` itself because on macOS
# `python3` often resolves to the Xcode Command Line Tools' bundled
# interpreter — which sits ahead of Homebrew's on a stock PATH and can be
# years older (3.9 while 3.12+ is installed and just shadowed) — so
# trusting whatever `python3` happens to be silently picks the wrong one.
_find_python3() {
  local candidate minor best="" best_minor=-1
  while IFS= read -r candidate; do
    minor="${candidate#python3.}"
    case "$minor" in '' | *[!0-9]*) continue ;; esac
    if [ "$minor" -ge "$_MIN_PY_MINOR" ] && [ "$minor" -gt "$best_minor" ]; then
      best="$candidate"
      best_minor="$minor"
    fi
  done < <(compgen -c python3. | sort -u)
  if [ -n "$best" ]; then
    command -v "$best"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    minor="$(python3 -c 'import sys; print(sys.version_info[1])' 2>/dev/null)" || return 1
    if [ "$minor" -ge "$_MIN_PY_MINOR" ]; then
      command -v python3
      return 0
    fi
  fi
  return 1
}

# Creates ./.venv (relative to the caller's cwd) if missing and installs
# ./requirements.txt into it. Fails with an actionable message instead of
# python3 -m venv's own cryptic "ensurepip is not available" error when
# the venv module isn't installed (e.g. Debian/Ubuntu's python3 package
# splits it out into python3-venv) — the actual failure mode fresh clones
# hit most often.
setup_venv() {
  local python_bin
  if ! python_bin="$(_find_python3)"; then
    echo "error: no Python 3.${_MIN_PY_MINOR}+ found on PATH — install one (e.g. 'brew install python@3.12' on macOS) or put it ahead of an older python3 on PATH." >&2
    exit 1
  fi

  if [ -d .venv ]; then
    local venv_minor
    venv_minor=$(.venv/bin/python3 -c 'import sys; print(sys.version_info[1])' 2>/dev/null) || venv_minor=0
    if [ "$venv_minor" -lt "$_MIN_PY_MINOR" ]; then
      echo "existing .venv was built with Python 3.${venv_minor} (< 3.${_MIN_PY_MINOR} required) — recreating with $("$python_bin" --version 2>&1)" >&2
      rm -rf .venv
    fi
  fi

  if [ ! -d .venv ]; then
    local venv_err
    if ! venv_err=$("$python_bin" -m venv .venv 2>&1); then
      echo "$venv_err" >&2
      echo "error: could not create .venv — on Debian/Ubuntu, install the venv module: sudo apt install python3-venv" >&2
      exit 1
    fi
  fi
  .venv/bin/pip install -q -r requirements.txt
}

# Runs "$@" with a wall-clock time limit, killing it if it overruns.
# GNU coreutils' `timeout` isn't there to reach for: macOS ships none
# (it's not part of BSD userland, and Homebrew's coreutils installs it
# as `gtimeout` to avoid clobbering anything, so plain `timeout` stays
# missing even with coreutils installed), and adding it as a required
# dependency just for this one call isn't worth it. Prefers a real
# `timeout`/`gtimeout` when one happens to be on PATH; otherwise runs
# the command in the background and races it against a sleeping
# watchdog subshell that SIGTERMs (then SIGKILLs, after a 2s grace
# period) whichever one loses.
run_with_timeout() {
  local secs="$1"
  shift
  local timeout_bin=""
  if command -v timeout >/dev/null 2>&1; then
    timeout_bin="timeout"
  elif command -v gtimeout >/dev/null 2>&1; then
    timeout_bin="gtimeout"
  fi
  if [ -n "$timeout_bin" ]; then
    "$timeout_bin" -k 2 "$secs" "$@"
    return $?
  fi

  "$@" &
  local cmd_pid=$!
  (
    sleep "$secs"
    kill -TERM "$cmd_pid" 2>/dev/null
    sleep 2
    kill -KILL "$cmd_pid" 2>/dev/null
  ) &
  local watchdog_pid=$!

  local status
  if wait "$cmd_pid" 2>/dev/null; then
    status=0
  else
    status=$?
  fi
  kill "$watchdog_pid" 2>/dev/null
  wait "$watchdog_pid" 2>/dev/null
  return "$status"
}
