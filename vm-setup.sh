#!/bin/bash
# Запускать ВНУТРИ VM: bash vm-setup.sh
# Устанавливает Docker, копирует проект, настраивает автозапуск.

set -e
export DEBIAN_FRONTEND=noninteractive

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║       Bloomberg Proxy — VM Setup Script          ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# ── 1. Обновление системы ──────────────────────────────
echo "[1/6] Обновление системы..."
sudo apt-get update -qq
sudo apt-get upgrade -y -qq
sudo apt-get install -y -qq \
    curl wget git rsync \
    ca-certificates gnupg lsb-release \
    openssh-server

# ── 2. Установка Docker ────────────────────────────────
echo "[2/6] Установка Docker..."
if ! command -v docker &>/dev/null; then
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker "$USER"
    sudo systemctl enable docker
    sudo systemctl start docker
    echo "Docker установлен"
else
    echo "Docker уже установлен: $(docker --version)"
fi

# ── 3. Директория проекта ─────────────────────────────
echo "[3/6] Создание директории /app/bloomberg-proxy..."
sudo mkdir -p /app/bloomberg-proxy
sudo chown -R "$USER:$USER" /app

# ── 4. Playwright зависимости (для Docker образа) ─────
echo "[4/6] Установка зависимостей Playwright..."
sudo apt-get install -y -qq \
    libglib2.0-0 libnss3 libnspr4 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libdrm2 libdbus-1-3 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libasound2 libpango-1.0-0 libcairo2

# ── 5. Автозапуск приложения при старте системы ───────
echo "[5/6] Настройка автозапуска..."
sudo tee /etc/systemd/system/bloomberg-proxy.service > /dev/null <<'SERVICE'
[Unit]
Description=Bloomberg Proxy Docker App
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/app/bloomberg-proxy
ExecStart=/usr/bin/docker compose -f docker-compose.cloudflare.yml up -d
ExecStop=/usr/bin/docker compose -f docker-compose.cloudflare.yml down
User=ubuntu

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable bloomberg-proxy.service

# ── 6. Финальная проверка ─────────────────────────────
echo "[6/6] Проверка установки..."
docker --version
docker compose version

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║  Готово! Следующий шаг:                          ║"
echo "║                                                  ║"
echo "║  1. Скопируй проект:                             ║"
echo "║     rsync (с Mac, см. transfer.sh)               ║"
echo "║                                                  ║"
echo "║  2. Добавь CLOUDFLARE_TUNNEL_TOKEN в .env        ║"
echo "║                                                  ║"
echo "║  3. Запусти:                                     ║"
echo "║     cd /app/bloomberg-proxy                      ║"
echo "║     docker compose -f docker-compose.cloudflare.yml up -d ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""
