#!/bin/bash
# Deploy script — run locally to push updates to the Hetzner server
# Usage: ./deploy.sh <SERVER_IP>
# Example: ./deploy.sh 1.2.3.4

set -e

SERVER_IP="${1:-}"
APP_DIR="/app/bloomberg-proxy"

if [ -z "$SERVER_IP" ]; then
  echo "Usage: ./deploy.sh <SERVER_IP>"
  exit 1
fi

echo "=== Bloomberg Proxy Deploy ==="
echo "Server: root@$SERVER_IP"
echo ""

# 1. Sync files to server (exclude cache, venv, .git)
echo "[1/3] Синхронизация файлов..."
rsync -avz --exclude='.git' --exclude='cache_data' --exclude='.venv' \
  ./ "root@$SERVER_IP:$APP_DIR/"

# 2. Restart app container (rebuild if Dockerfile changed)
echo "[2/3] Перезапуск Docker..."
ssh "root@$SERVER_IP" "cd $APP_DIR && docker compose up -d --build app"

# 3. Check health
echo "[3/3] Проверка здоровья..."
sleep 5
HEALTH=$(ssh "root@$SERVER_IP" "curl -sf http://localhost:8080/health || echo 'FAIL'")
echo "Health: $HEALTH"

echo ""
echo "=== Деплой завершён ==="
echo "Сайт: https://bloomberg-ru.duckdns.org"
