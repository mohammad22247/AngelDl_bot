#!/bin/bash
# Set the bot token + admin IDs + force-join channel in .env
# (run ON THE SERVER; values are typed here, never stored in git).
# Creates .env from .env.example if missing.
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV="$DIR/.env"
[ -f "$ENV" ] || cp "$DIR/.env.example" "$ENV"
read -rsp "Paste the bot token, then Enter: " T; echo
sed -i "s|^BOT_TOKEN=.*|BOT_TOKEN=$T|" "$ENV"
read -p "Admin numeric Telegram ID (e.g. 5601964971): " A
sed -i "s|^ADMIN_IDS=.*|ADMIN_IDS=$A|" "$ENV"
read -p "Force-join channel [@ange1Vpn]: " C
C="${C:-@ange1Vpn}"
sed -i "s|^FORCE_CHANNELS=.*|FORCE_CHANNELS=$C|" "$ENV"
chmod 600 "$ENV"
echo "Saved to $ENV (chmod 600). Restart the bot: sudo systemctl restart dlbot"
