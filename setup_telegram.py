"""
Запусти этот скрипт ОДИН РАЗ чтобы узнать свой Telegram chat_id.

Шаги:
  1. Создай бота у @BotFather в Telegram → получи TOKEN
  2. Вставь TOKEN ниже
  3. Напиши своему боту любое сообщение в Telegram
  4. Запусти этот скрипт: python setup_telegram.py
  5. Скопируй chat_id в config.py
"""

import httpx

TOKEN = "8525172381:AAFlfrm-pVYUkzIBdP7W6gEgTrFpyusvnt0"   # <-- вставь сюда токен от @BotFather

r = httpx.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates")
data = r.json()

if not data.get("ok"):
    print("Ошибка:", data)
elif not data["result"]:
    print("Нет сообщений. Напиши своему боту в Telegram что-нибудь и запусти снова.")
else:
    for upd in data["result"]:
        chat = upd.get("message", {}).get("chat", {})
        print(f"chat_id: {chat.get('id')}  имя: {chat.get('first_name')} {chat.get('last_name','')}")
