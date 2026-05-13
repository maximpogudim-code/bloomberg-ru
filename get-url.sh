#!/bin/bash
# Показывает текущий URL туннеля bloomberg-proxy
LOG=/tmp/cloudflared-bloomberg-err.log

URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$LOG" 2>/dev/null | tail -1)

if [ -z "$URL" ]; then
    echo "Туннель ещё не запущен или URL не найден."
    echo "Запусти: launchctl load ~/Library/LaunchAgents/com.cloudflared-bloomberg.plist"
else
    echo "Bloomberg Proxy URL:"
    echo ""
    echo "  $URL"
    echo ""
    echo "Открой с телефона: $URL/news"
fi
