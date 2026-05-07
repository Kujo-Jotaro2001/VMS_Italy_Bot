#!/bin/bash
cd "$(dirname "$0")"
while true; do
    echo "[$(date '+%H:%M:%S')] Запускаю бота…"
    venv/bin/python bot.py
    code=$?
    if [ $code -eq 0 ]; then
        echo "[$(date '+%H:%M:%S')] Бот завершился штатно — выхожу"
        break
    fi
    echo "[$(date '+%H:%M:%S')] Бот упал с кодом $code — перезапуск через 10с…"
    sleep 10
done
