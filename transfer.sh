#!/bin/bash
# Запускать на Mac для копирования проекта в VM
# Usage: ./transfer.sh
# VM должна быть запущена и SSH доступен на localhost:2222

set -e

VM_USER="ubuntu"
VM_HOST="localhost"
VM_PORT="2222"
VM_DEST="/app/bloomberg-proxy"

SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=10"

echo "╔══════════════════════════════════════════════════╗"
echo "║  Bloomberg Proxy → VM Transfer                   ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# Проверка доступности SSH
echo "[1/3] Проверка SSH соединения..."
if ! ssh $SSH_OPTS -p "$VM_PORT" "$VM_USER@$VM_HOST" "echo 'SSH OK'" 2>/dev/null; then
    echo "ERROR: Нет SSH доступа. Убедитесь что VM запущена и Ubuntu установлена."
    echo "Дождитесь перезагрузки VM после установки (~15 мин от старта)."
    exit 1
fi
echo "SSH: OK"

# Запуск vm-setup.sh внутри VM
echo "[2/3] Установка зависимостей внутри VM..."
ssh $SSH_OPTS -p "$VM_PORT" "$VM_USER@$VM_HOST" "bash -s" < vm-setup.sh
echo "Зависимости установлены"

# Копирование файлов проекта
echo "[3/3] Копирование проекта в VM..."
rsync -avz \
    --exclude='.venv' \
    --exclude='cache_data' \
    --exclude='__pycache__' \
    --exclude='.git' \
    -e "ssh $SSH_OPTS -p $VM_PORT" \
    ./ "$VM_USER@$VM_HOST:$VM_DEST/"
echo "Проект скопирован"

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║  Перенос завершён!                               ║"
echo "║                                                  ║"
echo "║  Следующий шаг — добавить Cloudflare токен:      ║"
echo "║                                                  ║"
echo "║  ssh -p 2222 ubuntu@localhost                    ║"
echo "║  nano /app/bloomberg-proxy/.env                  ║"
echo "║  # Добавить: CLOUDFLARE_TUNNEL_TOKEN=...         ║"
echo "║                                                  ║"
echo "║  Затем запустить:                                ║"
echo "║  cd /app/bloomberg-proxy                         ║"
echo "║  docker compose -f docker-compose.cloudflare.yml up -d --build ║"
echo "╚══════════════════════════════════════════════════╝"
