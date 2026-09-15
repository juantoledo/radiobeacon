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

# Shared virtualenv every package/script installs into and runs from — one
# venv instead of one per package. data-adapters/dispatcher/actions/ui/
# beacon editable-install each other (-e ../data-adapters, ui also -e
# ../dispatcher) and share no conflicting pins, so building and filling 5
# separate venvs was pure redundant work — the dominant cost of a fresh
# clone's first start. Resolved from lib.sh's own location (like RUN_DIR
# below), not the caller's cwd, so it's the same directory whether sourced
# as ../lib.sh (a package's own start.sh, cwd = package dir) or ./lib.sh
# (repo root's own start.sh / stop.sh, cwd = repo root).
VENV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.venv"

# Serializes access to $VENV_DIR across processes: root start.sh backgrounds
# all five packages' start.sh scripts, and each of those still calls
# setup_venv on its way in (so it keeps working when run standalone) — with
# one shared venv, that's up to five `pip install` invocations racing
# against the *same* site-packages directory at once, which pip has no
# built-in protection against (seen in testing as spurious "No existe el
# archivo o el directorio" errors on the .pth / dist-info files pip itself
# was mid-write on). `mkdir` is used as the lock, not flock, since flock
# isn't available on macOS (no util-linux there) and this codebase
# otherwise stays portable to it (see run_with_timeout's gtimeout handling
# below) — mkdir's EEXIST-on-collision is atomic on any POSIX filesystem.
# A generous wait: a genuinely fresh install (no cached wheels) can take
# minutes, and this is what a concurrent caller may be waiting out.
_venv_lock_acquire() {
  local lock_dir="$VENV_DIR.lock" waited=0
  until mkdir "$lock_dir" 2>/dev/null; do
    sleep 1
    waited=$((waited + 1))
    if [ "$waited" -ge 600 ]; then
      echo "error: timed out waiting for another venv setup to finish -- if none is actually running, remove the stale lock with: rmdir $lock_dir" >&2
      exit 1
    fi
  done
  # EXIT (not RETURN): _ensure_venv_dir below exits directly on failure
  # rather than returning, which a RETURN trap would miss and leave the
  # lock stuck. Safe to leave set past a normal return too -- nothing
  # between here and the next exec (which discards it) reruns this lock.
  trap 'rmdir "'"$lock_dir"'" 2>/dev/null' EXIT
}

_venv_lock_release() {
  rmdir "$VENV_DIR.lock" 2>/dev/null
  trap - EXIT
}

# Creates $VENV_DIR if missing, or recreates it if it's stale (built with a
# Python older than $_MIN_PY_MINOR). Fails with an actionable message
# instead of python3 -m venv's own cryptic "ensurepip is not available"
# error when the venv module isn't installed (e.g. Debian/Ubuntu's python3
# package splits it out into python3-venv) — the actual failure mode fresh
# clones hit most often. Shared by setup_venv and setup_venv_all below;
# nothing else calls it directly.
_ensure_venv_dir() {
  local python_bin
  if ! python_bin="$(_find_python3)"; then
    echo "error: no Python 3.${_MIN_PY_MINOR}+ found on PATH — install one (e.g. 'brew install python@3.12' on macOS) or put it ahead of an older python3 on PATH." >&2
    exit 1
  fi

  if [ -d "$VENV_DIR" ]; then
    local venv_minor
    venv_minor=$("$VENV_DIR/bin/python3" -c 'import sys; print(sys.version_info[1])' 2>/dev/null) || venv_minor=0
    if [ "$venv_minor" -lt "$_MIN_PY_MINOR" ]; then
      echo "existing .venv was built with Python 3.${venv_minor} (< 3.${_MIN_PY_MINOR} required) — recreating with $("$python_bin" --version 2>&1)" >&2
      rm -rf "$VENV_DIR"
    fi
  fi

  if [ ! -d "$VENV_DIR" ]; then
    local venv_err
    if ! venv_err=$("$python_bin" -m venv "$VENV_DIR" 2>&1); then
      echo "$venv_err" >&2
      echo "error: could not create .venv — on Debian/Ubuntu, install the venv module: sudo apt install python3-venv" >&2
      exit 1
    fi
  fi
}

# Ensures the shared venv exists, then installs ./requirements.txt
# (relative to the caller's cwd) into it. Called by each package's own
# start.sh, and by sources.sh/policies.sh/override_item.sh, so every one of
# them keeps working standalone — against the one shared venv — per this
# repo's "run just one piece" contract (see root start.sh's own comment).
#
# No-ops if RADIOBEACON_VENV_READY is set: root start.sh's setup_venv_all
# already installed every package's requirements (including this one, as
# part of its merged pass) and exports that var before backgrounding each
# package's own start.sh, which is what calls this. Without this check,
# every `./start.sh` run pays for a second, fully redundant `pip install`
# per package — five of them, serialized against each other by
# _venv_lock_acquire below since they all race the same site-packages dir
# — on top of the one merged install that already covered them.
setup_venv() {
  [ -n "${RADIOBEACON_VENV_READY:-}" ] && return 0
  _venv_lock_acquire
  _ensure_venv_dir
  "$VENV_DIR/bin/pip" install -q -r requirements.txt
  _venv_lock_release
}

# Ensures the shared venv exists, then installs every requirements file
# named in "$@" (paths relative to the caller's cwd, e.g.
# "data-adapters/requirements.txt") in a single pip invocation instead of
# one per file. Used only by the repo root's own start.sh, to bring up the
# whole stack with one pip pass instead of setup_venv's five.
#
# Can't just pip install -r one -r another: each file's own "-e ../foo"
# lines are written relative to *that package's own directory* (matching
# what setup_venv above uses, cwd == the caller's own dir), but pip
# resolves a requirements file's "-e <relative path>" lines against the
# cwd pip was invoked from — not that file's own directory, confirmed
# empirically (this holds even when the file is reached via a nested -r,
# not just a top-level one) — so handing pip several such files at once
# from the repo root breaks every "-e ../data-adapters"-style line. Worked
# around by rewriting each file's own "-e <path>" line to an absolute path
# (resolved against *that file's* directory, before pip ever sees it) into
# one merged temp file; every other line (plain package specs, comments,
# blanks) passes through untouched — so no package's requirements.txt
# needs to change to support this.
setup_venv_all() {
  _venv_lock_acquire
  _ensure_venv_dir
  local merged
  merged="$(mktemp)"
  trap 'rm -f "$merged"' RETURN

  local f dir line target
  for f in "$@"; do
    dir="$(cd "$(dirname "$f")" && pwd)"
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in
        -e\ *)
          target="${line#-e }"
          case "$target" in
            /*) ;; # already absolute
            *) target="$(cd "$dir/$target" && pwd)" ;;
          esac
          echo "-e $target"
          ;;
        *)
          echo "$line"
          ;;
      esac
    done < "$f"
  done > "$merged"

  "$VENV_DIR/bin/pip" install -q -r "$merged"
  _venv_lock_release

  # Lets setup_venv (above) skip its own redundant install: root start.sh
  # backgrounds each package's own start.sh after calling this, and those
  # child processes inherit this export, so their setup_venv sees it set.
  export RADIOBEACON_VENV_READY=1
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
