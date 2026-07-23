#!/usr/bin/env bash
# Exit immediately if a command exits with a non-zero status
set -e

echo "===================================================="
echo " Discord Stream Bot - Universal Installer"
echo "===================================================="

# Check if script is running as root
if [ "$EUID" -eq 0 ]; then
    echo "❌ Please run this script as your regular user, NOT as root or using sudo directly."
    echo "The script will automatically request sudo privileges when needed."
    exit 1
fi

# Detect Linux Distribution
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS_ID=$ID
    OS_LIKE=$ID_LIKE
else
    OS_ID="unknown"
    OS_LIKE="unknown"
fi

echo "🔍 Detecting operating system architecture..."

# 1. SPECIAL CASE: Manual Native Fix for Debian/Ubuntu on ARM64
if [[ "$OS_ID" == "debian" || "$OS_ID" == "ubuntu" || "$OS_LIKE" == *"debian"* || "$OS_LIKE" == *"ubuntu"* ]]; then
    echo "📦 Debian/Ubuntu variant detected. Performing direct native repository setup..."
    
    # Install foundational prerequisites and native sound/SDR apps
    sudo apt-get update
    sudo apt-get install -y ca-certificates curl gnupg alsa-utils sox libsox-fmt-all rtl-sdr librtlsdr-dev

    # Set up Docker's official signing keyring safely
    sudo install -m 0755 -d /etc/apt/keyrings
    sudo curl -fsSL https://docker.com -o /etc/apt/keyrings/docker.asc
    sudo chmod a+r /etc/apt/keyrings/docker.asc

    # Determine stable target branch fallback for testing/rolling releases (e.g. trixie -> bookworm)
    TARGET_SUITE="$VERSION_CODENAME"
    if [ "$TARGET_SUITE" = "trixie" ] || [ "$TARGET_SUITE" = "forky" ] || [ "$TARGET_SUITE" = "sid" ]; then
        TARGET_SUITE="bookworm"
    fi

    # Write the explicit native repository configuration file
    echo "Adding official Docker repository tracking ($TARGET_SUITE)..."
    echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://docker.com \
    $TARGET_SUITE stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

    # Pull down indexes and install Docker packages directly
    sudo apt-get update
    sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# 2. FALLBACK CASE: For Fedora/RHEL and Arch distributions
else
    case "$OS_ID" in
        fedora|rhel|centos)
            sudo dnf check-update || true
            sudo dnf install -y alsa-utils sox rtl-sdr
            sudo dnf config-manager --add-repo https://docker.com
            sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
            sudo systemctl start docker
            sudo systemctl enable docker
            ;;
        arch|manjaro)
            sudo pacman -Syu --noconfirm
            sudo pacman -S --noconfirm alsa-utils sox rtl-sdr docker docker-compose
            sudo systemctl start docker
            sudo systemctl enable docker
            ;;
        *)
            echo "❌ Linux distribution '$OS_ID' could not be automatically configured."
            echo "Please install Docker, ALSA, SoX, and rtl-sdr manually before running the bot."
            exit 1
            ;;
    esac
fi

# 3. OVERRIDE AUTOMATED DESKTOP TV TUNERS TO FREEOUT SDR SIGNAL REGISTERS
echo "🛡️  Configuring kernel override filters for Nooelec NESDR SMArt v5..."
BLACKLIST_CONF="/etc/modprobe.d/blacklist-rtlsdr.conf"
if [ ! -f "$BLACKLIST_CONF" ]; then
    sudo bash -c "cat << 'EOF' > $BLACKLIST_CONF
# Block host TV drivers from claiming Nooelec raw radio registers
blacklist dvb_usb_rtl2832u
blacklist rtl2832
blacklist rtl2830
blacklist dvb_usb_v2
blacklist dvb_core
EOF"
    echo "✅ Kernel rules saved to $BLACKLIST_CONF."
else
    echo "⏭️ Driver configuration exists. Skipping."
fi

# 4. DEPLOY RAW ACCESS PERMISSION SCHEMAS INSIDE REALTIME UDEV REGISTRIES
echo "🔌 Injecting interface control policies into local udev parameters..."
UDEV_RULES="/etc/udev/rules.d/20-rtlsdr.rules"
if [ ! -f "$UDEV_RULES" ]; then
    sudo bash -c "cat << 'EOF' > $UDEV_RULES
SUBSYSTEM==\"usb\", ATTRS{idVendor}==\"0bda\", ATTRS{idProduct}==\"2838\", MODE=\"0666\", GROUP=\"plugdev\"
EOF"
    sudo udevadm control --reload-rules && sudo udevadm trigger || true
    echo "✅ Applied local USB device layout boundaries."
fi

# Ensure current user has localized execution permissions
if ! groups "$USER" | grep -q "\bdocker\b"; then
    echo "👤 Adding $USER to the system 'docker' security group..."
    sudo usermod -aG docker "$USER"
fi

# Map non-root application accounts to standard system hardware queues
if ! groups "$USER" | grep -q "\baudio\b"; then
    echo "👤 Adding $USER to the system 'audio' access group..."
    sudo usermod -aG audio "$USER"
fi

# Check for .env Configuration
ENV_GENERATED=false
if [ ! -f .env ]; then
    echo -e "\n📝 Environment setup missing! Creating a new configuration template..."
    cat << EOF > .env
# Application Gateway Token
DISCORD_TOKEN=your_actual_discord_bot_token_here

# Customizable Application Slash Root Scope Name
COMMAND_BASE=radio

# Crash Recovery Automation Routing Profile Type
RECOVERY_MODE=resume

# Baseline Broadcast Spectrum Target Frequency Location (e.g. 94.9M)
SDR_FREQUENCY=94.9M
EOF
    ENV_GENERATED=true
fi

echo -e "\n===================================================="
echo "🎉 Setup complete! All prerequisites have been configured."
echo "===================================================="

if [ "$ENV_GENERATED" = true ]; then
    echo -e "👉 NEXT STEP REQUIRED:"
    echo -e "1. Open and edit the newly generated '.env' file."
    echo -e "2. Replace 'your_actual_discord_bot_token_here' with your real Discord Bot Token."
    echo -e "3. Verify 'SDR_FREQUENCY' handles your native operational station targets."
else
    echo -e "👉 NEXT STEP REQUIRED:"
    echo -e "Your '.env' file already exists. Please verify that it contains a valid token configuration."
fi

echo -e "\n⚠️ NOTE: Since hardware permissions, groups, and kernel modules were altered, you must log out of this terminal session or restart the system before starting up containers!"
echo -e "\nOnce your configuration is complete, run the following command to start the bot:"
echo -e "🚀 docker compose up -d --build\n"
