#!/bin/bash
# Bloomberg Terminal RU — запуск одним кликом (macOS)
# Двойной клик — и сайт откроется в браузере.

# Переходим в папку со скриптом (важно для .venv)
cd "$(dirname "$0")"

echo "============================================"
echo "  Bloomberg Terminal на русском — запуск"
echo "============================================"
echo ""

# ── 1. Проверяем python3 ──────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo "ОШИБКА: Python не найден на этом компьютере."
    echo ""
    echo "Пожалуйста, установите Python:"
    echo "  1. Откройте браузер и перейдите на https://python.org/downloads"
    echo "  2. Скачайте и установите Python 3 (зелёная кнопка)"
    echo "  3. После установки снова двойной клик на START.command"
    echo ""
    read -p "Нажмите Enter, чтобы закрыть это окно..."
    exit 1
fi

PYTHON=$(command -v python3)
echo "Python найден: $PYTHON"
echo ""

# ── 2. Создаём виртуальное окружение, если нет ───────────────────────────
if [ ! -d ".venv" ]; then
    echo "Первый запуск — устанавливаем программы..."
    echo "(Это займёт 5–10 минут, один раз)"
    echo ""
    "$PYTHON" -m venv .venv
    if [ $? -ne 0 ]; then
        echo "ОШИБКА: не удалось создать виртуальное окружение."
        read -p "Нажмите Enter, чтобы закрыть..."
        exit 1
    fi
    echo "Установка зависимостей..."
    .venv/bin/pip install --upgrade pip --quiet
    .venv/bin/pip install -r requirements.txt
    if [ $? -ne 0 ]; then
        echo "ОШИБКА при установке. Попробуйте запустить ещё раз."
        read -p "Нажмите Enter, чтобы закрыть..."
        exit 1
    fi
    echo ""
    echo "Установка завершена!"
fi

# ── 3. Playwright (best-effort, не падаем при ошибке) ────────────────────
if .venv/bin/python -c "import playwright" &>/dev/null; then
    echo "Устанавливаем Chromium для полных статей (~150 МБ, один раз)..."
    .venv/bin/playwright install chromium 2>/dev/null || true
fi

# ── 4. Убиваем старый процесс на порту 8080 ──────────────────────────────
OLD_PID=$(lsof -ti :8080 2>/dev/null)
if [ -n "$OLD_PID" ]; then
    echo "Останавливаем предыдущий запуск (PID $OLD_PID)..."
    kill "$OLD_PID" 2>/dev/null
    sleep 1
fi

# ── 5. Запускаем сервер ───────────────────────────────────────────────────
echo ""
echo "Запускаем сервер Bloomberg..."
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8080 &
SERVER_PID=$!

# ── 6. Ждём готовности (до 60 секунд) ────────────────────────────────────
echo "Ожидаем запуска (до 60 секунд)..."
READY=0
for i in $(seq 1 60); do
    sleep 1
    if curl -sf http://localhost:8080/health &>/dev/null; then
        READY=1
        break
    fi
    # Проверяем, что процесс ещё жив
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo ""
        echo "ОШИБКА: сервер неожиданно завершился."
        read -p "Нажмите Enter, чтобы закрыть..."
        exit 1
    fi
    printf "."
done
echo ""

if [ $READY -eq 0 ]; then
    echo "ОШИБКА: сервер не ответил за 60 секунд."
    kill "$SERVER_PID" 2>/dev/null
    read -p "Нажмите Enter, чтобы закрыть..."
    exit 1
fi

# ── 7. Открываем браузер ─────────────────────────────────────────────────
open http://localhost:8080

echo ""
echo "============================================"
echo "  Сайт работает! Браузер открыт."
echo ""
echo "  НЕ ЗАКРЫВАЙТЕ это окно —"
echo "  оно держит сервер запущенным."
echo ""
echo "  Чтобы выключить сайт — просто"
echo "  закройте это окно."
echo "============================================"
echo ""

# ── 8. Держим окно открытым (сервер работает, пока окно живёт) ───────────
wait "$SERVER_PID"
echo ""
echo "Сервер остановлен. Окно можно закрыть."
