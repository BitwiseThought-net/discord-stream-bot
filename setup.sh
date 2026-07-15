#!/usr/bin/env bash

# Exit immediately if a command exits with a non-zero status
set -e

echo "===================================================="
echo "      Discord Stream Bot - Installer Script"
echo "===================================================="

# Check if script is running as root
if [ "$EUID" -eq 0 ]; then
  echo "❌ Please run this script as your regular user (e.g., pi), NOT as root or using sudo directly."
  echo "The script will automatically request sudo permissions when needed."
  exit 1
fi

# 1. Update system package lists
echo -e "\n🔄 Updating system package repositories..."
sudo apt-get update

# 2. Check and Install Docker Engine
if ! command -v docker &> /dev/null; then
    echo "📦 Docker not found. Installing official Docker Engine..."
    curl -fsSL https://docker.com -o get-docker.sh
    sudo sh get-docker.sh
    rm get-docker.sh

    # Add current user to docker group to avoid running docker commands with sudo
    echo "👤 Adding $USER to the docker group..."
    sudo usermod -aG docker "$USER"
    echo "⚠️ Group changes require a system relog. We will use sudo for the remainder of this setup."
else
    echo "✅ Docker Engine is already installed."
fi

# 3. Check and Install Docker Compose
if ! docker compose version &> /dev/null; then
    echo "📦 Docker Compose plugin not found. Installing package..."
    sudo apt-get install -y docker-compose-plugin
else
    echo "✅ Docker Compose plugin is already installed."
fi

# 4. Check for .env Configuration
if [ ! -f .env ]; then
    echo -e "\n📝 .env configuration file missing! Creating a template..."
    cat << EOF > .env
DISCORD_TOKEN=your_actual_discord_bot_token_here
INPUT_DEVICE=hw:1,0
EOF
    echo "⚠️ A baseline '.env' file has been generated."
    echo "👉 Please pause now, open '.env', and paste your real DISCORD_TOKEN before starting."
else
    # Quick sanity validation to make sure user didn't leave placeholder text
    if grep -q "your_actual_discord_bot_token_here" .env; then
        echo -e "\n🛑 WARNING: Your .env file still contains the default token placeholder!"
        echo "Please edit your '.env' file with a valid Discord application token before proceeding."
    else
        echo "✅ '.env' file validated with custom settings."
    fi
fi

# 5. Build and deploy containerized environment
echo -e "\n🚀 Building and starting the Discord Stream Bot container..."
# Use sudo here to ensure commands execute cleanly even if group privileges haven't refreshed yet
sudo docker compose up -d --build

echo -e "\n===================================================="
echo "🎉 Setup complete! The bot is compiling in the background."
echo "🤖 Container configuration 'restart: always' is active."
echo "   The bot will automatically start whenever this OS boots."
echo "===================================================="
echo -e "\nTo view live stream audio initialization logs, run:"
echo "👉 sudo docker compose logs -f"
