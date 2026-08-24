#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source ../lib.sh

load_env_file ../.env
setup_venv

_default_piper_model="storage/piper_voices/es_MX-claude-high.onnx"
if [ "${BEACON_TTS_ENGINE:-piper}" = "piper" ] \
    && [ "${BEACON_TTS_PIPER_MODEL:-$_default_piper_model}" = "$_default_piper_model" ]; then
  ensure_default_piper_voice "$(dirname "$_default_piper_model")"
fi

# exec replaces this shell process with the Python one (instead of running
# it as a child) so SIGINT/SIGTERM sent to this script reach the
# long-running TDMA loop directly — see src/beacon/__main__.py.
export PYTHONPATH=src
# voice.py shells out to the bare "piper" command (BEACON_TTS_PIPER_BINARY)
# rather than an absolute path -- pip installs its console-script entry
# point into .venv/bin, but running .venv/bin/python3 directly (below)
# never adds that directory to PATH the way `source .venv/bin/activate`
# would, so it'd otherwise be unresolvable even though it's right there.
export PATH="$PWD/.venv/bin:$PATH"
guard_single_instance beacon beacon
exec .venv/bin/python3 -m beacon
