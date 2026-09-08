#!/usr/bin/env bash
#
# install-msync-client-service.sh
#
# Installs msync_client.py as a systemd SYSTEM service so synchronized
# playback keeps running even when nobody is logged in.
#
# The service launches the client with NO arguments: the client auto-discovers
# the server on the LAN via UDP broadcast (config.py's DEFAULT_PORT). Edit
# config.py, then re-run this script to apply.
#
#   sudo ./install-msync-client-service.sh [options]
#
# Options:
#   --user USER     system user the service runs as (default: the user that
#                   invoked sudo, so their login session's audio daemon is
#                   reachable; created as an 'msync' system user otherwise)
#   --no-start      register + enable, but do NOT start the service now
#   --uninstall     remove the service (disable, delete unit + env file)
#   --help          show this help
#
# Safe to re-run: it re-installs/updates the unit + env file in place and
# restarts the service to apply the changes.
#
# The script also creates ./venv (if missing) and keeps its Python
# requirements (requirements.txt) installed, so a fresh checkout installs
# cleanly. Override the interpreter with MSYNC_VENV_PYTHON to manage your
# own environment instead.
#
# Audio notes
# -----------
# A system service connects to whatever audio daemon it can reach. The unit
# points XDG_RUNTIME_DIR / PIPEWIRE_RUNTIME_DIR / PULSE_SERVER at the
# service user's /run/user/<uid> dir (see /etc/msync/msync-client.env).
# That works when the service user has a running PipeWire/Pulse session.
# If you instead want pure ALSA (no session daemon) the service user still
# works because it's added to the 'audio' group — edit the env file and
# clear the three audio variables.
#
set -euo pipefail

SERVICE_NAME="msync-client"
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLIENT="$INSTALL_DIR/msync_client.py"
PYTHON="${MSYNC_VENV_PYTHON:-$INSTALL_DIR/venv/bin/python}"
ENV_DIR="/etc/msync"
ENV_FILE="$ENV_DIR/$SERVICE_NAME.env"
UNIT_FILE="/etc/systemd/system/$SERVICE_NAME.service"

SERVICE_USER="${SUDO_USER:-}"
START="yes"
UNINSTALL="no"

ensure_venv() {
    # Create and populate the project's virtualenv on first run, and keep
    # its requirements installed on every run. MSYNC_VENV_PYTHON overrides
    # the interpreter entirely for users who manage their own environment.
    if [ -n "${MSYNC_VENV_PYTHON:-}" ]; then
        return 0
    fi

    local venv_dir="$INSTALL_DIR/venv"
    local venv_py="$venv_dir/bin/python"

    if [ ! -x "$venv_py" ]; then
        echo "Creating virtualenv at $venv_dir ..."
        if command -v python3 >/dev/null 2>&1 && python3 -m venv "$venv_dir"; then
            :
        elif command -v virtualenv >/dev/null 2>&1 && virtualenv -p python3 "$venv_dir"; then
            :
        else
            echo "error: could not create a virtualenv at $venv_dir" >&2
            echo "  install python3-venv (e.g. sudo apt install python3-venv)" >&2
            echo "  or virtualenv (e.g. sudo apt install python3-virtualenv), then re-run." >&2
            exit 1
        fi
    fi

    # Some distros (Debian/Ubuntu) ship a python3 whose venv lacks pip —
    # the venv then exists but can install nothing. Bootstrap pip with the
    # official get-pip.py instead of failing cryptically later.
    if ! "$venv_py" -m pip --version >/dev/null 2>&1; then
        echo "Bootstrapping pip into $venv_dir ..."
        if command -v curl >/dev/null 2>&1; then
            curl -fsSL https://bootstrap.pypa.io/get-pip.py | "$venv_py" - \
                || { echo "error: could not bootstrap pip into $venv_dir" >&2; exit 1; }
        elif command -v wget >/dev/null 2>&1; then
            wget -qO- https://bootstrap.pypa.io/get-pip.py | "$venv_py" - \
                || { echo "error: could not bootstrap pip into $venv_dir" >&2; exit 1; }
        else
            echo "error: no curl or wget available to bootstrap pip" >&2
            echo "  install python3-venv (e.g. sudo apt install python3-venv) and re-run." >&2
            exit 1
        fi
    fi

    "$venv_dir/bin/pip" install -r "$INSTALL_DIR/requirements.txt" \
        || { echo "error: could not install $INSTALL_DIR/requirements.txt" >&2; exit 1; }
}

usage() {
    awk 'NR > 1 && /^#/ { line = $0; sub(/^# ?/, "", line); print line }
         NR > 1 && !/^#/ { exit }' "$0"
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --user)        SERVICE_USER="${2:?--user needs a value}"; shift 2 ;;
        --no-start)    START="no"; shift ;;
        --uninstall)   UNINSTALL="yes"; shift ;;
        --help|-h)     usage 0 ;;
        *)             echo "unknown option: $1" >&2; usage 1 ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "error: must run as root (try: sudo $0 ...)" >&2
    exit 1
fi

if [ "$UNINSTALL" = "yes" ]; then
    echo "Removing $SERVICE_NAME service..."
    systemctl disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
    rm -f "$UNIT_FILE"
    rm -rf "$ENV_DIR"
    systemctl daemon-reload
    echo "Removed. (The service user and /tmp cache were left in place.)"
    exit 0
fi

# --- sanity checks ------------------------------------------------------- #
[ -f "$CLIENT" ] || { echo "error: $CLIENT not found (run from the msync dir)" >&2; exit 1; }
ensure_venv   # create ./venv + install requirements.txt if needed

# --- read port from config.py (the service's source of truth) ------------- #
# Importing config.py here also picks up any MSYNC_DEFAULT_PORT env override
# present in this shell, exactly as the service will see them.
CFG_PORT="$( { cd "$INSTALL_DIR" && "$PYTHON" -c \
    'import config; print(config.DEFAULT_PORT)'; } 2>/dev/null )" \
    || { echo "error: could not read DEFAULT_PORT from config.py" >&2; exit 1; }
case "$CFG_PORT" in
    ''|*[!0-9]*) echo "error: config.py DEFAULT_PORT is not a valid port ('$CFG_PORT')" >&2; exit 1 ;;
esac

# --- pick / create the service user --------------------------------------- #
if [ -z "$SERVICE_USER" ]; then
    SERVICE_USER="msync"
    if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
        echo "Creating system user '$SERVICE_USER'..."
        useradd --system --no-create-home --shell /usr/sbin/nologin \
            --groups audio "$SERVICE_USER"
    fi
elif ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    echo "error: user '$SERVICE_USER' does not exist" >&2
    exit 1
fi
# Direct-ALSA fallback: make sure the user is in the audio group.
if ! id -nG "$SERVICE_USER" | tr ' ' '\n' | grep -qx audio; then
    echo "Adding '$SERVICE_USER' to the 'audio' group..."
    usermod -a -G audio "$SERVICE_USER"
fi
UID_VAL="$(id -u "$SERVICE_USER")"
RUN_DIR="/run/user/$UID_VAL"

# --- env file (audio only; port lives in config.py) ----------------------- #
install -d -m 0755 "$ENV_DIR"
cat > "$ENV_FILE" <<EOF
# msync $SERVICE_NAME service settings ($(date +%FT%T))
# Port is NOT set here: the service reads it from config.py (DEFAULT_PORT).
# Uncomment this to override config.py for this service only:
# MSYNC_DEFAULT_PORT=9770

# --- audio daemon access -------------------------------------------------
# Point the client at the service user's runtime dir, where PipeWire
# (pipewire-0) and pipewire-pulse sockets live. Clear these to use a bare
# ALSA device via the 'audio' group instead.
XDG_RUNTIME_DIR=$RUN_DIR
PIPEWIRE_RUNTIME_DIR=$RUN_DIR
PULSE_SERVER=unix:$RUN_DIR/pulse/native
EOF
chmod 0644 "$ENV_FILE"

# --- unit file ------------------------------------------------------------ #
cat > "$UNIT_FILE" <<EOF
[Unit]
Description=msync synchronized music client
Documentation=file:$INSTALL_DIR/README.md
Wants=network-online.target sound.target
After=network-online.target sound.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
EnvironmentFile=-$ENV_FILE
WorkingDirectory=$INSTALL_DIR
ExecStart=$PYTHON $CLIENT
Restart=on-failure
RestartSec=3
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "$UNIT_FILE"

echo "Installed $SERVICE_NAME (user=$SERVICE_USER)"
echo "UDP port read from config.py: $CFG_PORT (server auto-discovered on the LAN)"
sudo -u "$SERVICE_USER" "$PYTHON" -c "import sounddevice, numpy, miniaudio" \
    || echo "warning: service user's Python can't import the audio libs"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
if [ "$START" = "yes" ]; then
    systemctl restart "$SERVICE_NAME"   # starts a fresh install, re-applies a re-install
    echo "Started (restarted if it was already running). Status:"
    systemctl --no-pager --full --lines=5 status "$SERVICE_NAME" || true
else
    echo "Enabled but not started (--no-start). Start it with:"
    echo "  sudo systemctl start $SERVICE_NAME"
fi

echo
echo "Logs:   journalctl -u $SERVICE_NAME -f"
echo "Config: edit SERVER / DEFAULT_PORT in $INSTALL_DIR/config.py,"
echo "  then re-run:  sudo $0"