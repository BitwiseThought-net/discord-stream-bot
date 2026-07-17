#!/usr/bin/env bash

# Exit immediately if a command exits with a non-zero status
set -e

echo "===================================================="
echo "      Discord Stream Bot - Universal Installer"
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
    
    # Install foundational prerequisites
    sudo apt-get update
    sudo apt-get install -y ca-certificates curl gnupg

    # Set up Docker's official signing keyring safely
    sudo install -m 0755 -d /etc/apt/keyrings
    sudo curl -fsSL https://docker.com -o /etc/apt/keyrings/docker.asc
    sudo chmod a+r /etc/apt/keyrings/docker.asc

    # Determine stable target branch fallback for testing/rolling releases (e.g. trixie -> bookworm)
    # This prevents '404 Repository Not Found' errors on newer release versions
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
            sudo dnf config-manager --add-repo https://docker.com
            sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
            sudo systemctl start docker
            sudo systemctl enable docker
            ;;
        arch|manjaro)
            sudo pacman -Syu --noconfirm
            sudo pacman -S --noconfirm docker docker-compose
            sudo systemctl start docker
            sudo systemctl enable docker
            ;;
        *)
            echo "❌ Linux distribution '$OS_ID' could not be automatically configured."
            echo "Please install Docker and Docker Compose manually before running the bot."
            exit 1
            ;;
    esac
fi

# Ensure current user has localized execution permissions
if ! groups "$USER" | grep -q "\bdocker\b"; then
    echo "👤 Adding $USER to the system 'docker' security group..."
    sudo usermod -aG docker "$USER"
fi

# Check for .env Configuration
ENV_GENERATED=false
if [ ! -f .env ]; then
    echo -e "\n📝 Environment setup missing! Creating a new configuration template..."
    cat << EOF > .env
DISCORD_TOKEN=your_actual_discord_bot_token_here
INPUT_DEVICE=hw:1,0
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
    echo -e "3. Verify 'INPUT_DEVICE' matches your hardware mapping from 'arecord -l'."
else
    echo -e "👉 NEXT STEP REQUIRED:"
    echo -e "Your '.env' file already exists. Please verify that it contains a valid token configuration."
fi

echo -e "\nOnce your configuration is complete, run the following command to start the bot:"
echo -e "🚀 docker compose up -d --build\n"

