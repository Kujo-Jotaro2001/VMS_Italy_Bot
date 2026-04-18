"""
VMS Italy — монитор свободных слотов на подачу документов.

Поток:
  1. Открыть форму «Информация о записи» напрямую
  2. Ввести номер записи + паспорта (посимвольно — поле с jquery-маской)
  3. Кликнуть капчу:
       - если прошла сразу (только чекбокс) — хорошо
       - если появился image challenge — решаем YOLOv8
  4. Нажать «Далее», перейти на «Назначить другую дату»
  5. На странице переноса кликать капчу каждые ~55 сек
  6. Если слот найден — нажать кнопку переноса + уведомить Telegram
  7. При истечении сессии — перелогиниться автоматически
"""

import asyncio
import logging
import random
import httpx
from datetime import datetime
from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout
from playwright_stealth import stealth_async


async def human_delay(lo: float = 0.4, hi: float = 1.4) -> None:
    await asyncio.sleep(random.uniform(lo, hi))


# Текущая позиция мыши (Playwright её не отдаёт — трекаем сами)
_mouse_pos = {"x": 0.0, "y": 0.0}


async def _bezier_move(page: Page, tx: float, ty: float) -> None:
    """
    Движение мыши по кривой Безье с переменной скоростью.
    Гораздо реалистичнее чем mouse.move(steps=N) (линейная интерполяция).
    """
    sx, sy = _mouse_pos["x"], _mouse_pos["y"]
    # Контрольные точки — случайные отклонения от прямой
    dx, dy = tx - sx, ty - sy
    dist = max((dx * dx + dy * dy) ** 0.5, 1.0)
    # Перпендикуляр к отрезку
    nx, ny = -dy / dist, dx / dist
    offset1 = random.uniform(-0.25, 0.25) * dist
    offset2 = random.uniform(-0.25, 0.25) * dist
    c1x = sx + dx * 0.33 + nx * offset1
    c1y = sy + dy * 0.33 + ny * offset1
    c2x = sx + dx * 0.66 + nx * offset2
    c2y = sy + dy * 0.66 + ny * offset2

    steps = max(int(dist / random.uniform(6, 12)), 8)
    steps = min(steps, 60)

    for i in range(1, steps + 1):
        t = i / steps
        # Ease-in-out — медленный старт/финиш
        t = t * t * (3 - 2 * t)
        mt = 1 - t
        x = mt**3 * sx + 3*mt**2*t*c1x + 3*mt*t**2*c2x + t**3 * tx
        y = mt**3 * sy + 3*mt**2*t*c1y + 3*mt*t**2*c2y + t**3 * ty
        try:
            await page.mouse.move(x, y)
        except Exception:
            return
        _mouse_pos["x"], _mouse_pos["y"] = x, y
        # Джиттер времени — человек не двигает мышь с постоянной скоростью
        await asyncio.sleep(random.uniform(0.005, 0.018))


async def human_delay(lo: float = 0.4, hi: float = 1.4) -> None:
    await asyncio.sleep(random.uniform(lo, hi))


async def human_mouse_move(page: Page) -> None:
    """Случайное движение мыши по странице (через Безье)."""
    w, h = 1280, 800
    tx = random.uniform(50, w - 50)
    ty = random.uniform(50, h - 50)
    await _bezier_move(page, tx, ty)


async def human_scroll(page: Page) -> None:
    """Плавный скролл небольшим шагом (туда-сюда)."""
    try:
        for _ in range(random.randint(1, 3)):
            dy = random.randint(80, 240) * random.choice([-1, 1])
            await page.mouse.wheel(0, dy)
            await asyncio.sleep(random.uniform(0.3, 0.9))
    except Exception:
        pass


async def human_click(page: Page, element) -> None:
    """Клик по кривой Безье в случайную точку элемента."""
    try:
        box = await element.bounding_box()
        if box is None:
            await element.click()
            return
        tx = box["x"] + box["width"] * random.uniform(0.25, 0.75)
        ty = box["y"] + box["height"] * random.uniform(0.3, 0.7)
        await _bezier_move(page, tx, ty)
        await asyncio.sleep(random.uniform(0.08, 0.28))
        await page.mouse.click(tx, ty)
    except Exception:
        try:
            await element.click()
        except Exception:
            pass

import bot_state
from captcha_solver import solve_image_challenge
from audio_solver import solve_audio_challenge, is_captcha_blocked
from config import (
    BOOKING_NUMBER, PASSPORT_NUMBER,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    SITE_URL, NO_SLOTS_TEXT, HEADLESS,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

INFO_URL = "https://italyvms.com/autoform/info.htm?lang=ru"

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

async def tg(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
            })
            if r.status_code != 200:
                log.warning("Telegram ответил %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("Не смог отправить в Telegram: %s", e)

def ts() -> str:
    return datetime.now().strftime("%d.%m %H:%M:%S")


def _tg_send_sync(text: str) -> None:
    """Синхронная отправка в Telegram через urllib (без httpx/asyncio)."""
    import urllib.request
    import json as _json
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    body = _json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
    except Exception as e:
        log.warning("TG send: %s: %s", type(e).__name__, e)


def telegram_command_listener_sync() -> None:
    """
    СИНХРОННЫЙ long-polling TG-листенер.
    Запускается в отдельном потоке — не трогает asyncio/Playwright event loop.
    Старые сообщения фильтруются по дате (msg.date >= startup).
    """
    import urllib.request
    import urllib.parse
    import json as _json
    import time as _time

    base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    offset = None
    startup_ts = int(_time.time())
    log.info("TG listener: старт, игнорирую сообщения старше %d", startup_ts)

    while True:
        try:
            params = {"timeout": "10"}
            if offset is not None:
                params["offset"] = str(offset)
            url = f"{base_url}/getUpdates?{urllib.parse.urlencode(params)}"
            # socket timeout должен быть > long-poll timeout
            with urllib.request.urlopen(url, timeout=20) as r:
                data = _json.loads(r.read())
            if not data.get("ok"):
                log.warning("TG getUpdates not ok: %s", data)
                _time.sleep(5)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                msg_date = msg.get("date", 0)
                if msg_date < startup_ts:
                    continue  # старое — игнорируем
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = (msg.get("text") or "").strip().lower()
                log.info("TG upd: chat_id=%s text=%r (ожидаю %s)", chat_id, text, TELEGRAM_CHAT_ID)
                if chat_id != str(TELEGRAM_CHAT_ID):
                    log.warning("TG: chat_id %s не совпадает с %s — игнорирую", chat_id, TELEGRAM_CHAT_ID)
                    continue
                log.info("TG команда принята: %s", text)
                if text in ("/status", "/stats", "status"):
                    _tg_send_sync(bot_state.status_text())
                elif text in ("/ping", "ping"):
                    _tg_send_sync(f"🏓 pong [{ts()}]")
        except Exception as e:
            err = str(e).lower()
            if "timed out" in err or "timeout" in err:
                log.debug("TG listener: idle timeout — переподключаюсь")
            else:
                log.warning("TG listener: %s: %s — жду 5с", type(e).__name__, e)
                _time.sleep(5)

# ---------------------------------------------------------------------------
# Заполнение поля с маской (jquery.maskedinput)
# ---------------------------------------------------------------------------

async def type_masked(page: Page, selector: str, value: str) -> None:
    field = page.locator(selector)
    await human_click(page, field)
    await human_delay(0.5, 1.2)
    await field.press("Control+a")
    await asyncio.sleep(random.uniform(0.1, 0.25))
    await field.press("Delete")
    await asyncio.sleep(random.uniform(0.3, 0.7))
    # Печатаем посимвольно с человеческим ритмом
    for ch in value:
        await field.press(ch)
        # Основная задержка: 120-280ms (человек печатает ~4-5 симв/сек)
        await asyncio.sleep(random.uniform(0.12, 0.28))
        # 10% шанс "задуматься" на 0.4-1.2с
        if random.random() < 0.10:
            await asyncio.sleep(random.uniform(0.4, 1.2))

# ---------------------------------------------------------------------------
# Капча
# ---------------------------------------------------------------------------

async def captcha_is_checked(page: Page) -> bool:
    try:
        frame = page.frame_locator('iframe[title*="reCAPTCHA"]').first
        aria = await frame.locator("#recaptcha-anchor").get_attribute("aria-checked", timeout=3_000)
        return aria == "true"
    except Exception:
        return False

async def image_challenge_present(page: Page) -> bool:
    """Проверяем появился ли iframe с картинками."""
    try:
        challenge = page.frame_locator('iframe[src*="bframe"]')
        return await challenge.locator("table.rc-imageselect-table").is_visible(timeout=2_000)
    except Exception:
        return False

async def solve_captcha(page: Page, context: str = "") -> bool:
    """
    1. Кликает чекбокс
    2. Если прошло — возвращает True
    3. Если появился image challenge — решает через YOLOv8
    """
    label = f" ({context})" if context else ""

    # Клик по чекбоксу — через iframe координаты (mouse.move внутрь frame не умеет,
    # но случайные движения + задержки перед кликом всё равно помогают)
    try:
        await human_mouse_move(page)
        await human_delay(0.8, 2.2)
        frame = page.frame_locator('iframe[title*="reCAPTCHA"]').first
        checkbox = frame.locator("#recaptcha-anchor")
        await checkbox.wait_for(state="visible", timeout=8_000)
        await human_delay(0.4, 1.1)
        await checkbox.click()
        log.info("Кликнул капчу%s", label)
        await asyncio.sleep(random.uniform(2.8, 4.2))
    except Exception as e:
        log.warning("Не смог кликнуть капчу%s: %s", label, e)
        return False

    # Прошла с первого клика?
    if await captcha_is_checked(page):
        log.info("Капча прошла автоматически ✅")
        return True

    # Challenge → только аудио, без YOLO-fallback
    if await image_challenge_present(page):
        log.info("Challenge появился — пробую аудио-режим (Whisper)…")
        ok = await solve_audio_challenge(page)
        if ok:
            return True
        log.warning("Аудио не сработало — вернём False, вызывающий код перезагрузит страницу")
        return False

    log.warning("Капча не прошла и challenge не обнаружен%s", label)
    return False

# ---------------------------------------------------------------------------
# Авторизация
# ---------------------------------------------------------------------------

async def login(page: Page, max_attempts: int = 4) -> bool:
    for attempt in range(1, max_attempts + 1):
        log.info("Открываю форму входа (попытка %d/%d)…", attempt, max_attempts)
        await page.goto(INFO_URL, wait_until="domcontentloaded")

        # 1. Долгий dwell — человек читает заголовок/инструкции ~4-8с
        await human_delay(4.0, 8.0)
        await human_mouse_move(page)
        await human_scroll(page)
        await human_delay(1.5, 3.5)

        try:
            # 2. Заполняем appnum
            await type_masked(page, "input[name='appnum']", BOOKING_NUMBER)
            # 3. «Проверяем» что набрали — пауза + движение мыши куда-то в сторону
            await human_delay(1.2, 2.8)
            await human_mouse_move(page)
            await human_delay(0.8, 2.0)
            # 4. Заполняем passnum
            await type_masked(page, "input[name='passnum']", PASSPORT_NUMBER)
            # 5. «Читаем заполненное» + лёгкий скролл
            await human_delay(1.5, 3.2)
            await human_scroll(page)
            await human_delay(1.0, 2.5)
            log.info("Форма заполнена")
        except Exception as e:
            log.error("Не смог заполнить форму: %s", e)
            return False

        if not await captcha_is_checked(page):
            ok = await solve_captcha(page, "форма входа")
            if not ok:
                # Если Google выкинул предупреждение — reload и перезаход
                if await is_captcha_blocked(page) and attempt < max_attempts:
                    bot_state.on_blocked()
                    cooldown = random.uniform(8.0, 15.0)
                    log.warning(
                        "Блокировка «Повторите попытку позже» — жду %.1fс и перезагружаю",
                        cooldown,
                    )
                    await asyncio.sleep(cooldown)
                    continue
                log.error("Капча не решена")
                return False

        try:
            # 6. Пауза перед «Далее» — человек смотрит на форму прежде чем отправить
            await human_delay(1.5, 3.5)
            submit_btn = page.locator("input[type='submit']").first
            await human_click(page, submit_btn)
        except Exception as e:
            log.error("Не смог нажать «Далее»: %s", e)
            return False

        await asyncio.sleep(random.uniform(2.5, 4.0))
        break

    if await page.locator("text=Номер записи").is_visible(timeout=6_000):
        log.info("Вошли успешно ✅")
        return True

    log.error("После входа оказались не там: %s", page.url)
    return False

# ---------------------------------------------------------------------------
# Переход на страницу переноса
# ---------------------------------------------------------------------------

async def go_to_reschedule(page: Page) -> bool:
    try:
        await human_delay(1.2, 2.8)
        await human_mouse_move(page)
        await human_delay(0.5, 1.4)
        link = page.locator("text=Назначить другую дату").first
        await link.wait_for(state="visible", timeout=8_000)
        await human_click(page, link)
        await asyncio.sleep(random.uniform(2.0, 3.5))
        log.info("Открыта страница переноса даты")
        return True
    except Exception as e:
        log.error("Не нашёл кнопку переноса: %s", e)
        return False

# ---------------------------------------------------------------------------
# Проверка наличия слотов
# ---------------------------------------------------------------------------

async def slots_available(page: Page) -> bool:
    content = await page.content()
    return NO_SLOTS_TEXT.lower() not in content.lower()

# ---------------------------------------------------------------------------
# Нажать кнопку подтверждения переноса
# ---------------------------------------------------------------------------

async def confirm_reschedule(page: Page) -> bool:
    try:
        await human_delay(0.6, 1.5)
        btn = page.locator("input[type='submit'], button:has-text('Назначить')").first
        await btn.wait_for(state="visible", timeout=5_000)
        await human_click(page, btn)
        log.info("Нажал кнопку подтверждения переноса")
        return True
    except Exception as e:
        log.warning("Не смог нажать кнопку переноса: %s", e)
        return False

# ---------------------------------------------------------------------------
# Основной цикл мониторинга
# ---------------------------------------------------------------------------

SLOT_CHECK_INTERVAL = 5   # проверять слоты и галку каждые 5 секунд
MOUSE_MOVE_INTERVAL = 20  # двигать мышь каждые ~20 секунд (чтобы не стоять мёртво)

async def monitor_loop(page: Page) -> None:
    log.info("Мониторинг запущен (проверка каждые %sс)…", SLOT_CHECK_INTERVAL)
    await tg(f"▶️ Мониторинг запущен [{ts()}]")

    # Решаем капчу сразу при входе
    if not await captcha_is_checked(page):
        ok = await solve_captcha(page, "страница переноса")
        if not ok:
            raise RuntimeError("session_expired")

    ticks_since_mouse = 0

    while True:
        await asyncio.sleep(SLOT_CHECK_INTERVAL)

        # Проверяем сессию
        if SITE_URL.rstrip("/") == page.url.rstrip("/") or "info.htm" in page.url:
            raise RuntimeError("session_expired")

        # Галка слетела → сразу решаем капчу
        if not await captcha_is_checked(page):
            log.info("Галка слетела — решаю капчу…")
            reschedule_url = page.url
            ok = await solve_captcha(page, "страница переноса")
            if not ok:
                log.warning("Капча не решилась — перезагружаю reschedule-страницу и пробую ещё раз…")
                await tg(f"⚠️ Капча не прошла — перезагружаю страницу [{ts()}]")
                await asyncio.sleep(5)
                await page.goto(reschedule_url, wait_until="domcontentloaded")
                await asyncio.sleep(3)
                ok2 = await solve_captcha(page, "страница переноса (после reload)")
                if not ok2:
                    log.warning("Повторная попытка тоже провалилась — перелогиниваюсь")
                    raise RuntimeError("session_expired")

        # Проверяем слоты
        has_slots = await slots_available(page)
        bot_state.on_slot_check(has_slots)
        if has_slots:
            msg = f"🎉 НАЙДЕН СВОБОДНЫЙ СЛОТ! [{ts()}]\n{page.url}"
            log.info(msg)
            await tg(msg)
            pressed = await confirm_reschedule(page)
            if pressed:
                await tg(f"✅ Нажал кнопку «Назначить другую дату» [{ts()}] — проверь сайт!")
            else:
                await tg(f"⚠️ Не смог нажать автоматически — зайди вручную немедленно!")
            await asyncio.sleep(10)
        else:
            log.info("Слотов нет")

        # Случайное движение мыши раз в ~20с, чтобы не выглядеть мёртво
        ticks_since_mouse += 1
        if ticks_since_mouse >= MOUSE_MOVE_INTERVAL // SLOT_CHECK_INTERVAL:
            await human_mouse_move(page)
            ticks_since_mouse = 0

# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.firefox.launch(
            headless=HEADLESS,
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="ru-RU",
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        await stealth_async(page)

        # TG listener синхронный в отдельном потоке
        import threading
        threading.Thread(target=telegram_command_listener_sync, daemon=True).start()

        while True:
            try:
                if not await login(page):
                    log.error("Не смог войти, жду 60 сек")
                    await tg(f"❌ Ошибка входа [{ts()}], повторяю через 60 сек")
                    await asyncio.sleep(60)
                    continue

                if not await go_to_reschedule(page):
                    await asyncio.sleep(30)
                    continue

                await monitor_loop(page)

            except RuntimeError as e:
                if "session_expired" in str(e):
                    log.info("Сессия истекла, перелогиниваемся")
                    await asyncio.sleep(5)
                    continue
                raise
            except PWTimeout:
                log.warning("Таймаут Playwright, перезапускаю")
                await asyncio.sleep(10)
                continue
            except KeyboardInterrupt:
                log.info("Остановлено пользователем")
                await tg(f"⏹ Мониторинг остановлен [{ts()}]")
                break
            except Exception as e:
                log.exception("Неожиданная ошибка: %s", e)
                await tg(f"💥 Ошибка [{ts()}]: {e}\nПерезапускаю через 30 сек")
                await asyncio.sleep(30)

        await browser.close()
        # listener_thread — daemon, завершится сам при выходе


if __name__ == "__main__":
    asyncio.run(main())
