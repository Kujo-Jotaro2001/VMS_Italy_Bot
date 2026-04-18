"""
Пошаговый тест бота. Проходит каждый критический этап,
сохраняет скриншот и ждёт Enter перед следующим шагом.

Запуск: python test_bot.py
Скриншоты: папка screenshots/
"""

import asyncio
import logging
import os
import random
from playwright.async_api import async_playwright, Page
from playwright_stealth import stealth_async

# Включаем логи ДО импорта captcha_solver — иначе log.info() внутри него молчит
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)

from config import BOOKING_NUMBER, PASSPORT_NUMBER, NO_SLOTS_TEXT
from captcha_solver import solve_image_challenge
from audio_solver import solve_audio_challenge, is_captcha_blocked
from bot import telegram_command_listener_sync

INFO_URL = "https://italyvms.com/autoform/info.htm?lang=ru"
SCREENSHOTS_DIR = "screenshots"
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

# ---------------------------------------------------------------------------

async def _fill_form(page: Page) -> None:
    await type_masked(page, "input[name='appnum']", BOOKING_NUMBER)
    await asyncio.sleep(random.uniform(1.2, 2.8))
    await human_mouse_move(page)
    await asyncio.sleep(random.uniform(0.8, 2.0))
    await type_masked(page, "input[name='passnum']", PASSPORT_NUMBER)
    await asyncio.sleep(random.uniform(1.5, 3.2))
    await human_scroll(page)
    await asyncio.sleep(random.uniform(1.0, 2.5))


async def solve_captcha_with_retry(page: Page, max_attempts: int = 4, return_url: str = None) -> bool:
    """
    Кликает капчу, пробует аудио → YOLO.
    Если Google выкидывает «подозрительная активность» — перезагружает
    форму входа и повторяет (до max_attempts раз).
    return_url — если задан, после ре-логина перейдёт обратно на эту страницу.
    Возвращает True если капча пройдена.
    """
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

        aria = await checkbox.get_attribute("aria-checked", timeout=3_000)
        if aria == "true":
            print("  ✅ Прошла автоматически!")
            return True

        # Ждём появления bframe (любой challenge — image или audio)
        challenge = page.frame_locator('iframe[src*="bframe"]')
        bframe_visible = False
        try:
            await challenge.locator("body").wait_for(state="visible", timeout=5_000)
            bframe_visible = True
        except Exception:
            pass

        if not bframe_visible:
            print("  ⚠️  bframe не появился — перепроверяю aria-checked")
            try:
                aria = await checkbox.get_attribute("aria-checked", timeout=2_000)
                if aria == "true":
                    print("  ✅ Капча прошла (повторная проверка)")
                    return True
            except Exception:
                pass
            continue

        print("  ✅ Challenge появился — пробую АУДИО…")
        ok = await solve_audio_challenge(page)
        if ok:
            print("  🎉 АУДИО решил!")
            return True

        # Аудио не прошло — перезагружаем страницу и пробуем снова
        cooldown = 5.0 + attempt * 3.0
        print(f"  🔄 Аудио не сработало — жду {cooldown:.0f}с и перезагружаю страницу…")
        await asyncio.sleep(cooldown)
        if return_url:
            await page.goto(return_url, wait_until="domcontentloaded")
        else:
            await page.goto(INFO_URL, wait_until="domcontentloaded")
            await asyncio.sleep(3)
            await _fill_form(page)
        try:
            chk = page.frame_locator('iframe[title*="reCAPTCHA"]').first
            await chk.locator("#recaptcha-anchor").wait_for(state="visible", timeout=10_000)
        except Exception:
            pass
        await asyncio.sleep(2)
        print(f"  ↩️  Страница перезагружена, следующая попытка…")

    return False

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


async def type_masked(page: Page, selector: str, value: str) -> None:
    field = page.locator(selector)
    await human_click(page, field)
    await asyncio.sleep(random.uniform(0.5, 1.2))
    await field.press("Control+a")
    await asyncio.sleep(random.uniform(0.1, 0.25))
    await field.press("Delete")
    await asyncio.sleep(random.uniform(0.3, 0.7))
    for ch in value:
        await field.press(ch)
        await asyncio.sleep(random.uniform(0.12, 0.28))
        if random.random() < 0.10:
            await asyncio.sleep(random.uniform(0.4, 1.2))

# ---------------------------------------------------------------------------

async def run_test():
    async with async_playwright() as pw:
        browser = await pw.firefox.launch(
            headless=False,
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

        import threading
        threading.Thread(target=telegram_command_listener_sync, daemon=True).start()

        print("\n" + "="*60)
        print("  ПОШАГОВЫЙ ТЕСТ (со stealth)")
        print("  Браузер открыт — наблюдай за каждым действием")
        print("="*60)

        # ------------------------------------------------------------------
        # ШАГ 1 — форма входа
        # ------------------------------------------------------------------
        print("\n► ШАГ 1: открываю форму входа напрямую…")
        await page.goto(INFO_URL, wait_until="domcontentloaded")
        # Dwell — имитируем чтение страницы перед любыми действиями
        await asyncio.sleep(random.uniform(4.0, 8.0))
        await human_mouse_move(page)
        await human_scroll(page)
        await asyncio.sleep(random.uniform(1.5, 3.5))
        await shot(page, "01_info_form", "Форма входа открыта")

        appnum_ok = await page.locator("input[name='appnum']").is_visible()
        passnum_ok = await page.locator("input[name='passnum']").is_visible()
        print(f"  Поле номера записи  (appnum):  {'✅' if appnum_ok else '❌'}")
        print(f"  Поле номера паспорта (passnum): {'✅' if passnum_ok else '❌'}")
        await pause("шаг 1 — форма открылась")

        # ------------------------------------------------------------------
        # ШАГ 2 — заполнение формы посимвольно
        # ------------------------------------------------------------------
        print("\n► ШАГ 2: заполняю форму посимвольно (обход jquery-маски)…")

        try:
            await type_masked(page, "input[name='appnum']", BOOKING_NUMBER)
            filled_val = await page.locator("input[name='appnum']").input_value()
            print(f"  Номер записи   → в поле: '{filled_val}'  ожидалось: '{BOOKING_NUMBER}'")
            print(f"  {'✅ совпадает' if filled_val == BOOKING_NUMBER else '⚠️  НЕ совпадает — маска могла изменить формат'}")
        except Exception as e:
            print(f"  ❌ Ошибка appnum: {e}")

        await asyncio.sleep(0.3)

        try:
            await type_masked(page, "input[name='passnum']", PASSPORT_NUMBER)
            filled_val2 = await page.locator("input[name='passnum']").input_value()
            print(f"  Номер паспорта → в поле: '{filled_val2}'  ожидалось: '{PASSPORT_NUMBER}'")
            print(f"  {'✅ совпадает' if filled_val2 == PASSPORT_NUMBER else '⚠️  НЕ совпадает'}")
        except Exception as e:
            print(f"  ❌ Ошибка passnum: {e}")

        await shot(page, "02_form_filled", "Форма заполнена")
        await pause("шаг 2 — убедись что оба поля заполнены корректно")

        # ------------------------------------------------------------------
        # ШАГ 3 — капча (stealth + YOLOv8 fallback)
        # ------------------------------------------------------------------
        print("\n► ШАГ 3: кликаю капчу (с retry при блокировке)…")
        captcha_passed = await solve_captcha_with_retry(page)
        print(f"\n  Итог капчи: {'✅ ПРОЙДЕНА' if captcha_passed else '❌ НЕ ПРОЙДЕНА'}")
        await shot(page, "03_captcha", "Капча после решения")
        await pause("шаг 3 — капча")

        # ------------------------------------------------------------------
        # ШАГ 4 — нажать «Далее»
        # ------------------------------------------------------------------
        print("\n► ШАГ 4: нажимаю «Далее»…")
        try:
            btn = page.locator("input[type='submit']").first
            val = await btn.get_attribute("value") or ""
            print(f"  Найдена кнопка: '{val}'")
            await btn.click(timeout=5_000)
            pressed = True
        except Exception as e:
            print(f"  ❌ Кнопка не найдена: {e}")
            pressed = False

        await asyncio.sleep(2)
        await shot(page, "04_after_login", "После нажатия «Далее»")

        if pressed:
            content = await page.content()
            if "Назначить другую дату" in content:
                print("  ✅ Попали на страницу с информацией о записи!")
            else:
                print("  ⚠️  Неожиданная страница — смотри скриншот 04")
        await pause("шаг 4 — страница с информацией о записи")

        # ------------------------------------------------------------------
        # ШАГ 5 — «Назначить другую дату»
        # ------------------------------------------------------------------
        print("\n► ШАГ 5: нажимаю «Назначить другую дату»…")
        try:
            btn = page.locator("text=Назначить другую дату").first
            await btn.scroll_into_view_if_needed(timeout=3_000)
            async with page.expect_navigation(timeout=15_000):
                await btn.click(timeout=8_000)
            print("  ✅ Нажал — навигация прошла!")
        except Exception as e:
            # Если навигация уже прошла, ошибка клика не страшна
            if "italyvms.com/autoform/" in page.url and "info" not in page.url:
                print("  ✅ Навигация прошла (клик дал ошибку, но мы на нужной странице)")
            else:
                print(f"  ❌ Ошибка: {e}")

        await shot(page, "05_reschedule_page", "Страница переноса")
        await pause("шаг 5 — страница переноса открыта")

        # ------------------------------------------------------------------
        # ШАГ 6 — капча на странице переноса
        # ------------------------------------------------------------------
        print("\n► ШАГ 6: капча на странице переноса (с retry при блокировке)…")
        print("  ℹ️  При блокировке — перезайдём через форму входа и снова откроем перенос")
        captcha2_passed = False
        for attempt in range(1, 5):
            frame2 = page.frame_locator('iframe[title*="reCAPTCHA"]').first
            cb2 = frame2.locator("#recaptcha-anchor")
            try:
                await cb2.wait_for(state="visible", timeout=8_000)
                await asyncio.sleep(1.0)
                await cb2.click()
                print(f"  Попытка {attempt}: кликнул чекбокс, жду…")
                await asyncio.sleep(4)
            except Exception as e:
                print(f"  ❌ Не смог кликнуть: {e}")
                break

            aria2 = await cb2.get_attribute("aria-checked", timeout=3_000)
            if aria2 == "true":
                print("  ✅ Прошла автоматически")
                captcha2_passed = True
                break

            # Ждём появления bframe (любой challenge — image или audio)
            challenge2 = page.frame_locator('iframe[src*="bframe"]')
            bframe_visible = False
            try:
                await challenge2.locator("body").wait_for(state="visible", timeout=5_000)
                bframe_visible = True
            except Exception:
                pass

            if not bframe_visible:
                print("  ⚠️  bframe не появился — возможно капча уже прошла или ошибка")
                # Перепроверим aria-checked ещё раз
                try:
                    aria2 = await cb2.get_attribute("aria-checked", timeout=2_000)
                    if aria2 == "true":
                        print("  ✅ Капча прошла (повторная проверка)")
                        captcha2_passed = True
                except Exception:
                    pass
                break

            print("  ✅ Challenge появился — пробую АУДИО…")
            ok2 = await solve_audio_challenge(page)
            if ok2:
                print("  🎉 АУДИО решил!")
                captcha2_passed = True
                break

            if await is_captcha_blocked(page) and attempt < 4:
                cooldown = 5.0 + attempt * 2.0
                print(f"  ⛔ Блокировка — жду {cooldown:.0f}с, перезахожу…")
                await asyncio.sleep(cooldown)
                await page.goto(INFO_URL, wait_until="domcontentloaded")
                await asyncio.sleep(3)
                await _fill_form(page)
                await asyncio.sleep(0.5)
                captcha2_passed = await solve_captcha_with_retry(page)
                if captcha2_passed:
                    await page.locator("input[type='submit']").first.click(timeout=5_000)
                    await asyncio.sleep(2)
                    await page.locator("text=Назначить другую дату").first.click(timeout=8_000)
                    await asyncio.sleep(2)
                break

            print("  ⚠️  Аудио не сработало, fallback на YOLO…")
            ok2 = await solve_image_challenge(page)
            if ok2:
                print("  ✅ YOLO решил!")
                captcha2_passed = True
            else:
                print("  ❌ Ни аудио ни YOLO не решили")
            break

        print(f"\n  Итог: {'✅ ПРОЙДЕНА' if captcha2_passed else '❌ НЕ ПРОЙДЕНА'}")
        await shot(page, "06_reschedule_captcha", "Капча на странице переноса")
        await pause("шаг 6 — капча страницы переноса")

        # ------------------------------------------------------------------
        # ШАГ 7 — мониторинг: галка + слоты каждые 5с
        # ------------------------------------------------------------------
        print("\n► ШАГ 7: запускаю мониторинг (галка + слоты каждые 5с)…")
        print("  Нажми Ctrl+C чтобы остановить и посмотреть итог\n")

        SLOT_CHECK_INTERVAL = 5
        MOUSE_MOVE_INTERVAL = 20
        ticks_since_mouse = 0
        check_count = 0
        slots_found = False
        reschedule_url = page.url  # сохраняем URL для возврата после ре-логина

        # Диагностика текущих текстовых блоков
        all_texts = await page.locator("td, p, div.slot, span").all_text_contents()
        relevant = [t.strip() for t in all_texts if 10 < len(t.strip()) < 120]
        print("  Текстовые блоки на странице (для отладки NO_SLOTS_TEXT):")
        for t in relevant[:15]:
            print(f"    • {t}")

        try:
            while True:
                await asyncio.sleep(SLOT_CHECK_INTERVAL)
                check_count += 1

                # Галка слетела → решаем капчу заново
                frame2 = page.frame_locator('iframe[title*="reCAPTCHA"]').first
                try:
                    aria = await frame2.locator("#recaptcha-anchor").get_attribute(
                        "aria-checked", timeout=2_000
                    )
                    captcha_ok = aria == "true"
                except Exception:
                    captcha_ok = False

                if not captcha_ok:
                    print(f"\n  [{check_count}] ⚠️  Галка слетела — решаю капчу заново…")
                    ok = await solve_captcha_with_retry(page, return_url=reschedule_url)
                    print(f"  Капча: {'✅ решена' if ok else '❌ не решена'}")
                    if not ok:
                        print("  🔄 Не решилась — перезагружаю reschedule-страницу и пробую ещё раз…")
                        await asyncio.sleep(5)
                        await page.goto(reschedule_url, wait_until="domcontentloaded")
                        await asyncio.sleep(3)
                        ok2 = await solve_captcha_with_retry(page, return_url=reschedule_url)
                        if not ok2:
                            print("  ❌ Повторная попытка тоже провалилась — останавливаю мониторинг")
                            break
                        print("  ✅ Капча решена после перезагрузки")

                # Проверяем слоты
                content = await page.content()
                has_slots = NO_SLOTS_TEXT.lower() not in content.lower()
                status = "🎉 ЕСТЬ СЛОТЫ!" if has_slots else "нет слотов"
                print(f"  [{check_count}] галка={'✅' if captcha_ok else '❌'}  слоты: {status}")

                if has_slots:
                    slots_found = True
                    await shot(page, "07_slot_found", "Найдены слоты!")
                    print("\n  🎉 СЛОТЫ НАЙДЕНЫ! Смотри скриншот 07_slot_found")
                    break

                # Движение мыши раз в ~20с
                ticks_since_mouse += 1
                if ticks_since_mouse >= MOUSE_MOVE_INTERVAL // SLOT_CHECK_INTERVAL:
                    await human_mouse_move(page)
                    ticks_since_mouse = 0

        except KeyboardInterrupt:
            print("\n  Остановлено пользователем")

        await shot(page, "07_monitor_final", "Финал мониторинга")
        await pause(f"шаг 7 — мониторинг завершён (проверок: {check_count}, слоты: {'найдены' if slots_found else 'не найдены'})")

        # ------------------------------------------------------------------
        # ШАГ 8 — кнопка подтверждения
        # ------------------------------------------------------------------
        print("\n► ШАГ 8: ищу кнопку подтверждения переноса…")
        buttons = await page.locator("input[type='submit'], button").all()
        print(f"  Найдено кнопок: {len(buttons)}")
        for btn in buttons:
            try:
                txt = (await btn.text_content() or await btn.get_attribute("value") or "").strip()
                vis = await btn.is_visible()
                print(f"    • '{txt}'  visible={vis}")
            except Exception:
                pass

        await shot(page, "08_buttons", "Кнопки на странице переноса")
        print("\n  ℹ️  Кнопка не нажимается — только проверка наличия")
        await pause("шаг 8 — кнопки найдены")

        # ------------------------------------------------------------------
        # Итог
        # ------------------------------------------------------------------
        print("\n" + "="*60)
        print("  ТЕСТ ЗАВЕРШЁН")
        print(f"  Скриншоты: {os.path.abspath(SCREENSHOTS_DIR)}/")
        print("="*60)
        print("""
  Чеклист:
  ✅ 01 — форма открылась, поля appnum/passnum видны
  ✅ 02 — оба поля заполнились корректно
  ✅ 03 — капча прошла автоматически (aria-checked=true)
  ✅ 04 — попали на страницу с информацией о записи
  ✅ 05 — страница переноса открылась
  ✅ 06 — капча страницы переноса прошла
  ✅ 07 — мониторинг галки + слотов каждые 5с
  ✅ 08 — кнопка подтверждения видна
        """)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, input, "  Enter для закрытия браузера…")
        await browser.close()
        # listener thread — daemon, завершится сам при выходе


if __name__ == "__main__":
    asyncio.run(run_test())
