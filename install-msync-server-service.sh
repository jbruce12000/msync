#!/usr/bin/env bash
#
# install-msync-server-service.sh
#
# Installs msync_server.py as a systemd SYSTEM service so music keeps
# playing (and the web UI stays up) even when nobody is logged in.
#
# The service launches the server with no arguments: the music dir, sync
# port and catalog DB come from config.py (MUSIC_DIR / DEFAULT_PORT /
# DB_PATH, or their MSYNC_MUSIC_DIR / MSYNC_DEFAULT_PORT / MSYNC_DB_PATH
# env overrides). Edit config.py, then re-run this script to apply.
#
#   sudo ./install-msync-server-service.sh [options]
#
# Options:
#   --user USER     system user the service runs as (default: the user that
#                   invoked sudo — their music dir and catalog DB are already
#                   accessible; a dedicated 'msync' system user is created
#                   otherwise, with the catalog DB relocated to /var/lib/msync)
#   --no-start      register + enable, but do NOT start the service now
#   --uninstall     remove the service (disable, delete unit + env file)
#   --help          show this help
#
# Safe to re-run: it re-installs/updates the unit + env file in place and
# restarts the service to apply the changes.
#
# Audio & permissions notes
# -------------------------
# The server plays audio too, so the unit points XDG_RUNTIME_DIR /
# PIPEWIRE_RUNTIME_DIR / PULSE_SERVER at the service user's /run/user/<uid>
# dir. That works when that user has a running PipeWire/Pulse session. For
# headless ALSA the user must be in the 'audio' group (the installer adds
# it) — clear the three audio vars in the env file.
#
# The service user also needs to READ the music dir and WRITE a catalog DB.
# Using the sudo-invoking user satisfies both out of the box; a dedicated
# 'msync' user gets the DB relocated to /var/lib/msync but the music dir
# must be made readable for it (the installer warns if it isn't).
#
set -euo pipefail

SERVICE_NAME="msync-server"
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_SCRIPT="$INSTALL_DIR/msync_server.py"
PYTHON="${MSYNC_VENV_PYTHON:-$INSTALL_DIR/venv/bin/python}"
ENV_DIR="/etc/msync"
ENV_FILE="$ENV_DIR/$SERVICE_NAME.env"
UNIT_FILE="/etc/systemd/system/$SERVICE_NAME.service"

SERVICE_USER="${SUDO_USER:-}"
START="yes"
UNINSTALL="no"

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
    echo "Removed. (The service user, catalog DB and /var/lib/msync were left in place.)"
    exit 0
fi

# --- sanity checks ------------------------------------------------------- #
[ -f "$SERVER_SCRIPT" ] || { echo "error: $SERVER_SCRIPT not found (run from the msync dir)" >&2; exit 1; }
[ -x "$PYTHON" ] || { echo "error: venv python not found at $PYTHON" >&2; exit 1; }

# --- read settings from config.py (the service's source of truth) -------- #
# Importing config.py here also picks up any MSYNC_* env overrides present
# in this shell, exactly as the service will see them.
CFG_LINE="$( { cd "$INSTALL_DIR" && "$PYTHON" -c \
    'import config; print("%s|%s|%s|%s" % (config.MUSIC_DIR, config.DEFAULT_PORT, config.DB_PATH, config.HTTP_PORT_OFFSET))'; } 2>/dev/null )" \
    || { echo "error: could not read config.py" >&2; exit 1; }
CFG_MUSIC="${CFG_LINE%%|*}"
CFG_REST="${CFG_LINE#*|}"
CFG_PORT="${CFG_REST%%|*}"
CFG_REST="${CFG_REST#*|}"
CFG_DB="${CFG_REST%%|*}"
CFG_HTTP_OFF="${CFG_REST#*|}"
case "$CFG_PORT" in
    ''|*[!0-9]*) echo "error: config.py DEFAULT_PORT is not a valid port ('$CFG_PORT')" >&2; exit 1 ;;
esac
[ -d "$CFG_MUSIC" ] || { echo "error: config.py MUSIC_DIR does not exist: $CFG_MUSIC" >&2; exit 1; }

# --- pick / create the service user --------------------------------------- #
DB_ACTIVE=""    # active MSYNC_DB_PATH= line for the env file (dedicated user)
if [ -z "$SERVICE_USER" ]; then
    SERVICE_USER="msync"
    if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
        echo "Creating system user '$SERVICE_USER'..."
        useradd --system --no-create-home --shell /usr/sbin/nologin \
            --groups audio "$SERVICE_USER"
    fi
    # A dedicated user can't write config.py's DB (it lives in the owner's
    # project dir), so relocate the catalog DB to /var/lib/msync.
    install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 /var/lib/msync
    DB_ACTIVE="MSYNC_DB_PATH=/var/lib/msync/msync.db"
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

# --- env file (audio only; music/port/db live in config.py) --------------- #
install -d -m 0755 "$ENV_DIR"
cat > "$ENV_FILE" <<EOF
# msync $SERVICE_NAME service settings ($(date +%FT%T))
# Music dir, sync port and catalog DB are read from config.py. Uncomment to
# override config.py for this service only (they take effect via config.py's
# env-var handling):
# MSYNC_MUSIC_DIR=/path/to/music
# MSYNC_DEFAULT_PORT=9770
# MSYNC_DB_PATH=/var/lib/msync/msync.db
$DB_ACTIVE

# --- audio daemon access -------------------------------------------------
# Point the server at the service user's runtime dir, where PipeWire
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
Description=msync synchronized music server
Documentation=file:$INSTALL_DIR/README.md
Wants=network-online.target sound.target
After=network-online.target sound.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
EnvironmentFile=-$ENV_FILE
WorkingDirectory=$INSTALL_DIR
ExecStart=$PYTHON $SERVER_SCRIPT --headless
Restart=on-failure
RestartSec=3
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "$UNIT_FILE"

echo "Installed $SERVICE_NAME (user=$SERVICE_USER)"
echo "Music dir (from config.py): $CFG_MUSIC"
echo "Sync port (from config.py): $CFG_PORT   HTTP/web: http://<server-ip>:$((CFG_PORT + CFG_HTTP_OFF))/"
[ -n "$DB_ACTIVE" ] && echo "Catalog DB relocated for $SERVICE_USER: /var/lib/msync/msync.db"
echo "    config.py DB_PATH was: $CFG_DB"
sudo -u "$SERVICE_USER" "$PYTHON" -c "import sounddevice, numpy, miniaudio" \
    || echo "warning: service user's Python can't import the audio libs"

# --- permission self-checks ------------------------------------------------ #
if ! sudo -u "$SERVICE_USER" test -r "$CFG_MUSIC"; then
    echo "warning: music dir '$CFG_MUSIC' is not readable by '$SERVICE_USER'."
    echo "  grant access, e.g.:  setfacl -m u:$SERVICE_USER:rx $CFG_MUSIC"
fi
DB_DIR="$(dirname "$CFG_DB")"
if [ -n "$DB_ACTIVE" ]; then
    DB_DIR="/var/lib/msync"
fi
if ! sudo -u "$SERVICE_USER" test -w "$DB_DIR"; then
    echo "warning: catalog dir '$DB_DIR' is not writable by '$SERVICE_USER'."
fi

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
echo "Config: edit MUSIC_DIR / DEFAULT_PORT / DB_PATH in $INSTALL_DIR/config.py,"
echo "  then re-run:  sudo $0"
echo "Web:    http://<server-ip>:$((CFG_PORT + CFG_HTTP_OFF))/"