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

# Detect Linux Distribution and set package manager commands
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS_ID=$ID
    OS_LIKE=$ID_LIKE
else
    OS_ID="unknown"
    OS_LIKE="unknown"
fi

echo "🔍 Detecting operating system architecture..."
case "$OS_ID" in
    ubuntu|debian|raspbian|pop)
        PKG_MANAGER="apt"
        UPDATE_CMD="sudo apt-get update"
        INSTALL_DOCKER_COMP="sudo apt-get install -y docker-compose-plugin"
        ;;
    fedora|rhel|centos)
        PKG_MANAGER="dnf"
        UPDATE_CMD="sudo dnf check-update || true"
        INSTALL_DOCKER_COMP="sudo dnf install -y docker-compose-plugin"
        ;;
    arch|manjaro)
        PKG_MANAGER="pacman"
        UPDATE_CMD="sudo pacman -Syu --noconfirm"
        INSTALL_DOCKER_COMP="sudo pacman -S --noconfirm docker-compose"
        ;;
    *)
        # Check ID_LIKE strings fallback
        if [[ "$OS_LIKE" == *"debian"* ]] || [[ "$OS_LIKE" == *"ubuntu"* ]]; then
            PKG_MANAGER="apt"
            UPDATE_CMD="sudo apt-get update"
            INSTALL_DOCKER_COMP="sudo apt-get install -y docker-compose-plugin"
        elif [[ "$OS_LIKE" == *"fedora"* ]]; then
            PKG_MANAGER="dnf"
            UPDATE_CMD="sudo dnf check-update || true"
            INSTALL_DOCKER_COMP="sudo dnf install -y docker-compose-plugin"
        elif [[ "$OS_LIKE" == *"arch"* ]]; then
            PKG_MANAGER="pacman"
            UPDATE_CMD="sudo pacman -Syu --noconfirm"
            INSTALL_DOCKER_COMP="sudo pacman -S --noconfirm docker-compose"
        else
            echo "⚠️ Linux distribution '$OS_ID' not explicitly recognized."
            echo "Skipping native package synchronization. Assuming dependencies are manually maintained."
            PKG_MANAGER="manual"
        fi
        ;;
esac

# Run update sequence if package manager detected
if [ "$PKG_MANAGER" != "manual" ]; then
    echo "🔄 Synchronizing system repositories using $PKG_MANAGER..."
    $UPDATE_CMD
fi

# Check and Install Docker Engine
if ! command -v docker &> /dev/null; then
    echo "📦 Docker not found. Deploying via official Docker installer convenience script..."
    
    # Secure download fallback logic
    if command -v curl &> /dev/null; then
        curl -fsSL https://docker.com -o get-docker.sh || true
    fi
    
    # If curl failed or is missing, attempt fallback with wget
    if [ ! -s get-docker.sh ] && command -v wget &> /dev/null; then
        echo "🌐 Curl failed or missing. Retrying download using wget..."
        wget -qO get-docker.sh https://docker.com || true
    fi

    # Absolute safety check: Validate that the file downloaded and is not empty/HTML error markup
    if [ ! -s get-docker.sh ] || grep -q "<html" get-docker.sh; then
        echo "❌ Critical Error: Unable to securely fetch the official Docker installation script."
        echo "Please verify your internet connection or install Docker manually via your package manager."
        rm -f get-docker.sh
        exit 1
    fi

    sudo sh get-docker.sh
    rm -f get-docker.sh
    
    echo "👤 Adding $USER to the system 'docker' security group..."
    sudo usermod -aG docker "$USER"
    echo "⚠️ Group profile changes require an active session relog. Sudo overrides will step in for initialization."
else
    echo "✅ Docker Engine environment verified."
fi

# Check and Install Docker Compose
if ! docker compose version &> /dev/null; then
    if [ "$PKG_MANAGER" != "manual" ]; then
        echo "📦 Docker Compose plugin missing. Provisioning dependency package..."
        $INSTALL_DOCKER_COMP
    else
        echo "❌ Docker Compose command missing. Please install docker-compose manually on your OS environment."
        exit 1
    fi
else
    echo "✅ Docker Compose core plugin verified."
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

