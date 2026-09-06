#!/bin/bash

echo "Confirm the audio bridge is alive on the host"

ps aux | grep "tail -f"
