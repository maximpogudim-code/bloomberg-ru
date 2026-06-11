@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title Bloomberg Terminal на русском

echo ============================================
echo   Bloomberg Terminal на русском — запуск
echo ============================================
echo.

:: ── 1. Ищем Python ──────────────────────────────────────────────────────
set PYTHON_CMD=

:: Сначала пробуем команду py (Windows Launcher)
py --version >nul 2>&1
if %errorlevel%==0 (
    set PYTHON_CMD=py
    goto :python_found
)

:: Затем пробуем python
python --version >nul 2>&1
if %errorlevel%==0 (
    set PYTHON_CMD=python
    goto :python_found
)

:: Затем пробуем python3
python3 --version >nul 2>&1
if %errorlevel%==0 (
    set PYTHON_CMD=python3
    goto :python_found
)

echo ОШИБКА: Python не найден на этом компьютере.
echo.
echo Пожалуйста, установите Python:
echo   1. Откройте браузер и перейдите на https://python.org/downloads
echo   2. Скачайте Python 3 и запустите установщик
echo   3. ВАЖНО: поставьте галочку "Add Python to PATH" при установке!
echo   4. После установки перезапустите компьютер
echo   5. Снова двойной клик на START.bat
echo.
pause
exit /b 1

:python_found
:: Проверяем, что Python не старше 3.10
%PYTHON_CMD% -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if %errorlevel% neq 0 (
    echo ОШИБКА: установлен слишком старый Python — нужна версия 3.10 или новее.
    echo.
    echo Пожалуйста, установите свежий Python:
    echo   1. Откройте браузер и перейдите на https://python.org/downloads
    echo   2. Скачайте Python 3 и запустите установщик
    echo   3. ВАЖНО: поставьте галочку "Add Python to PATH" при установке!
    echo   4. Снова двойной клик на START.bat
    echo.
    pause
    exit /b 1
)
echo Python найден (%PYTHON_CMD%)
echo.

:: Переходим в папку скрипта
cd /d "%~dp0"

:: ── 2. Создаём виртуальное окружение, если нет или оно сломано ──────────
if exist ".venv\" if not exist ".venv\Scripts\uvicorn.exe" (
    echo Прошлая установка не завершилась — начинаем заново...
    rmdir /s /q .venv
)

if not exist ".venv\" (
    echo Первый запуск — устанавливаем программы...
    echo Это займёт 5–10 минут, один раз.
    echo.
    %PYTHON_CMD% -m venv .venv
    if %errorlevel% neq 0 (
        echo ОШИБКА: не удалось создать виртуальное окружение.
        pause
        exit /b 1
    )
    echo Установка зависимостей...
    .venv\Scripts\pip install --upgrade pip --quiet
    .venv\Scripts\pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo ОШИБКА при установке. Попробуйте запустить ещё раз.
        pause
        exit /b 1
    )
    echo.
    echo Устанавливаем дополнительные компоненты — если не получится, сайт всё равно заработает...
    .venv\Scripts\pip install -r requirements-optional.txt
    echo.
    echo Установка завершена!
    echo.
)

:: ── 3. Playwright best-effort ────────────────────────────────────────────
.venv\Scripts\python -c "import playwright" >nul 2>&1
if %errorlevel%==0 (
    echo Проверяем Chromium...
    .venv\Scripts\playwright install chromium >nul 2>&1
)

:: ── 4. Убиваем старый процесс на порту 8080 ─────────────────────────────
for /f "tokens=5" %%p in ('netstat -aon 2^>nul ^| findstr ":8080 "') do (
    taskkill /PID %%p /F >nul 2>&1
)

:: ── 5. Запускаем сервер ──────────────────────────────────────────────────
echo Запускаем сервер Bloomberg...
start /b .venv\Scripts\uvicorn app:app --host 127.0.0.1 --port 8080

:: ── 6. Ждём готовности (до 60 секунд) ───────────────────────────────────
echo Ожидаем запуска (до 60 секунд)...
set READY=0
for /l %%i in (1,1,60) do (
    if !READY!==0 (
        timeout /t 1 /nobreak >nul
        curl -sf http://localhost:8080/health >nul 2>&1
        if !errorlevel!==0 set READY=1
    )
)

if %READY%==0 (
    echo.
    echo ОШИБКА: сервер не ответил за 60 секунд.
    pause
    exit /b 1
)

:: ── 7. Открываем браузер ─────────────────────────────────────────────────
start http://localhost:8080

echo.
echo ============================================
echo   Сайт работает! Браузер открыт.
echo.
echo   НЕ ЗАКРЫВАЙТЕ это окно —
echo   оно держит сервер запущенным.
echo.
echo   Чтобы выключить сайт — просто
echo   закройте это окно.
echo ============================================
echo.

:: ── 8. Держим окно открытым ─────────────────────────────────────────────
:keepalive
timeout /t 5 /nobreak >nul
curl -sf http://localhost:8080/health >nul 2>&1
if %errorlevel%==0 goto :keepalive

echo Сервер остановлен.
pause
