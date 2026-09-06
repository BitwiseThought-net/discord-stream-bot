FROM python:3.11-slim

# Needed for the docker-compose binary download below (arch-aware --
# see TARGETARCH usage further down). Provided automatically by BuildKit.
ARG TARGETARCH

# Prevent Python from writing cached compiled .pyc tracks onto the container image
ENV PYTHONTONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies, including Rust, Cargo, ALSA, SoX, usbutils, and RTL-SDR packages
RUN apt-get update && apt-get install -y \
    ffmpeg \
    alsa-utils \
    usbutils \
    build-essential \
    libffi-dev \
    cargo \
    rustc \
    cmake \
    git \
    pkg-config \
    libusb-1.0-0-dev \
    libasound2-dev \
    sox \
    libsox-fmt-all \
    rtl-sdr \
    librtlsdr-dev \
    docker.io \
    curl \
    && rm -rf /var/lib/apt/lists/*

# --- Docker Compose v2 CLI plugin ---------------------------------------
# `docker.io` (the Debian apt package, as opposed to Docker Inc.'s own repo)
# ships the base `docker` CLI WITHOUT the Compose plugin, and there is no
# `docker-compose-plugin` package in plain Debian's repos either (that name
# only exists in Docker's own apt repo, which we're intentionally not
# adding here just for this one plugin). Instead, fetch the official
# compose binary directly from GitHub releases and install it as a CLI
# plugin -- this is the same binary `docker-compose-plugin` would have
# installed, just fetched directly rather than through an extra apt repo.
#
# TARGETARCH (amd64/arm64/arm) is BuildKit's own arch identifier and does
# NOT match docker/compose's release asset naming (x86_64/aarch64/armv7),
# hence the mapping below -- this is what makes the image build correctly
# on both a Raspberry Pi (arm64) and an x86_64 host with zero configuration.
#
# Version is pinned for reproducible builds; bump deliberately rather than
# tracking "latest". Verified working (both archs) as of this Dockerfile change.
ARG DOCKER_COMPOSE_VERSION=v5.5.1
RUN case "${TARGETARCH}" in \
      amd64) COMPOSE_ARCH=x86_64 ;; \
      arm64) COMPOSE_ARCH=aarch64 ;; \
      arm)   COMPOSE_ARCH=armv7 ;; \
      *) echo "Unsupported TARGETARCH for docker-compose: ${TARGETARCH}" && exit 1 ;; \
    esac && \
    mkdir -p /usr/local/lib/docker/cli-plugins && \
    curl -fSL "https://github.com/docker/compose/releases/download/${DOCKER_COMPOSE_VERSION}/docker-compose-linux-${COMPOSE_ARCH}" \
      -o /usr/local/lib/docker/cli-plugins/docker-compose && \
    chmod +x /usr/local/lib/docker/cli-plugins/docker-compose && \
    /usr/local/lib/docker/cli-plugins/docker-compose version

WORKDIR /app

# Install Python requirements including PyNaCl, encryption tools, and davey protocol layers
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir discord.py PyNaCl davey pytest pytest-cov numpy

# Copy the application repository files into the image workspace layer
COPY . .

# Expose our structural state storage folder for volume hooks
RUN mkdir -p /data

# Run the script
CMD ["python", "bot.py"]
