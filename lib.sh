# Shared setup helpers, sourced by every package's start.sh and by the
# repo root's own ./start.sh — kept in one place instead of copy-pasted
# per package, where the copies could drift out of sync with each other.
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

# Downloads the default Piper neural-TTS voice model (BEACON_TTS_ENGINE=
# piper's default engine) into $1 if it isn't already there. Keeps a
# ~60MB binary out of git (see beacon/storage/piper_voices/ in
# .gitignore) while still giving a fresh clone working, natural-sounding
# TTS with zero manual setup -- only this one bundled default voice is
# auto-fetched; a custom BEACON_TTS_PIPER_MODEL pointing elsewhere is the
# caller's own responsibility to provide. Never fails the caller: a
# missing/failed download just leaves piper erroring at runtime (already
# handled there -- beacon/src/beacon/voice.py logs and returns False),
# same as before this existed.
ensure_default_piper_voice() {
  local voice_dir="$1"
  local voice_name="es_MX-claude-high"
  local model_file="$voice_dir/$voice_name.onnx"
  local config_file="$voice_dir/$voice_name.onnx.json"
  [ -f "$model_file" ] && [ -f "$config_file" ] && return 0

  local base_url="https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_MX/claude/high"
  echo "beacon: downloading default piper voice model ($voice_name, ~60MB, one-time)..." >&2
  mkdir -p "$voice_dir"
  if ! curl -fsSL -o "$model_file.tmp" "$base_url/$voice_name.onnx?download=true" \
      || ! curl -fsSL -o "$config_file.tmp" "$base_url/$voice_name.onnx.json?download=true"; then
    echo "warning: could not download the default piper voice model -- BEACON_TTS_ENGINE=piper will fail to synthesize speech until $model_file is provided manually. Set BEACON_TTS_ENGINE=espeak instead if offline." >&2
    rm -f "$model_file.tmp" "$config_file.tmp"
    return 0
  fi
  mv "$model_file.tmp" "$model_file"
  mv "$config_file.tmp" "$config_file"
}

# Shared PID-file directory for guard_single_instance/service_is_running
# below, resolved from lib.sh's own location (not the caller's cwd) so it
# resolves to the same repo-root run/ dir whether sourced as ../lib.sh
# (each package's start.sh, cwd = package dir) or ./lib.sh (the repo
# root's own start.sh / stop.sh, cwd = repo root).
RUN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/run"

# True (0) if $1's PID file names a live process that still looks like our
# `-m $2` invocation. Always checked live (kill -0, plus a /proc/$pid/cmdline
# match where /proc exists) rather than trusted from the file's mere
# presence -- makes the mechanism self-healing against a stale PID file left
# behind by a crash, kill -9, or a failure before exec was ever reached (none
# of which get a chance to clean up after themselves; exec also discards any
# trap the launching shell had set). The cmdline check is skipped, not
# failed, where /proc doesn't exist (e.g. macOS) -- kill -0 alone is still a
# real check, just weaker against the rare case of the pid being reused by
# an unrelated process.
service_is_running() {
  local pid_file="$RUN_DIR/$1.pid" pid
  [ -f "$pid_file" ] || return 1
  pid="$(cat "$pid_file" 2>/dev/null)"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  if [ -r "/proc/$pid/cmdline" ]; then
    tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q -- "-m $2" || return 1
  fi
  return 0
}

# Refuses to continue if $1 (module $2) is already running -- called by
# each start.sh right before exec. Prevents the exact bug class this
# existed to fix: a second instance launched into a stack that already has
# one running, silently fighting the first over shared state (MQTT client
# id, in-memory queues, ...) with no record anywhere of the duplicate ever
# existing. Otherwise records this process's pid (== the pid the following
# exec keeps, since exec replaces the image in place rather than forking)
# so ./stop.sh can find and stop it later, from any shell.
guard_single_instance() {
  if service_is_running "$1" "$2"; then
    echo "error: $1 is already running (pid $(cat "$RUN_DIR/$1.pid")) -- stop it with ./stop.sh $1 (or ./stop.sh for the whole stack) before starting another." >&2
    exit 1
  fi
  mkdir -p "$RUN_DIR"
  echo "$$" > "$RUN_DIR/$1.pid"
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
