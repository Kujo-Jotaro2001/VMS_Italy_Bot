"""Быстрый тест TG getUpdates — запусти отдельно, напиши /ping боту"""
import urllib.request
import urllib.parse
import json
import time

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
offset = None

print(f"Слушаю бота... (токен: ...{TELEGRAM_BOT_TOKEN[-10:]})")
print(f"Ожидаю chat_id: {TELEGRAM_CHAT_ID}")
print("Напиши /ping в TG-чат бота — должно появиться здесь\n")

while True:
    try:
        params = {"timeout": "10"}
        if offset is not None:
            params["offset"] = str(offset)
        url = f"{base_url}/getUpdates?{urllib.parse.urlencode(params)}"
        print(f"[{time.strftime('%H:%M:%S')}] Жду обновлений (offset={offset})…")
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.loads(r.read())
        updates = data.get("result", [])
        print(f"  → Получил {len(updates)} обновлений, ok={data.get('ok')}")
        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            print(f"  📨 update_id={upd['update_id']} chat_id={msg.get('chat',{}).get('id')} text={msg.get('text')!r} date={msg.get('date')}")
    except Exception as e:
        print(f"  ⚠️  {type(e).__name__}: {e}")
        time.sleep(3)
