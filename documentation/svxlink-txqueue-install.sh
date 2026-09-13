#!/usr/bin/env bash
# Automates documentation/svxlink-txqueue-SETUP.md sections 2 (install
# SvxLink + Direwolf) and 4 (the svxlink-txqueue daemon + systemd unit), then
# wires radiobeacon's own .env so `beacon` can hand WAVs straight to the
# spool. Every stage below is tagged with the doc section it automates —
# read that doc for the why, this script just does the steps.
#
# Deliberately stays root-required and system-touching (apt, /etc,
# systemd) rather than folding into ./start.sh, which is scoped to the
# app stack only and needs no root/system changes to run.
#
# Idempotent: re-running is safe. Everything happens through --dry-run
# first if you want to preview it. See --help for every flag.
set -euo pipefail

# ---------------------------------------------------------------------------
# Colors — disabled automatically when stdout isn't a terminal, or when
# NO_COLOR is set (https://no-color.org), so piped/redirected output and
# any log capture stay clean.
# ---------------------------------------------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RESET=$'\033[0m'
  C_BOLD=$'\033[1m'
  C_DIM=$'\033[2m'
  C_RED=$'\033[31m'
  C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'
  C_CYAN=$'\033[36m'
else
  C_RESET='' C_BOLD='' C_DIM='' C_RED='' C_GREEN='' C_YELLOW='' C_CYAN=''
fi

log()  { echo "${C_CYAN}==>${C_RESET} $*"; }             # routine progress
ok()   { echo "${C_GREEN}==>${C_RESET} $*"; }             # confirmed success
dry()  { echo "${C_YELLOW}==> [dry-run]${C_RESET} $*"; }  # preview, no change made
warn() { echo "${C_YELLOW}warning:${C_RESET} $*" >&2; }
die()  { echo "${C_RED}error:${C_RESET} $*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---------------------------------------------------------------------------
# Defaults (all overridable via flags)
# ---------------------------------------------------------------------------
LOGIC_NAME="SimplexLogic"
CALLSIGN="NOCALL"
AUDIO_DEVICE=""            # "card,device", e.g. "1,0" — empty = detect/prompt/flag
PTT_TYPE="gpio"            # gpio | serial | none
PTT_PORT=""                # default resolved per PTT_TYPE
PTT_PIN="GPIO3"
SVXLINK_USER=""            # empty = auto-detect from /etc/default/svxlink
SVXLINK_CONF="/etc/svxlink/svxlink.conf"
SPOOL_ROOT="/var/spool/svxlink-tx"
COMMAND_PTY="/dev/shm/svxlink_simplex_ctrl"
DAEMON_SRC="$SCRIPT_DIR/svxlink-txqueue/svxlink-txqueue"
UNIT_SRC="$SCRIPT_DIR/svxlink-txqueue/svxlink-txqueue.service"
DAEMON_DST="/usr/local/bin/svxlink-txqueue"
UNIT_DST="/etc/systemd/system/svxlink-txqueue.service"
ENV_FILE="$REPO_ROOT/.env"

DRY_RUN=0
SKIP_SVXLINK_CONFIG=0
SKIP_ENV=0
SKIP_VERIFY=0
DO_UNINSTALL=0
PURGE_PACKAGES=0

usage() {
  cat <<'EOF'
Usage: sudo ./documentation/svxlink-txqueue-install.sh [options]

Automates documentation/svxlink-txqueue-SETUP.md sections 2 (install SvxLink +
Direwolf) and 4 (the svxlink-txqueue daemon + systemd unit), and wires
radiobeacon's own .env so `beacon` can hand WAVs straight to SvxLink.

Options:
  --logic-name=NAME       SvxLink logic section to use (default: SimplexLogic)     [doc §2.3/§4.1]
  --callsign=CALL         CALLSIGN= written into the logic block (default: NOCALL) [doc §2.3]
  --audio-device=C,D      ALSA card,device for Rx1/Tx1 — skips the interactive     [doc §2.2]
                          picker (a numbered menu of `aplay -l` devices)
  --ptt-type=TYPE         gpio | serial | none (default: gpio)                    [doc §2.3]
  --ptt-port=PATH         PTT device path (default: /dev/hidraw0 gpio, /dev/ttyUSB0 serial)
  --ptt-pin=PIN           GPIO pin for --ptt-type=gpio (default: GPIO3)
  --svxlink-user=NAME     Override the auto-detected SvxLink user
                          (normally read from /etc/default/svxlink's RUNASUSER)
  --skip-svxlink-config   Don't touch svxlink.conf at all — only install the daemon/unit [doc §4]
  --skip-env              Don't touch this repo's .env
  --skip-verify           Don't offer the end-of-run test transmission            [doc §6]
  --dry-run               Print what would happen; change nothing
  --uninstall             Reverse the daemon/unit/spool/COMMAND_PTY steps         [doc §8]
                          (packages are left alone — see --purge-packages)
  --purge-packages        With --uninstall, also `apt remove` svxlink-server and direwolf
  -h, --help              Show this help and exit

Every [doc §N] tag refers to the matching section in
documentation/svxlink-txqueue-SETUP.md.
EOF
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --logic-name=*) LOGIC_NAME="${1#*=}" ;;
    --callsign=*) CALLSIGN="${1#*=}" ;;
    --audio-device=*) AUDIO_DEVICE="${1#*=}" ;;
    --ptt-type=*) PTT_TYPE="${1#*=}" ;;
    --ptt-port=*) PTT_PORT="${1#*=}" ;;
    --ptt-pin=*) PTT_PIN="${1#*=}" ;;
    --svxlink-user=*) SVXLINK_USER="${1#*=}" ;;
    --skip-svxlink-config) SKIP_SVXLINK_CONFIG=1 ;;
    --skip-env) SKIP_ENV=1 ;;
    --skip-verify) SKIP_VERIFY=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --uninstall) DO_UNINSTALL=1 ;;
    --purge-packages) PURGE_PACKAGES=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
  shift
done

if [ "$(id -u)" -ne 0 ] && [ "$DRY_RUN" -eq 0 ]; then
  die "this installs system packages and edits system config — run it with sudo (or add --dry-run to preview without root)"
fi

# ---------------------------------------------------------------------------
# svxlink.conf.<UTC-stamp>.bak sibling before any in-place edit, matching
# ui/src/ui/routers/rf_conf.py's _write_conf convention — an operator who's
# used the dashboard's own raw editor recognizes the pattern.
# ---------------------------------------------------------------------------
backup_file() {
  local f="$1"
  [ -f "$f" ] || return 0
  local stamp bak
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  bak="${f}.${stamp}.bak"
  if [ "$DRY_RUN" -eq 1 ]; then
    dry "would back up $f -> $bak"
  else
    cp -p "$f" "$bak"
    ok "backed up $f -> $bak"
  fi
}

# `mktemp`'s default mode is 0600 (owner-only) — a plain `mv "$tmp" "$dest"`
# after an awk rewrite silently carries that mode onto dest, locking out
# any non-root reader (e.g. the svxlink user reading svxlink.conf, which
# needs to stay 0644). Hit this for real during testing: SvxLink came back
# up as "Could not open configuration file" after a COMMAND_PTY edit.
# Restore dest's existing mode/owner onto tmp before the swap.
atomic_replace() {
  local tmp="$1" dest="$2"
  if [ -e "$dest" ]; then
    chmod --reference="$dest" "$tmp"
    chown --reference="$dest" "$tmp" 2>/dev/null || true
  fi
  mv "$tmp" "$dest"
}

# Idempotent KEY=VALUE upsert against a .env-style file. No existing helper
# in the repo to reuse — settings normally live in the SQLite settings
# table, not .env text (confirmed: only lib.sh's load_env_file reads .env,
# nothing writes it).
set_env_var() {
  local key="$1" value="$2" file="$3"
  if [ "$DRY_RUN" -eq 1 ]; then
    if grep -qE "^${key}=" "$file" 2>/dev/null; then
      dry "would set ${key}=${value} in $file (replacing existing value)"
    else
      dry "would append ${key}=${value} to $file"
    fi
    return 0
  fi
  if grep -qE "^${key}=" "$file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

# ---------------------------------------------------------------------------
# §2.1 — packages
# ---------------------------------------------------------------------------
stage_packages() {
  log "[2.1] installing packages"
  if [ "$DRY_RUN" -eq 1 ]; then
    dry "would run: apt-get update && apt-get install -y svxlink-server direwolf alsa-utils"
    return 0
  fi
  apt-get update
  apt-get install -y svxlink-server direwolf alsa-utils
}

# ---------------------------------------------------------------------------
# SvxLink user — the doc previously said `systemctl show svxlink -p User
# --value`, which returns empty on the real svxlink-server package: its
# unit has no User=, it runs `--runasuser=${RUNASUSER}` via
# EnvironmentFile=/etc/default/svxlink instead. Read that directly.
# ---------------------------------------------------------------------------
detect_svxlink_user() {
  if [ -n "$SVXLINK_USER" ]; then
    log "using --svxlink-user=$SVXLINK_USER"
    return 0
  fi
  if [ -f /etc/default/svxlink ]; then
    SVXLINK_USER="$(grep -E '^RUNASUSER=' /etc/default/svxlink | tail -n1 | cut -d= -f2)"
  fi
  [ -n "$SVXLINK_USER" ] || die "could not detect the SvxLink user from /etc/default/svxlink; pass --svxlink-user=<name>"
  log "detected SvxLink user: $SVXLINK_USER"
}

# ---------------------------------------------------------------------------
# §2.2 — audio device: a numbered menu built from `aplay -l`, so picking the
# radio interface is "type a number" instead of "read raw ALSA output and
# hand-assemble card,device yourself". The menu is built from the playback
# (TX) side because that's what AUDIO_DEV actually gets set to for both
# Rx1/Tx1 below — a combined interface like the R1 2023 exposes the same
# card on capture and playback, so one list is enough.
#
# `LC_ALL=C` forces English output regardless of the host's locale — ALSA
# utils translate their "card"/"device" labels (confirmed on this host,
# which defaults to Spanish "tarjeta"/"dispositivo"), and a locale-specific
# parser would silently find nothing on any non-English system.
# ---------------------------------------------------------------------------

# Parses `aplay -l` lines like:
#   card 1: Pebbles [JBL Pebbles], device 0: USB Audio [USB Audio]
# into "card,device|short name — device name". Deliberately greedy (.*)
# rather than a bracket-aware pattern: some cards' own long names contain
# nested brackets (e.g. "Audigy2 [SB Audigy 5/Rx [SB1550]]"), which breaks
# a naive non-nesting bracket match — greedy capture up to the one literal
# ", device N: " on the line sidesteps that instead of trying to parse
# brackets correctly.
list_playback_devices() {
  LC_ALL=C aplay -l 2>/dev/null | sed -nE 's/^card ([0-9]+): (.*), device ([0-9]+): (.*)$/\1,\3|\2 — \4/p'
}

detect_audio_device() {
  [ "$SKIP_SVXLINK_CONFIG" -eq 1 ] && return 0
  if [ -n "$AUDIO_DEVICE" ]; then
    log "using --audio-device=$AUDIO_DEVICE"
    return 0
  fi
  log "[2.2] detecting sound cards"

  if [ ! -t 0 ] || [ ! -t 1 ]; then
    die "non-interactive session: pass --audio-device=<card,device> (run 'aplay -l' to list them)"
  fi

  local entries=() e dev desc labels=() values=()
  while IFS= read -r e; do
    entries+=("$e")
  done < <(list_playback_devices)

  if [ "${#entries[@]}" -eq 0 ]; then
    warn "no ALSA playback devices detected by 'aplay -l' — falling back to manual entry"
    echo "-- capture (RX), raw --"; arecord -l || true
    echo "-- playback (TX), raw --"; aplay -l || true
    read -r -p "Enter the ALSA card,device for the radio interface (e.g. 1,0): " AUDIO_DEVICE || true
    [ -n "$AUDIO_DEVICE" ] || die "no audio device given"
    return 0
  fi

  for e in "${entries[@]}"; do
    dev="${e%%|*}"
    desc="${e#*|}"
    values+=("$dev")
    labels+=("$desc  ${C_DIM}(card,device: $dev)${C_RESET}")
  done

  echo "${C_BOLD}Playback devices detected:${C_RESET}"
  local choice
  local PS3="${C_BOLD}Select the radio interface [1-$(( ${#values[@]} + 1 ))]: ${C_RESET}"
  select choice in "${labels[@]}" "enter card,device manually"; do
    if [ "${REPLY:-}" = "$(( ${#values[@]} + 1 ))" ]; then
      read -r -p "Enter the ALSA card,device (e.g. 1,0): " AUDIO_DEVICE || true
      if [ -z "$AUDIO_DEVICE" ]; then
        echo "no value given, try again"
        continue
      fi
      break
    elif [[ "${REPLY:-}" =~ ^[0-9]+$ ]] && [ "$REPLY" -ge 1 ] && [ "$REPLY" -le "${#values[@]}" ]; then
      AUDIO_DEVICE="${values[$((REPLY-1))]}"
      break
    else
      echo "invalid selection, try again"
    fi
  done
  ok "selected card,device: $AUDIO_DEVICE"
}

# ---------------------------------------------------------------------------
# §2.3 — svxlink.conf: bare logic first (no COMMAND_PTY), then §4.1 adds
# COMMAND_PTY as a separate, checked step — same staged approach as the doc,
# not collapsed into one blind write, so a bad audio/PTT config is caught
# before the queue mechanism is even added.
# ---------------------------------------------------------------------------
logic_exists() {
  grep -qE "^\[${LOGIC_NAME}\]" "$SVXLINK_CONF" 2>/dev/null
}

ptt_lines() {
  case "$PTT_TYPE" in
    gpio)
      local port="${PTT_PORT:-/dev/hidraw0}"
      printf 'PTT_TYPE=GPIO\nPTT_PORT=%s\nPTT_PIN=%s\n' "$port" "$PTT_PIN"
      ;;
    serial)
      local port="${PTT_PORT:-/dev/ttyUSB0}"
      printf 'PTT_TYPE=SERIAL\nPTT_PORT=%s\nPTT_PIN=RTS\n' "$port"
      ;;
    none)
      echo "PTT_TYPE=NONE"
      ;;
    *)
      die "unknown --ptt-type=$PTT_TYPE (expected gpio|serial|none)"
      ;;
  esac
}

render_bare_logic_block() {
  local dev="alsa:plughw:${AUDIO_DEVICE}"
  cat <<EOF

[${LOGIC_NAME}]
TYPE=Simplex
RX=Rx1
TX=Tx1
CALLSIGN=${CALLSIGN}

[Rx1]
TYPE=Local
AUDIO_DEV=${dev}
AUDIO_CHANNEL=0
SQL_DET=VOX
VOX_LIMIT=1000

[Tx1]
TYPE=Local
AUDIO_DEV=${dev}
AUDIO_CHANNEL=0
$(ptt_lines)
EOF
}

write_bare_logic() {
  log "[2.3] writing bare [${LOGIC_NAME}] (no COMMAND_PTY yet)"
  if [ "$DRY_RUN" -eq 1 ]; then
    dry "would append to $SVXLINK_CONF:"
    render_bare_logic_block | sed 's/^/    /'
    return 0
  fi
  backup_file "$SVXLINK_CONF"
  render_bare_logic_block >> "$SVXLINK_CONF"
}

restart_and_check_svxlink() {
  local label="$1"
  if [ "$DRY_RUN" -eq 1 ]; then
    dry "would run: systemctl restart svxlink ($label)"
    return 0
  fi
  log "restarting svxlink ($label)"
  systemctl restart svxlink
  sleep 2
  if ! systemctl is-active --quiet svxlink; then
    echo "-- journalctl -u svxlink (systemd-level events only) --"
    journalctl -u svxlink -n 20 --no-pager || true
    echo "-- tail of /var/log/svxlink (svxlink's own errors — usually the real cause) --"
    tail -n 20 /var/log/svxlink 2>/dev/null || true
    die "svxlink failed to start after $label — see the log above and documentation/svxlink-txqueue-SETUP.md §2 troubleshooting"
  fi
  if [ -f /var/log/svxlink ] && grep -q '\*\*\* ERROR' /var/log/svxlink; then
    warn "/var/log/svxlink contains an ERROR line — check it before continuing"
  fi
  ok "svxlink is active ($label)"
}

# Section-aware: only ever looks inside [${LOGIC_NAME}], never touches
# other sections, even ones with lines that happen to match a pattern.
command_pty_present() {
  awk -v logic="[${LOGIC_NAME}]" '
    $0 == logic { inside=1; next }
    /^\[/ { inside=0 }
    inside && /^COMMAND_PTY=/ { found=1 }
    END { exit !found }
  ' "$SVXLINK_CONF"
}

add_command_pty() {
  log "[4.1] adding COMMAND_PTY to [${LOGIC_NAME}]"
  if command_pty_present; then
    log "COMMAND_PTY already present in [${LOGIC_NAME}], skipping"
    return 0
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    dry "would insert COMMAND_PTY=${COMMAND_PTY} right after [${LOGIC_NAME}]"
    return 0
  fi
  backup_file "$SVXLINK_CONF"
  local tmp
  tmp="$(mktemp)"
  awk -v logic="[${LOGIC_NAME}]" -v pty="COMMAND_PTY=${COMMAND_PTY}" '
    { print }
    $0 == logic { print pty; next }
  ' "$SVXLINK_CONF" > "$tmp"
  atomic_replace "$tmp" "$SVXLINK_CONF"
}

configure_svxlink() {
  if [ "$SKIP_SVXLINK_CONFIG" -eq 1 ]; then
    log "skipping svxlink.conf configuration (--skip-svxlink-config)"
    return 0
  fi

  if logic_exists; then
    log "[${LOGIC_NAME}] already exists in $SVXLINK_CONF — leaving Rx1/Tx1 as configured"
  else
    write_bare_logic
    restart_and_check_svxlink "bare logic, before COMMAND_PTY"
  fi

  add_command_pty
  restart_and_check_svxlink "with COMMAND_PTY"

  if [ "$DRY_RUN" -eq 0 ]; then
    if [ -L "$COMMAND_PTY" ]; then
      ok "confirmed: $COMMAND_PTY is a symlink (SvxLink is listening)"
    else
      die "$COMMAND_PTY did not appear after restart — check /var/log/svxlink"
    fi
  fi
}

# ---------------------------------------------------------------------------
# §4.2 — spool directories
# ---------------------------------------------------------------------------
create_spool_dirs() {
  log "[4.2] creating spool directories under $SPOOL_ROOT"
  local d
  for d in incoming queue staging sent failed; do
    if [ "$DRY_RUN" -eq 1 ]; then
      dry "would run: install -d -o $SVXLINK_USER -g $SVXLINK_USER -m 2775 $SPOOL_ROOT/$d"
    else
      install -d -o "$SVXLINK_USER" -g "$SVXLINK_USER" -m 2775 "$SPOOL_ROOT/$d"
    fi
  done
}

# ---------------------------------------------------------------------------
# §4.3 — the daemon, §4.4 — the systemd unit
# ---------------------------------------------------------------------------
install_daemon() {
  log "[4.3] installing the svxlink-txqueue daemon"
  [ -f "$DAEMON_SRC" ] || die "daemon source not found: $DAEMON_SRC"
  if [ "$DRY_RUN" -eq 1 ]; then
    dry "would install $DAEMON_SRC -> $DAEMON_DST (mode 0755)"
    return 0
  fi
  install -m 0755 "$DAEMON_SRC" "$DAEMON_DST"
  python3 -c "import ast; ast.parse(open('$DAEMON_DST').read())" && ok "daemon script is valid Python"
}

install_unit() {
  log "[4.4] installing the systemd unit"
  [ -f "$UNIT_SRC" ] || die "unit source not found: $UNIT_SRC"
  if [ "$DRY_RUN" -eq 1 ]; then
    dry "would install $UNIT_SRC -> $UNIT_DST"
    [ "$SVXLINK_USER" != "svxlink" ] && dry "would patch User=/Group= to $SVXLINK_USER"
    dry "would run: systemctl daemon-reload && systemctl enable --now svxlink-txqueue.service"
    return 0
  fi
  if [ "$SVXLINK_USER" != "svxlink" ]; then
    sed -e "s/^User=svxlink/User=${SVXLINK_USER}/" -e "s/^Group=svxlink/Group=${SVXLINK_USER}/" \
      "$UNIT_SRC" > "$UNIT_DST"
  else
    install -m 0644 "$UNIT_SRC" "$UNIT_DST"
  fi
  systemctl daemon-reload
  systemctl enable --now svxlink-txqueue.service
  systemctl status --no-pager svxlink-txqueue.service || true
}

# ---------------------------------------------------------------------------
# §2.5 — Direwolf: confirm gen_packets and stop there, no daemon/unit for it
# ---------------------------------------------------------------------------
check_direwolf() {
  log "[2.5] confirming gen_packets"
  if command -v gen_packets >/dev/null 2>&1; then
    ok "gen_packets found: $(command -v gen_packets)"
  else
    die "gen_packets not found on PATH — the direwolf install may have failed"
  fi
}

# ---------------------------------------------------------------------------
# radiobeacon's own .env — requires .env to already exist (./start.sh owns
# creating it); this script only ever upserts specific keys, never creates
# or wipes the file.
# ---------------------------------------------------------------------------
configure_env() {
  if [ "$SKIP_ENV" -eq 1 ]; then
    log "skipping .env wiring (--skip-env)"
    return 0
  fi
  log "wiring radiobeacon's .env (BEACON_WAV_TRANSMITTER=spool)"
  if [ ! -f "$ENV_FILE" ]; then
    warn "$ENV_FILE does not exist — run ./start.sh first, then re-run with --skip-svxlink-config to just wire .env, or pass --skip-env to skip this permanently"
    return 0
  fi
  backup_file "$ENV_FILE"
  set_env_var BEACON_WAV_TRANSMITTER spool "$ENV_FILE"
  set_env_var BEACON_TXQUEUE_INCOMING_DIR "${SPOOL_ROOT}/incoming" "$ENV_FILE"
  set_env_var BEACON_GEN_PACKETS_BINARY gen_packets "$ENV_FILE"
}

# ---------------------------------------------------------------------------
# §6 — optional test transmission. Never runs unprompted: this keys the
# real transmitter.
# ---------------------------------------------------------------------------
offer_verify() {
  [ "$SKIP_VERIFY" -eq 1 ] && return 0
  [ "$DRY_RUN" -eq 1 ] && return 0
  [ -t 0 ] || return 0
  echo
  echo "${C_BOLD}${C_RED}This next step KEYS THE REAL TRANSMITTER.${C_RESET} Use a dummy load or a"
  echo "clear simplex frequency, and identify per your licence."
  local ans=""
  read -r -p "Run a test transmission now (plays a short 'online' clip)? [y/N] " ans || true
  case "$ans" in
    y|Y|yes|YES)
      local sample="/usr/share/svxlink/sounds/en_US/Core/online.wav"
      if [ ! -f "$sample" ]; then
        warn "sample clip not found at $sample, skipping"
        return 0
      fi
      sudo -u "$SVXLINK_USER" sh -c "printf 'EVENT ::playFile ${sample}\n' > '$COMMAND_PTY'" \
        || warn "test transmission failed — check /var/log/svxlink"
      ok "sent. Check /var/log/svxlink for 'Turning the transmitter ON' ... 'OFF'."
      ;;
    *)
      echo "skipped"
      ;;
  esac
}

# ---------------------------------------------------------------------------
# §8 — uninstall (mirrors the doc's manual teardown steps)
# ---------------------------------------------------------------------------
do_uninstall() {
  log "uninstalling svxlink-txqueue (packages left alone; see --purge-packages)"
  if [ "$DRY_RUN" -eq 1 ]; then
    dry "would run: systemctl disable --now svxlink-txqueue.service"
    dry "would remove $UNIT_DST, $DAEMON_DST"
    dry "would remove the COMMAND_PTY= line from [${LOGIC_NAME}] in $SVXLINK_CONF"
    dry "would remove spool directories under $SPOOL_ROOT"
    [ "$PURGE_PACKAGES" -eq 1 ] && dry "would run: apt-get remove -y svxlink-server direwolf"
    return 0
  fi
  systemctl disable --now svxlink-txqueue.service 2>/dev/null || true
  rm -f "$UNIT_DST"
  systemctl daemon-reload
  rm -f "$DAEMON_DST"
  if [ -f "$SVXLINK_CONF" ] && command_pty_present; then
    backup_file "$SVXLINK_CONF"
    local tmp
    tmp="$(mktemp)"
    awk -v logic="[${LOGIC_NAME}]" '
      $0 == logic { inside=1; print; next }
      /^\[/ { inside=0 }
      inside && /^COMMAND_PTY=/ { next }
      { print }
    ' "$SVXLINK_CONF" > "$tmp"
    atomic_replace "$tmp" "$SVXLINK_CONF"
    systemctl restart svxlink || true
  fi
  rm -rf "$SPOOL_ROOT"
  if [ "$PURGE_PACKAGES" -eq 1 ]; then
    apt-get remove -y svxlink-server direwolf
  fi
  ok "uninstall complete"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if [ "$DO_UNINSTALL" -eq 1 ]; then
  do_uninstall
  exit 0
fi

stage_packages
detect_svxlink_user
detect_audio_device
configure_svxlink
create_spool_dirs
install_daemon
install_unit
check_direwolf
configure_env
offer_verify

echo
echo "${C_BOLD}${C_GREEN}== summary ==${C_RESET}"
echo "SvxLink + Direwolf packages installed."
echo "Logic [$LOGIC_NAME] configured with COMMAND_PTY=$COMMAND_PTY."
echo "svxlink-txqueue daemon installed and running as a systemd service."
echo "Spool root: $SPOOL_ROOT"
if [ "$SKIP_ENV" -eq 0 ]; then
  echo "radiobeacon's .env updated: BEACON_WAV_TRANSMITTER=spool"
fi
echo
echo "${C_DIM}Still manual: programming the radio itself, the antenna/feedline, and your licence.${C_RESET}"
echo "Next: ./start.sh to bring up radiobeacon (beacon starts disabled — see BEACON_ENABLED)."
echo "See documentation/svxlink-txqueue-SETUP.md for full background on every step above."
