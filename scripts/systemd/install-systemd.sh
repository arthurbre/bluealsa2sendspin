#!/bin/bash
# bluealsa2sendspin systemd service installation
set -e

# Ensure output is visible even when piped
exec 2>&1

# Colors
C='\033[0;36m'
G='\033[0;32m'
Y='\033[1;33m'
R='\033[0;31m'
B='\033[1m'
D='\033[2m'
N='\033[0m'

# Detect if running interactively
INTERACTIVE=true
if [ ! -t 0 ]; then
    # stdin is not a terminal (piped)
    if [ ! -c /dev/tty ]; then
        # No TTY available - fully non-interactive
        INTERACTIVE=false
        echo "Running in non-interactive mode - using defaults" >&2
    fi
fi

# Prompt for yes/no with configurable default
# Usage: prompt_yn "question" [default]
# default can be "yes" (default) or "no"
prompt_yn() {
    local question="$1"
    local default="${2:-yes}"

    if [ "$INTERACTIVE" = true ]; then
        if [ "$default" = "no" ]; then
            read -p "$question [y/N] " -n1 -r REPLY </dev/tty; echo
            [[ $REPLY =~ ^[Yy]$ ]]
        else
            read -p "$question [Y/n] " -n1 -r REPLY </dev/tty; echo
            [[ ! $REPLY =~ ^[Nn]$ ]]
        fi
    else
        echo "$question [auto: $default]"
        [ "$default" = "yes" ]
    fi
}

# Prompt for input with default value
# Usage: VAR=$(prompt_input "prompt text" "default value")
prompt_input() {
    local prompt="$1"
    local default="$2"
    if [ "$INTERACTIVE" = true ]; then
        echo -en "${C}${prompt}${N} [$default]: " >&2
        read -r REPLY </dev/tty
        echo "${REPLY:-$default}"
    else
        echo "Using default for $prompt: $default" >&2
        echo "$default"
    fi
}

# Check for root
[[ $EUID -ne 0 ]] && { echo -e "${R}Error:${N} Please run with sudo or as root"; exit 1; }

echo -e "\n${B}${C}bluealsa2sendspin Service Installation${N}\n"

# Determine user setup: if run as root directly, use dedicated user automatically
# If run via sudo, offer choice
USE_DEDICATED_USER=true
DAEMON_USER="bluealsa2sendspin"
DAEMON_HOME="/home/bluealsa2sendspin"

if [[ -n "$SUDO_USER" && "$SUDO_USER" != "root" ]]; then
    # Run via sudo - offer choice
    echo -e "${C}User Setup${N}"
    echo -e "${D}You can run bluealsa2sendspin as a dedicated 'bluealsa2sendspin' user (recommended)"
    echo -e "or as your current user ($SUDO_USER).${N}"
    echo ""

    if prompt_yn "Use dedicated 'bluealsa2sendspin' user?" "yes"; then
        DAEMON_USER="bluealsa2sendspin"
    else
        USE_DEDICATED_USER=false
        DAEMON_USER="$SUDO_USER"
    fi
else
    # Run as root directly - use dedicated user automatically
    echo -e "${C}User Setup${N}"
    echo -e "${D}Running as root - will use dedicated 'bluealsa2sendspin' user${N}"
fi

# Create bluealsa2sendspin user if using dedicated user and it doesn't exist
if [ "$USE_DEDICATED_USER" = true ] && ! id -u bluealsa2sendspin &>/dev/null; then
    echo -e "${D}Creating bluealsa2sendspin system user...${N}"
    useradd -r -m -d "$DAEMON_HOME" -s /usr/sbin/nologin -c "bluealsa2sendspin Daemon" bluealsa2sendspin || \
        { echo -e "${R}Failed to create user${N}"; exit 1; }

    # Add to audio group in case the local org.bluealsa D-Bus policy restricts
    # access to it (default bluez-alsa policies vary by distro)
    usermod -a -G audio bluealsa2sendspin 2>/dev/null || true

    echo -e "${G}✓${N} Created bluealsa2sendspin system user"
elif [ "$USE_DEDICATED_USER" = true ]; then
    echo -e "${D}User 'bluealsa2sendspin' already exists${N}"
fi

# Read the real home directory from passwd rather than assuming /home/$DAEMON_USER:
# it's what systemd will populate $HOME with for a User=$DAEMON_USER service, and
# the two must agree since that's where the daemon's identity/pairing state lives.
DAEMON_HOME="$(getent passwd "$DAEMON_USER" | cut -d: -f6)"
if [ -z "$DAEMON_HOME" ]; then
    echo -e "${R}Error:${N} could not determine home directory for user '$DAEMON_USER'"
    exit 1
fi

echo -e "${D}Daemon will run as: ${B}$DAEMON_USER${N} ${D}(home: $DAEMON_HOME)${N}"

echo -e "\n${C}Checking dependencies...${N}"

# BlueALSA itself is a prerequisite we don't install here - just warn if it's
# not obviously present, so the operator isn't left guessing why the service
# keeps restarting.
if command -v systemctl &>/dev/null && ! systemctl list-unit-files 2>/dev/null | grep -q '^bluealsa'; then
    echo -e "${Y}Warning:${N} No 'bluealsa'/'bluealsad' systemd service detected."
    echo -e "${D}bluealsa2sendspin requires BlueALSA (bluez-alsa) running and configured"
    echo -e "as an A2DP sink: https://github.com/arkq/bluez-alsa${N}"
fi

# Check for and offer to install uv if needed
if ! sudo -u "$DAEMON_USER" bash -l -c "command -v uv" &>/dev/null && \
   ! sudo -u "$DAEMON_USER" test -f "$DAEMON_HOME/.cargo/bin/uv" && \
   ! sudo -u "$DAEMON_USER" test -f "$DAEMON_HOME/.local/bin/uv"; then
    echo -e "${Y}Missing:${N} uv"
    if prompt_yn "Install now? (curl -LsSf https://astral.sh/uv/install.sh | sh)"; then
        sudo -u "$DAEMON_USER" bash -c "curl -LsSf https://astral.sh/uv/install.sh | sh" || { echo -e "${R}Failed${N}"; exit 1; }
        echo -e "${G}✓${N} uv installed"
    else
        echo -e "${R}Error:${N} uv required. Install with: ${B}curl -LsSf https://astral.sh/uv/install.sh | sh${N}"; exit 1
    fi
fi

# Install or upgrade bluealsa2sendspin
echo -e "\n${C}Installing bluealsa2sendspin...${N}"
if sudo -u "$DAEMON_USER" bash -l -c "uv tool list" 2>/dev/null | grep -q "^bluealsa2sendspin "; then
    echo -e "${D}bluealsa2sendspin already installed, upgrading...${N}"
    sudo -u "$DAEMON_USER" bash -l -c "uv tool upgrade bluealsa2sendspin" || { echo -e "${R}Failed${N}"; exit 1; }
else
    sudo -u "$DAEMON_USER" bash -l -c "uv tool install git+https://github.com/arthurbre/bluealsa2sendspin" || { echo -e "${R}Failed${N}"; exit 1; }
fi

# Grab the proper bin path from uv (in case it's non-standard)
BLUEALSA2SENDSPIN_BIN="$(sudo -u "$DAEMON_USER" bash -l -c "uv tool dir --bin")/bluealsa2sendspin"

# State dir mirrors default_state_dir(): $XDG_STATE_HOME/bluealsa2sendspin, or
# ~/.local/state/bluealsa2sendspin when XDG_STATE_HOME isn't set (systemd sets
# HOME for services with User=, matching what the daemon user would get logging in)
STATE_DIR="$DAEMON_HOME/.local/state/bluealsa2sendspin"
sudo -u "$DAEMON_USER" mkdir -p "$STATE_DIR"

# Read a KEY=value line out of the config file without ever treating its
# content as shell code (unlike `source`, this can't be tricked by a value
# containing spaces or shell metacharacters into breaking or executing).
# Usage: VAL=$(config_get KEY)
config_get() {
    sed -n "s/^$1=//p" "$CONFIG_FILE" | tail -n1
}

# Config: a plain KEY=value env file, loaded by the systemd unit via
# EnvironmentFile= and referenced from ExecStart as ${SERVER_URL}/${CLIENT_NAME}.
# Kept outside the uv tool install, so it survives `uv tool upgrade`.
CONFIG_DIR="/etc/bluealsa2sendspin"
CONFIG_FILE="$CONFIG_DIR/config"
mkdir -p "$CONFIG_DIR"

if [ -f "$CONFIG_FILE" ]; then
    echo -e "\n${G}✓${N} Existing config detected at ${B}$CONFIG_FILE${N} — keeping it as-is."
    SERVER_URL=$(config_get SERVER_URL)
    CLIENT_NAME=$(config_get CLIENT_NAME)
else
    echo -e "\n${C}Configuration${N}"
    SERVER_URL=$(prompt_input "Music Assistant Sendspin server URL (e.g. ws://ma-host:8927/sendspin)" "")
    if [ -z "$SERVER_URL" ] && [ "$INTERACTIVE" = true ]; then
        while [ -z "$SERVER_URL" ]; do
            echo -e "${R}Error:${N} a server URL is required."
            SERVER_URL=$(prompt_input "Music Assistant Sendspin server URL" "")
        done
    fi
    if [ -z "$SERVER_URL" ]; then
        echo -e "${R}Error:${N} SERVER_URL is required but no TTY is available to prompt for one."
        echo -e "${D}Pre-create $CONFIG_FILE with SERVER_URL=... (and optionally CLIENT_NAME=...) and re-run.${N}"
        exit 1
    fi
    CLIENT_NAME=$(prompt_input "Client name (shown in Music Assistant)" "$(hostname)")

    tee "$CONFIG_FILE" > /dev/null << EOF
SERVER_URL=$SERVER_URL
CLIENT_NAME=$CLIENT_NAME
EOF
    chmod 644 "$CONFIG_FILE"
    echo -e "${G}✓${N} Config written to $CONFIG_FILE"
fi

# Whether pairing has actually succeeded (not just been attempted): identity.json
# and pairing.json are both created as soon as `pair` starts -- well before any
# PIN is exchanged -- so their mere presence can't be used as a "paired" signal.
# Instead, `pair` prints one of exactly two outcomes; capture that.
PAIRED_MARKER="$STATE_DIR/.paired-via-installer"

if [ -f "$PAIRED_MARKER" ]; then
    echo -e "\n${G}✓${N} Existing pairing state detected at ${B}$STATE_DIR${N} — skipping pairing."
    echo -e "${D}(Delete that directory to re-pair from scratch.)${N}"
elif [ "$INTERACTIVE" = true ]; then
    echo -e "\n${C}Pairing with Music Assistant${N}"
    echo -e "${D}This prints a PIN; add this source in Music Assistant and enter the PIN when prompted.${N}"
    PAIR_OUTPUT=$(sudo -u "$DAEMON_USER" env HOME="$DAEMON_HOME" \
        "$BLUEALSA2SENDSPIN_BIN" pair --server-url "$SERVER_URL" --client-name "$CLIENT_NAME" </dev/tty | tee /dev/tty)
    if echo "$PAIR_OUTPUT" | grep -q "Paired successfully"; then
        sudo -u "$DAEMON_USER" touch "$PAIRED_MARKER"
    fi
else
    echo -e "\n${C}Pairing with Music Assistant${N}"
    echo -e "${Y}Non-interactive mode: pairing needs to happen manually before the service can connect.${N}"
    echo -e "Run this once the service is installed:"
    echo -e "  ${B}sudo -u $DAEMON_USER $BLUEALSA2SENDSPIN_BIN pair --server-url \"$SERVER_URL\" --client-name \"$CLIENT_NAME\"${N}"
fi

# Check if service is currently running (to determine if we need to restart)
SERVICE_WAS_RUNNING=false
if systemctl is-active --quiet bluealsa2sendspin.service 2>/dev/null; then
    SERVICE_WAS_RUNNING=true
    echo -e "\n${C}Service Update${N}"
    echo -e "${D}Service is currently running, stopping for update...${N}"
    systemctl stop bluealsa2sendspin.service
fi

# Install service
cat > /etc/systemd/system/bluealsa2sendspin.service << EOF
[Unit]
Description=BlueALSA to Sendspin bridge
After=bluealsa.service bluealsad.service dbus.service network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$DAEMON_USER
EnvironmentFile=$CONFIG_FILE
ExecStart=$BLUEALSA2SENDSPIN_BIN run --server-url \${SERVER_URL} --client-name \${CLIENT_NAME}
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
SupplementaryGroups=audio

[Install]
WantedBy=multi-user.target
EOF

chmod 644 /etc/systemd/system/bluealsa2sendspin.service

# Reload systemd to pick up service changes
systemctl daemon-reload

# Enable and start/restart
echo -e "\n${C}Service Setup${N}"

# Check if service is enabled
SERVICE_ENABLED=false
if systemctl is-enabled --quiet bluealsa2sendspin.service 2>/dev/null; then
    SERVICE_ENABLED=true
fi

# Offer to enable on boot if not already enabled
if [ "$SERVICE_ENABLED" = false ]; then
    if prompt_yn "Enable on boot?"; then
        systemctl enable bluealsa2sendspin.service &>/dev/null
        echo -e "${D}Service enabled${N}"
    fi
else
    echo -e "${D}Service already enabled on boot${N}"
fi

# Start or restart the service
if [ "$SERVICE_WAS_RUNNING" = true ]; then
    echo -e "${D}Restarting service...${N}"
    systemctl restart bluealsa2sendspin.service
    echo -e "${G}✓${N} Service restarted"
elif [ -f "$PAIRED_MARKER" ]; then
    if prompt_yn "Start now?"; then
        systemctl start bluealsa2sendspin.service
        echo -e "${G}✓${N} Service started"
    fi
else
    echo -e "${Y}Not starting yet:${N} pairing hasn't completed. Start manually once it has:"
    echo -e "  ${B}sudo systemctl start bluealsa2sendspin${N}"
fi

# Summary
echo -e "\n${B}${G}Installation Complete!${N}\n"
echo -e "${C}Config:${N}  $CONFIG_FILE"
echo -e "${C}Service:${N} systemctl {start|stop|status} bluealsa2sendspin"
echo -e "${C}Logs:${N}    journalctl -u bluealsa2sendspin -f\n"
