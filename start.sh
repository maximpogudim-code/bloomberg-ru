#!/bin/bash
# Bloomberg RU — полный запуск: сервер + Cloudflare туннель
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Убиваем старые процессы
lsof -ti :8080 | xargs kill -9 2>/dev/null || true
pkill -f cloudflared 2>/dev/null || true
sleep 1

# Запускаем FastAPI сервер
source "$DIR/.venv/bin/activate"
nohup uvicorn app:app --host 0.0.0.0 --port 8080 --workers 1 > /tmp/bloomberg-server.log 2>&1 &
SERVER_PID=$!
echo "Server PID: $SERVER_PID"

# Ждём пока сервер поднимется
until curl -s http://localhost:8080/health > /dev/null 2>&1; do sleep 1; done
echo "Server is up"

# Запускаем cloudflared и захватываем URL
"$DIR/cloudflared" tunnel --url http://localhost:8080 2>&1 | tee /tmp/cloudflared.log | while read -r line; do
    if echo "$line" | grep -q "trycloudflare.com"; then
        URL=$(echo "$line" | grep -o 'https://[^ ]*trycloudflare\.com')
        if [ -n "$URL" ]; then
            echo "$URL" > /tmp/bloomberg-url.txt
            echo "=== BLOOMBERG RU URL: $URL ===" | tee -a /tmp/bloomberg-server.log
            # Уведомление macOS
            osascript -e "display notification \"$URL\" with title \"Bloomberg RU\" subtitle \"Туннель активен\"" 2>/dev/null || true
        fi
    fi
done
