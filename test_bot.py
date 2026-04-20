"""
Пошаговый тест бота (новый flow — прямая ссылка с токеном).

Шаги:
  1. Открыть TARGET_URL
  2. Решить капчу (аудио → Whisper)
  3. Подождать автоперезагрузки, проверить наличие слотов
  4. Мониторинг: галка капчи + слоты каждые 5с
  5. Если слоты найдены — нажать #next_button

Запуск: python test_bot.py
Скриншоты: папка screenshots/
"""

import asyncio
import logging
import os
import random
from playwright.async_api import async_playwright, Page
from playwright_stealth import stealth_async

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)

from config import TARGET_URL, NO_SLOTS_TEXT
from audio_solver import solve_audio_challenge
from bot import telegram_command_listener_sync

SCREENSHOTS_DIR = "screenshots"
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

_mouse_pos = {"x": 0.0, "y": 0.0}


async def _bezier_move(page: Page, tx: float, ty: float) -> None:
    sx, sy = _mouse_pos["x"], _mouse_pos["y"]
    dx, dy = tx - sx, ty - sy
    dist = max((dx * dx + dy * dy) ** 0.5, 1.0)
    nx, ny = -dy / dist, dx / dist
    offset1 = random.uniform(-0.25, 0.25) * dist
    offset2 = random.uniform(-0.25, 0.25) * dist
    c1x = sx + dx * 0.33 + nx * offset1
    c1y = sy + dy * 0.33 + ny * offset1
    c2x = sx + dx * 0.66 + nx * offset2
    c2y = sy + dy * 0.66 + ny * offset2
    steps = min(max(int(dist / random.uniform(6, 12)), 8), 60)
    for i in range(1, steps + 1):
        t = i / steps
        t = t * t * (3 - 2 * t)
        mt = 1 - t
        x = mt**3*sx + 3*mt**2*t*c1x + 3*mt*t**2*c2x + t**3*tx
        y = mt**3*sy + 3*mt**2*t*c1y + 3*mt*t**2*c2y + t**3*ty
        try:
            await page.mouse.move(x, y)
        except Exception:
            return
        _mouse_pos["x"], _mouse_pos["y"] = x, y
        await asyncio.sleep(random.uniform(0.005, 0.018))


async def human_mouse_move(page: Page) -> None:
    tx = random.uniform(50, 1230)
    ty = random.uniform(50, 750)
    await _bezier_move(page, tx, ty)


async def human_scroll(page: Page) -> None:
    try:
        for _ in range(random.randint(1, 3)):
            dy = random.randint(80, 240) * random.choice([-1, 1])
            await page.mouse.wheel(0, dy)
            await asyncio.sleep(random.uniform(0.3, 0.9))
    except Exception:
        pass


async def human_click(page: Page, element) -> None:
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


async def pause(label: str) -> None:
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, input, f"\n  [Enter] → продолжить: {label}\n")


async def shot(page: Page, name: str, desc: str) -> None:
    path = os.path.join(SCREENSHOTS_DIR, f"{name}.png")
    await page.screenshot(path=path, full_page=True)
    print(f"\n{'='*60}")
    print(f"  ШАГ: {desc}")
    print(f"  URL: {page.url}")
    print(f"  Скриншот: {path}")
    print(f"{'='*60}")


async def captcha_is_checked(page: Page) -> bool:
    try:
        frame = page.frame_locator('iframe[title*="reCAPTCHA"]').first
        aria = await frame.locator("#recaptcha-anchor").get_attribute("aria-checked", timeout=3_000)
        return aria == "true"
    except Exception:
        return False


async def solve_captcha_with_retry(page: Page, max_attempts: int = 4) -> bool:
    """Кликает капчу, пробует аудио-режим. При неудаче перезагружает TARGET_URL."""
    for attempt in range(1, max_attempts + 1):
        print(f"\n  Попытка капчи {attempt}/{max_attempts}…")

        frame = page.frame_locator('iframe[title*="reCAPTCHA"]').first
        checkbox = frame.locator("#recaptcha-anchor")
        try:
            await checkbox.wait_for(state="visible", timeout=8_000)
            await asyncio.sleep(1.0)
            await checkbox.click()
            print("  Кликнул чекбокс, жду…")
            await asyncio.sleep(4)
        except Exception as e:
            print(f"  ❌ Не смог кликнуть: {e}")
            return False

        try:
            aria = await checkbox.get_attribute("aria-checked", timeout=3_000)
        except Exception:
            aria = None
        if aria == "true":
            print("  ✅ Прошла автоматически!")
            return True

        # Ждём bframe
        challenge = page.frame_locator('iframe[src*="bframe"]')
        bframe_visible = False
        try:
            await challenge.locator("body").wait_for(state="visible", timeout=5_000)
            bframe_visible = True
        except Exception:
            pass

        if not bframe_visible:
            try:
                aria = await checkbox.get_attribute("aria-checked", timeout=2_000)
                if aria == "true":
                    print("  ✅ Капча прошла (повторная проверка)")
                    return True
            except Exception:
                pass
            continue

        print("  Challenge появился — пробую аудио-режим (Whisper)…")
        ok = await solve_audio_challenge(page)
        if ok:
            print("  🎉 Аудио решил!")
            return True

        cooldown = 5.0 + attempt * 3.0
        print(f"  🔄 Аудио не сработало — жду {cooldown:.0f}с и перезагружаю TARGET_URL…")
        await asyncio.sleep(cooldown)
        await page.goto(TARGET_URL, wait_until="domcontentloaded")
        await asyncio.sleep(3)
        try:
            await page.frame_locator('iframe[title*="reCAPTCHA"]').first \
                .locator("#recaptcha-anchor").wait_for(state="visible", timeout=10_000)
        except Exception:
            pass
        await asyncio.sleep(2)

    return False

# ---------------------------------------------------------------------------

async def run_test():
    async with async_playwright() as pw:
        browser = await pw.firefox.launch(headless=False)
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

        import threading
        threading.Thread(target=telegram_command_listener_sync, daemon=True).start()

        print("\n" + "="*60)
        print("  ПОШАГОВЫЙ ТЕСТ (прямая ссылка с токеном)")
        print("  Браузер открыт — наблюдай за каждым действием")
        print("="*60)

        # ------------------------------------------------------------------
        # ШАГ 1 — открываем TARGET_URL
        # ------------------------------------------------------------------
        print("\n► ШАГ 1: открываю TARGET_URL…")
        await page.goto(TARGET_URL, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(3.0, 6.0))
        await human_mouse_move(page)
        await human_scroll(page)
        await asyncio.sleep(random.uniform(1.0, 2.5))
        await shot(page, "01_target_page", "TARGET_URL открыт")

        captcha_ok = await page.frame_locator('iframe[title*="reCAPTCHA"]').first \
            .locator("#recaptcha-anchor").is_visible(timeout=5_000).catch if False else True
        try:
            captcha_visible = await page.frame_locator('iframe[title*="reCAPTCHA"]').first \
                .locator("#recaptcha-anchor").is_visible(timeout=5_000)
        except Exception:
            captcha_visible = False
        next_btn_visible = await page.locator("#next_button").is_visible(timeout=3_000).catch if False else True
        try:
            next_btn_visible = await page.locator("#next_button").is_visible(timeout=3_000)
        except Exception:
            next_btn_visible = False

        print(f"  Капча (reCAPTCHA iframe): {'✅ видна' if captcha_visible else '❌ не найдена'}")
        print(f"  Кнопка #next_button:      {'✅ видна' if next_btn_visible else '❌ не найдена'}")
        await pause("шаг 1 — страница открылась")

        # ------------------------------------------------------------------
        # ШАГ 2 — решаем капчу
        # ------------------------------------------------------------------
        print("\n► ШАГ 2: решаю капчу (аудио-режим через Whisper)…")
        captcha_passed = await solve_captcha_with_retry(page)
        print(f"\n  Итог капчи: {'✅ ПРОЙДЕНА' if captcha_passed else '❌ НЕ ПРОЙДЕНА'}")
        await shot(page, "02_after_captcha", "После решения капчи")
        await pause("шаг 2 — капча")

        # ------------------------------------------------------------------
        # ШАГ 3 — ждём автоперезагрузки, проверяем содержимое
        # ------------------------------------------------------------------
        print("\n► ШАГ 3: жду автоперезагрузки страницы (3-5с)…")
        await asyncio.sleep(random.uniform(3.0, 5.0))
        await shot(page, "03_after_reload", "Страница после автоперезагрузки")

        content = await page.content()
        has_slots = NO_SLOTS_TEXT.lower() not in content.lower()
        print(f"  NO_SLOTS_TEXT: '{NO_SLOTS_TEXT}'")
        print(f"  Найден в HTML: {'❌ нет' if has_slots else '✅ да'}")
        print(f"  Слоты: {'🎉 ЕСТЬ!' if has_slots else 'нет'}")

        # Диагностика — показываем текстовые блоки
        all_texts = await page.locator("td, p, div, span").all_text_contents()
        relevant = [t.strip() for t in all_texts if 10 < len(t.strip()) < 200]
        print("\n  Текстовые блоки на странице (для отладки):")
        for t in relevant[:20]:
            print(f"    • {t}")

        await pause("шаг 3 — содержимое после перезагрузки")

        # ------------------------------------------------------------------
        # ШАГ 4 — мониторинг: галка + слоты каждые 5с
        # ------------------------------------------------------------------
        print("\n► ШАГ 4: мониторинг (галка + слоты каждые 5с)…")
        print("  Нажми Ctrl+C чтобы остановить\n")

        SLOT_CHECK_INTERVAL = 5
        MOUSE_MOVE_INTERVAL = 20
        ticks_since_mouse = 0
        check_count = 0
        slots_found = False

        try:
            while True:
                await asyncio.sleep(SLOT_CHECK_INTERVAL)
                check_count += 1

                # Галка слетела → решаем капчу заново
                captcha_ok = await captcha_is_checked(page)
                if not captcha_ok:
                    print(f"\n  [{check_count}] ⚠️  Галка слетела — решаю капчу заново…")
                    ok = await solve_captcha_with_retry(page)
                    print(f"  Капча: {'✅ решена' if ok else '❌ не решена'}")
                    if ok:
                        await asyncio.sleep(random.uniform(3.0, 5.0))
                        captcha_ok = True

                # Проверяем слоты
                content = await page.content()
                has_slots = NO_SLOTS_TEXT.lower() not in content.lower()
                status = "🎉 ЕСТЬ СЛОТЫ!" if has_slots else "нет слотов"
                print(f"  [{check_count}] галка={'✅' if captcha_ok else '❌'}  слоты: {status}")

                if has_slots:
                    slots_found = True
                    await shot(page, "04_slot_found", "Найдены слоты!")
                    print("\n  🎉 СЛОТЫ НАЙДЕНЫ! Смотри скриншот 04_slot_found")
                    break

                ticks_since_mouse += 1
                if ticks_since_mouse >= MOUSE_MOVE_INTERVAL // SLOT_CHECK_INTERVAL:
                    await human_mouse_move(page)
                    ticks_since_mouse = 0

        except KeyboardInterrupt:
            print("\n  Остановлено пользователем")

        await shot(page, "04_monitor_final", f"Финал мониторинга ({check_count} проверок)")
        await pause(f"шаг 4 — мониторинг завершён (проверок: {check_count}, слоты: {'найдены' if slots_found else 'не найдены'})")

        # ------------------------------------------------------------------
        # ШАГ 5 — кнопка #next_button (только если слоты найдены)
        # ------------------------------------------------------------------
        print("\n► ШАГ 5: проверяю кнопку #next_button…")
        try:
            btn = page.locator("#next_button")
            val = await btn.get_attribute("value") or ""
            vis = await btn.is_visible()
            print(f"  #next_button: value='{val}'  visible={vis}")
            if slots_found and vis:
                await human_click(page, btn)
                print("  ✅ Нажал #next_button!")
                await asyncio.sleep(2)
                await shot(page, "05_after_next", "После нажатия «Далее»")
            else:
                print("  ℹ️  Кнопка не нажималась (слоты не найдены или кнопка скрыта)")
        except Exception as e:
            print(f"  ❌ Ошибка: {e}")

        await pause("шаг 5 — готово")

        # ------------------------------------------------------------------
        # Итог
        # ------------------------------------------------------------------
        print("\n" + "="*60)
        print("  ТЕСТ ЗАВЕРШЁН")
        print(f"  Скриншоты: {os.path.abspath(SCREENSHOTS_DIR)}/")
        print("="*60)
        print("""
  Чеклист:
  ✅ 01 — TARGET_URL открылся, капча и #next_button видны
  ✅ 02 — капча решена (аудио/Whisper)
  ✅ 03 — страница перезагрузилась, NO_SLOTS_TEXT проверен
  ✅ 04 — мониторинг галки + слотов каждые 5с
  ✅ 05 — #next_button нажат при наличии слотов
        """)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, input, "  Enter для закрытия браузера…")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(run_test())
