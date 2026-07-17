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
    sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
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
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian \
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
if [ ! -f .env ]; then
    echo -e "\n📝 Environment setup missing! Creating a new configuration template..."
    cat << EOF > .env
DISCORD_TOKEN=your_actual_discord_bot_token_here
INPUT_DEVICE=hw:1,0
EOF
    echo "⚠️ A new '.env' template file has been generated."
    echo "👉 Please edit '.env' to enter your real DISCORD_TOKEN configuration before starting."
else
    if grep -q "your_actual_discord_bot_token_here" .env; then
        echo -e "\n🛑 WARNING: Your '.env' target file is currently pointing to placeholder data!"
        echo "Update your '.env' parameters with actual application parameters before running."
    else
        echo "✅ '.env' local configuration settings validated."
    fi
fi

# Build and deploy containerized environment
echo -e "\n🚀 Compiling runtime architecture and spinning up background container instances..."
sudo docker compose up -d --build

echo -e "\n===================================================="
echo "🎉 Setup complete! The service application has been launched."
echo "🤖 Active policy 'restart: always' is enforced."
echo "   The application instance will spin up automatically on system boot."
echo "===================================================="
echo -e "\nTo inspect live audio stream data capture feeds, use:"
echo "👉 sudo docker compose logs -f"

