#!/bin/bash

echo "" > stream_bot.py
nano stream_bot.py
docker compose down
docker compose up -d --build
docker compose logs -f discord-bot
