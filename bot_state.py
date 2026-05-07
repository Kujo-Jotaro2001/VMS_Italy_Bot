"""
Общее состояние бота — счётчики и последняя инфа.
Импортируется из bot.py и audio_solver.py.
"""

from datetime import datetime
from typing import Optional

_started_at: datetime = datetime.now()
_solved_count: int = 0
_last_audio_text: str = ""
_last_solved_at: Optional[datetime] = None
_last_check_at: Optional[datetime] = None
_last_slot_status: str = "неизвестно"
_last_http_preview: str = "—"
_blocked_count: int = 0
_current_vpn: str = "—"


def on_captcha_solved(audio_text: str = "") -> None:
    global _solved_count, _last_audio_text, _last_solved_at
    _solved_count += 1
    if audio_text:
        _last_audio_text = audio_text
    _last_solved_at = datetime.now()


def solved_count() -> int:
    return _solved_count


def on_vpn_rotated(name: str) -> None:
    global _current_vpn
    _current_vpn = name


def on_blocked() -> None:
    global _blocked_count
    _blocked_count += 1


def on_slot_check(has_slots: bool, http_preview: str = "") -> None:
    global _last_check_at, _last_slot_status, _last_http_preview
    _last_check_at = datetime.now()
    _last_slot_status = "есть свободные" if has_slots else "нет"
    if http_preview:
        _last_http_preview = http_preview


def status_text() -> str:
    now = datetime.now()
    uptime = now - _started_at
    hours, rem = divmod(int(uptime.total_seconds()), 3600)
    minutes, _ = divmod(rem, 60)

    def fmt(dt: Optional[datetime]) -> str:
        if dt is None:
            return "—"
        return dt.strftime("%d.%m %H:%M:%S")

    return (
        f"📊 <b>Статус бота</b>\n"
        f"⏱ Работает: {hours}ч {minutes}м\n"
        f"✅ Решено капчей: <b>{_solved_count}</b>\n"
        f"⛔ Блокировок: {_blocked_count}\n"
        f"🕒 Последняя капча: {fmt(_last_solved_at)}\n"
        f"🎙 Последний аудио-текст: <code>{_last_audio_text or '—'}</code>\n"
        f"🔍 Последняя проверка слотов: {fmt(_last_check_at)}\n"
        f"📦 Слоты: {_last_slot_status}\n"
        f"📝 Ответ сервера: <code>{_last_http_preview}</code>\n"
        f"🌍 VPN: {_current_vpn}"
    )
