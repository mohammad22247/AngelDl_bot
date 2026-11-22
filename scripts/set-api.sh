#!/bin/bash
# Enter your Telegram app api_id + api_hash (from my.telegram.org) for the
# local Bot API server. Stored only in api.env (chmod 600), never in git.
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"
read -p "api_id: " API_ID
read -p "api_hash: " API_HASH
printf "API_ID=%s\nAPI_HASH=%s\n" "$API_ID" "$API_HASH" > "$DIR/api.env"
chmod 600 "$DIR/api.env"
echo "Saved to $DIR/api.env"
