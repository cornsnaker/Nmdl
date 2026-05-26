#!/bin/bash
set -e

# Dynamically ensure directories exist upon volume mounting
mkdir -p "$DOWNLOAD_ROOT" "$(dirname "$SESSION_NAME")"

# Execute Python as PID 1
exec python -u bot.py
