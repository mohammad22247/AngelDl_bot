#!/bin/bash
# Set the bot token in .env (run ON THE SERVER; the token is typed here,
# never stored in git). Creates .env from .env.example if missing.
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV="$DIR/.env"
[ -f "$ENV" ] || cp "$DIR/.env.example" "$ENV"
read -rsp "Paste the bot token, then Enter: " T; echo
sed -i "s|^BOT_TOKEN=.*|BOT_TOKEN=$T|" "$ENV"
chmod 600 "$ENV"
echo "Saved to $ENV (chmod 600). Restart the bot: sudo systemctl restart dlbot"
