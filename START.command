#!/bin/bash
# Bloomberg Terminal RU — запуск одним кликом (macOS)
# Двойной клик — и сайт откроется в браузере.

# Переходим в папку со скриптом (важно для .venv)
cd "$(dirname "$0")"

echo "============================================"
echo "  Bloomberg Terminal на русском — запуск"
echo "============================================"
echo ""

# ── 1. Ищем Python 3.10 или новее ────────────────────────────────────────
# Системный python3 на macOS часто старый (3.9) — он не подходит.
PYTHON=""
for CAND in python3.13 python3.12 python3.11 python3.10 \
            /usr/local/bin/python3 /opt/homebrew/bin/python3 \
            /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
            /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
            /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 \
            /Library/Frameworks/Python.framework/Versions/3.10/bin/python3 \
            python3; do
    P=$(command -v "$CAND" 2>/dev/null) || continue
    if "$P" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
        PYTHON="$P"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ОШИБКА: нужен Python версии 3.10 или новее."
    echo ""
    echo "Пожалуйста, установите Python:"
    echo "  1. Откройте браузер и перейдите на https://python.org/downloads"
    echo "  2. Скачайте и установите Python 3 (жёлтая кнопка Download)"
    echo "  3. После установки снова двойной клик на START.command"
    echo ""
    read -p "Нажмите Enter, чтобы закрыть это окно..."
    exit 1
fi

echo "Python найден: $PYTHON ($("$PYTHON" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))'))"
echo ""

# ── 2. Создаём виртуальное окружение, если нет или оно сломано ───────────
# Если прошлая установка оборвалась — начинаем заново.
if [ -d ".venv" ] && [ ! -x ".venv/bin/uvicorn" ]; then
    echo "Прошлая установка не завершилась — начинаем заново..."
    rm -rf .venv
fi

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
    # Медленный интернет — не приговор: большой таймаут и до 3 попыток
    INSTALLED=0
    for ATTEMPT in 1 2 3; do
        if .venv/bin/pip install --timeout 120 --retries 10 -r requirements.txt; then
            INSTALLED=1
            break
        fi
        echo ""
        echo "Сбой сети при установке — пробуем ещё раз ($ATTEMPT из 3)..."
        sleep 5
    done
    if [ $INSTALLED -ne 1 ]; then
        echo "ОШИБКА при установке. Проверьте интернет и запустите ещё раз."
        read -p "Нажмите Enter, чтобы закрыть..."
        exit 1
    fi
    echo ""
    echo "Устанавливаем дополнительные компоненты (ТВ-озвучка, полные статьи)..."
    echo "(Если эта часть не установится — сайт всё равно будет работать)"
    .venv/bin/pip install --timeout 120 --retries 10 -r requirements-optional.txt || true
    echo ""
    echo "Установка завершена!"
fi

# ── 3. Playwright (best-effort, не падаем при ошибке) ────────────────────
if .venv/bin/python -c "import playwright" &>/dev/null; then
    if [ ! -d "$HOME/Library/Caches/ms-playwright" ]; then
        echo "Устанавливаем Chromium для полных статей (~150 МБ, один раз)..."
    fi
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
