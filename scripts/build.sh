#!/bin/bash

pushd ..

echo "Downing existing containers..."
docker compose down

git pull

echo "Building new images..."
docker compose build --no-cache

echo "Starting containers..."
docker compose up -d

echo "Confirm the socket mount landed:"

docker exec -it discord_audio_bot ls -l /var/run/docker.sock

echo "Running 'Second command'"
docker exec -it discord_audio_bot docker ps

echo "If the second command fails inside the container, the socket mount or the docker.io package didn't take — re-check docker-compose.yml and rebuild."
