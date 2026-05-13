# Инструкция по запуску сервера (Hetzner + DuckDNS)

## Шаг 1 — Создать сервер Hetzner (5 минут)

1. Зарегистрироваться: **hetzner.com/cloud**
2. Cloud → Projects → **+ Create Project** → Add Server
3. Настройки:
   - Location: **Nuremberg** (EU, быстро)
   - Image: **Ubuntu 22.04**
   - Type: **CX22** (4GB RAM, €4.35/мес) — минимум для Playwright
   - SSH Keys: загрузи `~/.ssh/id_rsa.pub`
4. Create Server → **запишите IP адрес**

---

## Шаг 2 — Бесплатный домен DuckDNS (2 минуты)

1. **duckdns.org** → Sign in with Google
2. subdomain: `bloomberg-ru` → **add domain**
3. В поле "current ip" вставь IP от Hetzner → **update ip**
4. Твой URL: `https://bloomberg-ru.duckdns.org`

---

## Шаг 3 — Настройка сервера (первый раз)

```bash
# Подключись к серверу
ssh root@<IP_СЕРВЕРА>

# Установить Docker
curl -fsSL https://get.docker.com | sh
systemctl enable docker

# Установить certbot для SSL
apt update && apt install -y certbot

# Получить SSL сертификат (домен должен уже указывать на сервер)
certbot certonly --standalone -d bloomberg-ru.duckdns.org

# Настроить автообновление SSL
echo "0 0 * * * root certbot renew --quiet --post-hook 'docker compose -f /app/bloomberg-proxy/docker-compose.yml restart nginx'" >> /etc/crontab

# Создать папку для проекта
mkdir -p /app/bloomberg-proxy
```

---

## Шаг 4 — Деплой проекта

На своём Mac (в папке bloomberg-proxy):

```bash
# Сделать скрипт деплоя исполняемым
chmod +x deploy.sh

# Деплой (замени IP на твой)
./deploy.sh 1.2.3.4
```

---

## Шаг 5 — Запустить Nginx

```bash
ssh root@<IP>
cd /app/bloomberg-proxy
docker compose up -d nginx
```

---

## Шаг 6 — Проверить

Открой в браузере: **https://bloomberg-ru.duckdns.org**

- Первая загрузка: ~15 сек (Playwright + Claude)
- Повторная: <1 сек (кэш)

---

## Предзагрев кэша (опционально)

```bash
# На сервере, после запуска
cd /app/bloomberg-proxy
docker compose exec app python warmup.py
```

---

## Обновление кода после изменений

```bash
# С твоего Mac:
./deploy.sh <IP_СЕРВЕРА>
```

---

## Мониторинг

```bash
# Логи в реальном времени
ssh root@<IP> "docker compose -f /app/bloomberg-proxy/docker-compose.yml logs -f app"

# Статистика кэша
curl https://bloomberg-ru.duckdns.org/api/cache/stats

# Здоровье сервиса
curl https://bloomberg-ru.duckdns.org/health
```

---

## Бюджет

| Статья | Стоимость |
|--------|-----------|
| Hetzner CX22 | €4.35/мес |
| Домен DuckDNS | $0 |
| SSL (Let's Encrypt) | $0 |
| Claude API (~30 статей/день) | ~$5-10/мес |
| **Итого** | **~€10-15/мес** |
