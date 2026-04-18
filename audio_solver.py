"""
Решение reCAPTCHA v2 через аудио-челлендж + OpenAI Whisper.

Как работает:
  1. Переключаемся в аудио-режим (кнопка 🎧)
  2. Получаем URL mp3 файла (ссылка "Скачать аудио")
  3. Скачиваем mp3
  4. Whisper транскрибирует → текст (обычно английские цифры/слова)
  5. Вводим текст в input → жмём «Подтвердить»

Требования:
  pip install openai-whisper
  + ffmpeg в системе (winget install Gyan.FFmpeg на Windows)
"""

import asyncio
import logging
import os
import random
import tempfile
from datetime import datetime
from typing import Optional

import httpx

import bot_state

log = logging.getLogger(__name__)

DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captcha_debug")
os.makedirs(DEBUG_DIR, exist_ok=True)

# Модель Whisper (ленивая загрузка)
_whisper_model = None

def _load_whisper():
    """Загружает tiny.en модель Whisper — ~39МБ, быстрая, English-only."""
    global _whisper_model
    if _whisper_model is None:
        import whisper
        log.info("Загружаю Whisper tiny.en… (первый раз скачает ~39 МБ)")
        _whisper_model = whisper.load_model("tiny.en")
        log.info("Whisper загружен ✅")
    return _whisper_model

async def _human_delay(lo: float = 0.3, hi: float = 1.2) -> None:
    await asyncio.sleep(random.uniform(lo, hi))

async def _human_click(page, element) -> None:
    """Клик с движением мыши (антибот эмуляция)."""
    try:
        box = await element.bounding_box()
        if box is None:
            await element.click()
            return
        tx = box["x"] + box["width"]  * random.uniform(0.25, 0.75)
        ty = box["y"] + box["height"] * random.uniform(0.25, 0.75)
        await page.mouse.move(tx, ty, steps=random.randint(4, 7))
        await asyncio.sleep(random.uniform(0.08, 0.22))
        await page.mouse.click(tx, ty)
    except Exception:
        try:
            await element.click()
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Детекция блокировки («подозрительная активность»)
# ---------------------------------------------------------------------------

async def is_captcha_blocked(page) -> bool:
    """
    Проверяет появилось ли предупреждение Google.
    Ищет как в bframe-iframe, так и в попапе на основной странице
    («Повторите попытку позже» / «Try again later»).
    """
    BLOCK_RE = "text=/Повторите попытку|Try again later|автоматизированн|automated quer|подозрительн|suspicious/i"

    # Попап на основной странице (именно его видно на скриншоте)
    try:
        if await page.locator(BLOCK_RE).first.is_visible(timeout=1_500):
            return True
    except Exception:
        pass

    # Внутри bframe
    try:
        challenge = page.frame_locator('iframe[src*="bframe"]')
        if await challenge.locator(BLOCK_RE).first.is_visible(timeout=1_500):
            return True
    except Exception:
        pass

    return False

# ---------------------------------------------------------------------------
# Основная функция
# ---------------------------------------------------------------------------

async def solve_audio_challenge(page, _depth: int = 0) -> bool:
    """
    Переключается на аудио-режим и решает через Whisper.
    Возвращает True если капча пройдена.
    _depth — для лимита рекурсии при повторных челленджах.
    """
    MAX_DEPTH = 2
    if _depth > MAX_DEPTH:
        print(f"  ⛔ Слишком много повторных аудио-челленджей ({_depth}) — сдаюсь, нужен reload", flush=True)
        return False
    challenge = page.frame_locator('iframe[src*="bframe"]')

    # 1. Переключиться на аудио (если ещё не в нём)
    # Важно: "подумать" перед кликом — Google палит мгновенное переключение
    try:
        audio_btn = challenge.locator("#recaptcha-audio-button")
        if await audio_btn.is_visible(timeout=3_000):
            print("  🎧 Переключаюсь на аудио-режим (после паузы)…", flush=True)
            # "Посмотреть" на картинки перед переключением
            await _human_delay(2.5, 5.0)
            try:
                await page.mouse.move(
                    random.randint(200, 1080),
                    random.randint(200, 600),
                    steps=random.randint(6, 14),
                )
            except Exception:
                pass
            await _human_delay(0.8, 2.0)
            await _human_click(page, audio_btn)
            await _human_delay(2.0, 3.5)
    except Exception:
        pass  # Возможно уже в аудио-режиме

    # 2. Проверяем попап «Повторите попытку позже» / «подозрительная активность»
    if await is_captcha_blocked(page):
        print("  ⛔ Google показал блокировку — нужна перезагрузка страницы", flush=True)
        return False

    # 3. Ищем ссылку на скачивание mp3
    audio_url: Optional[str] = None
    for sel in [
        ".rc-audiochallenge-tdownload-link",
        'a[href*=".mp3"]',
        "a[download]",
    ]:
        try:
            href = await challenge.locator(sel).first.get_attribute("href", timeout=5_000)
            if href and (".mp3" in href or "payload" in href):
                audio_url = href
                break
        except Exception:
            pass

    if not audio_url:
        # Fallback — пытаемся достать через <audio src>
        try:
            audio_url = await challenge.locator("audio source, audio").first.get_attribute("src", timeout=3_000)
        except Exception:
            pass

    if not audio_url:
        print("  ❌ Не нашёл URL аудио-файла", flush=True)
        return False

    print(f"  🔗 Аудио URL: {audio_url[:100]}…", flush=True)

    # 4. Скачиваем mp3
    ts_tag = datetime.now().strftime("%H%M%S")
    mp3_path = os.path.join(DEBUG_DIR, f"{ts_tag}_audio.mp3")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(audio_url)
            r.raise_for_status()
            with open(mp3_path, "wb") as f:
                f.write(r.content)
        print(f"  💾 Скачал {len(r.content)} байт → {mp3_path}", flush=True)
    except Exception as e:
        print(f"  ❌ Не смог скачать mp3: {e}", flush=True)
        return False

    # 5. Транскрипция через Whisper
    try:
        model = _load_whisper()
        print("  🎙  Распознаю через Whisper…", flush=True)
        # Whisper возвращает dict с 'text'
        result = model.transcribe(mp3_path, language="en", fp16=False)
        text = (result.get("text") or "").strip().lower()
        # Убираем пунктуацию и лишние пробелы
        text = "".join(c for c in text if c.isalnum() or c.isspace()).strip()
        text = " ".join(text.split())
        print(f"  📝 Whisper распознал: '{text}'", flush=True)
    except Exception as e:
        print(f"  ❌ Ошибка Whisper: {e}", flush=True)
        return False

    if not text:
        print("  ❌ Пустая транскрипция", flush=True)
        return False

    # 6. Вводим текст в поле
    try:
        input_field = challenge.locator("#audio-response")
        try:
            await input_field.scroll_into_view_if_needed(timeout=3_000)
        except Exception:
            pass
        try:
            await input_field.click(timeout=5_000)
        except Exception:
            # поле может быть вне viewport — форс-клик
            await input_field.click(timeout=5_000, force=True)
        await _human_delay(0.3, 0.6)
        # Вводим посимвольно как человек
        for ch in text:
            await input_field.press(ch)
            await asyncio.sleep(random.uniform(0.05, 0.15))
        print("  ⌨️  Текст введён", flush=True)
    except Exception as e:
        print(f"  ❌ Не смог ввести текст: {e}", flush=True)
        return False

    await _human_delay(0.6, 1.4)

    # 7. Жмём «Подтвердить»
    try:
        verify = challenge.locator("#recaptcha-verify-button")
        await _human_click(page, verify)
        print("  ✅ Нажал «Подтвердить»", flush=True)
    except Exception as e:
        print(f"  ❌ Не смог нажать Подтвердить: {e}", flush=True)
        return False

    await _human_delay(3.0, 4.5)

    # 8. Проверяем результат
    main_frame = page.frame_locator('iframe[title*="reCAPTCHA"]').first
    try:
        aria = await main_frame.locator("#recaptcha-anchor").get_attribute(
            "aria-checked", timeout=3_000
        )
        if aria == "true":
            print("  🎉 Капча решена через аудио!", flush=True)
            bot_state.on_captcha_solved(text)
            return True
    except Exception:
        pass

    # Может просит ещё один аудио (редко) — проверим
    try:
        still_audio = await challenge.locator("#audio-response").is_visible(timeout=2_000)
        if still_audio:
            print(f"  🔁 Дали ещё один аудио-челлендж (глубина {_depth + 1})…", flush=True)
            await _human_delay(1.0, 2.0)
            return await solve_audio_challenge(page, _depth=_depth + 1)
    except Exception:
        pass

    print("  ❌ После ввода капча не прошла", flush=True)
    return False
