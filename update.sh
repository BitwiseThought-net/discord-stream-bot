#!/bin/bash

echo "" > bot.py
nano bot.py
docker compose down
docker compose up -d --build
docker compose logs -f discord-bot
