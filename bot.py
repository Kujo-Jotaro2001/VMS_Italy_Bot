"""
VMS Italy — монитор свободных слотов по прямой ссылке с токеном.

Поток:
  1. Открываем браузер, загружаем TARGET_URL
  2. Решаем капчу один раз (аудио через Whisper)
  3. Каждые 30-50с читаем #first_date прямо из DOM (без перезагрузки)
     - если галка капчи слетела → решаем снова
     - если слот появился → бронируем, уведомляем TG
"""

import asyncio
import logging
import random
import httpx
from datetime import datetime
from playwright.async_api import async_playwright, Page
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
    steps = min(steps, 30)

    for i in range(1, steps + 1):
        t = i / steps
        t = t * t * (3 - 2 * t)
        mt = 1 - t
        x = mt**3 * sx + 3*mt**2*t*c1x + 3*mt*t**2*c2x + t**3 * tx
        y = mt**3 * sy + 3*mt**2*t*c1y + 3*mt*t**2*c2y + t**3 * ty
        try:
            await page.mouse.move(x, y)
        except Exception:
            return
        _mouse_pos["x"], _mouse_pos["y"] = x, y
    # Один sleep в конце вместо 30-60 микро-sleep на каждый шаг
    await asyncio.sleep(random.uniform(0.1, 0.3))


async def human_mouse_move(page: Page, fast: bool = False) -> None:
    """Случайное движение мыши по странице."""
    w, h = 1280, 800
    tx = random.uniform(50, w - 50)
    ty = random.uniform(50, h - 50)
    if fast:
        # Во время мониторинга — один быстрый move без Безье-цикла
        try:
            await page.mouse.move(tx, ty, steps=5)
        except Exception:
            pass
        _mouse_pos["x"], _mouse_pos["y"] = tx, ty
    else:
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
from audio_solver import solve_audio_challenge
from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    TARGET_URL, HEADLESS,
)

# Поддержка нескольких получателей: строка или список строк
_CHAT_IDS: list[str] = (
    [str(c) for c in TELEGRAM_CHAT_ID]
    if isinstance(TELEGRAM_CHAT_ID, (list, tuple))
    else [str(TELEGRAM_CHAT_ID)]
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

async def tg(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=10, verify=False) as client:
        for chat_id in _CHAT_IDS:
            try:
                r = await client.post(url, json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                })
                if r.status_code != 200:
                    log.warning("Telegram [%s] ответил %s: %s", chat_id, r.status_code, r.text[:200])
            except Exception as e:
                log.warning("Не смог отправить в Telegram [%s]: %s: %s", chat_id, type(e).__name__, e)

def ts() -> str:
    return datetime.now().strftime("%d.%m %H:%M:%S")


def _tg_send_sync(text: str) -> None:
    """Синхронная отправка в Telegram через urllib (без httpx/asyncio)."""
    import urllib.request
    import json as _json
    import ssl as _ssl
    _ctx = _ssl.create_default_context()
    _ctx.check_hostname = False
    _ctx.verify_mode = _ssl.CERT_NONE
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    for chat_id in _CHAT_IDS:
        body = _json.dumps({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=15, context=_ctx) as r:
                r.read()
        except Exception as e:
            log.warning("TG send [%s]: %s: %s", chat_id, type(e).__name__, e)


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
    import ssl as _ssl

    _ctx = _ssl.create_default_context()
    _ctx.check_hostname = False
    _ctx.verify_mode = _ssl.CERT_NONE

    base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    offset = None
    startup_ts = int(_time.time())
    log.info("TG listener: старт, игнорирую сообщения старше %d", startup_ts)

    while True:
        try:
            params = {"timeout": "5"}  # короткий long-poll → быстро реагируем на смену VPN
            if offset is not None:
                params["offset"] = str(offset)
            url = f"{base_url}/getUpdates?{urllib.parse.urlencode(params)}"
            # socket timeout чуть больше long-poll timeout
            with urllib.request.urlopen(url, timeout=8, context=_ctx) as r:
                data = _json.loads(r.read())
            if not data.get("ok"):
                log.warning("TG getUpdates not ok: %s", data)
                _time.sleep(3)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                msg_date = msg.get("date", 0)
                if msg_date < startup_ts:
                    continue  # старое — игнорируем
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = (msg.get("text") or "").strip().lower()
                log.info("TG upd: chat_id=%s text=%r", chat_id, text)
                if chat_id not in _CHAT_IDS:
                    log.warning("TG: chat_id %s не в списке — игнорирую", chat_id)
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
                log.debug("TG listener: %s: %s — жду 3с", type(e).__name__, e)
                _time.sleep(3)

# ---------------------------------------------------------------------------
# Капча
# ---------------------------------------------------------------------------

async def captcha_is_checked(page: Page) -> bool:
    try:
        frame = page.frame_locator('iframe[title*="reCAPTCHA"]').first
        aria = await frame.locator("#recaptcha-anchor").get_attribute("aria-checked", timeout=1_000)
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
    3. Если появился bframe (любой challenge) — вызывает solve_audio_challenge
       (который сам переключится на аудио через #recaptcha-audio-button)
    """
    label = f" ({context})" if context else ""

    try:
        # Одно движение мыши — «заметил капчу»
        await human_mouse_move(page)
        await human_delay(0.5, 1.2)
        frame = page.frame_locator('iframe[title*="reCAPTCHA"]').first
        checkbox = frame.locator("#recaptcha-anchor")
        await checkbox.wait_for(state="visible", timeout=20_000)
        await human_delay(0.4, 1.0)
        await human_click(page, checkbox)
        log.info("Кликнул капчу%s", label)
        await asyncio.sleep(random.uniform(1.2, 2.5))
    except Exception as e:
        log.warning("Не смог кликнуть капчу%s: %s", label, e)
        return False

    # Прошла с первого клика?
    if await captcha_is_checked(page):
        log.info("Капча прошла автоматически ✅")
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
        # Перепроверим галку — могла проставиться пока ждали
        if await captcha_is_checked(page):
            log.info("Капча прошла (повторная проверка) ✅")
            return True
        log.warning("Капча не прошла и bframe не появился%s", label)
        return False

    # Challenge есть → аудио-режим (Whisper)
    log.info("Challenge появился — пробую аудио-режим (Whisper)…")
    ok = await solve_audio_challenge(page)
    if ok:
        return True
    log.warning("Аудио не сработало — вернём False")
    return False

# ---------------------------------------------------------------------------
# Открытие целевой страницы
# ---------------------------------------------------------------------------

async def open_target(page: Page) -> None:
    log.info("Открываю TARGET_URL…")
    for attempt in range(5):
        try:
            await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=45_000)
            break
        except Exception as e:
            err = str(e)
            if attempt < 4 and any(x in err for x in ("ERR_NAME_NOT_RESOLVED", "ERR_NETWORK", "Timeout", "timeout")):
                log.warning("Сайт недоступен (%s), жду 8с (попытка %d/5)…", err[:60], attempt + 1)
                await asyncio.sleep(8.0)
            else:
                raise
    await human_delay(2.0, 4.0)
    await human_mouse_move(page)

# ---------------------------------------------------------------------------
# Проверка наличия слотов
# ---------------------------------------------------------------------------

RATE_LIMIT_TEXT = "Слишком много запросов"

async def slots_available(page: Page) -> bool:
    """
    Слот есть только если в #first_date стоит реальная дата dd.mm.yyyy.
    Rate-limit и NO_SLOTS_TEXT — оба означают «слота нет».
    """
    text = await page.evaluate("""
        () => {
            const el = document.querySelector('#first_date');
            return el ? (el.innerText || el.textContent || '').trim() : '';
        }
    """)
    import re
    if re.search(r'\d{2}\.\d{2}\.\d{4}', text):
        return True
    if RATE_LIMIT_TEXT in text:
        log.debug("Rate-limit от сервера, ждём следующей проверки")
    return False


async def is_rate_limited(page: Page) -> bool:
    try:
        text = await page.evaluate("""
            () => {
                const el = document.querySelector('#first_date');
                return el ? (el.innerText || el.textContent || '').trim() : '';
            }
        """)
        if RATE_LIMIT_TEXT in text:
            return True
    except Exception:
        pass
    try:
        body_txt = await page.locator("body").inner_text(timeout=1_500)
        if RATE_LIMIT_TEXT in body_txt:
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Нажать кнопку «Далее» (когда слоты появились)
# ---------------------------------------------------------------------------

async def _debug_snapshot(page: Page, tag: str) -> None:
    """Сохранить HTML + скриншот для разбора структуры при успехе."""
    import os
    from datetime import datetime
    try:
        ts_tag = datetime.now().strftime("%H%M%S")
        dbg_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captcha_debug")
        os.makedirs(dbg_dir, exist_ok=True)
        html_path = os.path.join(dbg_dir, f"{ts_tag}_{tag}.html")
        png_path = os.path.join(dbg_dir, f"{ts_tag}_{tag}.png")
        html = await page.content()
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        try:
            await page.screenshot(path=png_path, full_page=True)
        except Exception:
            pass
        log.info("Сохранил debug: %s, %s", html_path, png_path)
    except Exception as e:
        log.warning("Не смог сохранить debug snapshot: %s", e)


async def _pick_nearest_date(page: Page) -> str | None:
    """
    Читает дату из #first_date (формат dd.mm.yyyy).
    Возвращает None если там rate-limit или пусто — тогда будет fallback
    на первый доступный день в датепикере.
    """
    try:
        text = await page.evaluate("""
            () => {
                const root = document.querySelector('#first_date');
                return root ? (root.innerText || root.textContent || '').trim() : '';
            }
        """)
        import re
        m = re.search(r'(\d{2}\.\d{2}\.\d{4})', text)
        if m:
            return m.group(1)
        log.info("#first_date: '%s' — дата не найдена, используем fallback", text[:60])
    except Exception as e:
        log.warning("Ошибка при чтении #first_date: %s", e)
    return None


async def _select_date_and_time(page: Page) -> bool:
    """
    1. Кликает на поле #app_date — открывается jQuery UI datepicker
    2. Ждёт появления календаря, кликает первый доступный день
       (td без ui-state-disabled, с a.ui-state-default внутри)
    3. Ждёт появления опций в #timeslot и выбирает первую непустую
    """
    date_str = await _pick_nearest_date(page)
    if not date_str:
        log.warning("Дата в #first_date не найдена — не могу выбрать слот автоматически")
        return False

    log.info("Ближайшая доступная дата: %s", date_str)

    # Шаг 1: открываем календарь кликом на поле
    try:
        await page.locator("#app_date").wait_for(state="visible", timeout=5_000)
        await page.click("#app_date")
        await page.wait_for_function(
            "() => { const d = document.querySelector('#ui-datepicker-div'); "
            "return d && d.style.display !== 'none'; }",
            timeout=3_000,
        )
    except Exception as e:
        log.warning("Не смог открыть календарь: %s", e)
        return False

    # Шаг 2: листаем до нужного месяца и кликаем день
    try:
        if date_str:
            # Знаем точную дату — листаем до нужного месяца и кликаем конкретный день
            parts = date_str.split(".")
            target_day   = int(parts[0])
            target_month = int(parts[1])
            target_year  = int(parts[2])
            clicked = await page.evaluate(
                """([day, month, year]) => {
                    const $inp = $('#app_date');
                    for (let i = 0; i < 18; i++) {
                        const inst = $.datepicker._getInst($inp[0]);
                        if (inst.drawMonth + 1 === month && inst.drawYear === year) break;
                        $.datepicker._adjustDate($inp[0], +1, 'M');
                    }
                    const links = document.querySelectorAll(
                        '#ui-datepicker-div td:not(.ui-state-disabled) a.ui-state-default'
                    );
                    for (const a of links) {
                        if (a.textContent.trim() === String(day)) { a.click(); return true; }
                    }
                    return false;
                }""",
                [target_day, target_month, target_year],
            )
            if clicked:
                log.info("Выбрал дату %s в календаре", date_str)
            else:
                log.warning("День %s не найден — кликаю первый доступный", target_day)
                clicked = await page.evaluate("""
                    () => {
                        const a = document.querySelector(
                            '#ui-datepicker-div td:not(.ui-state-disabled) a.ui-state-default'
                        );
                        if (a) { a.click(); return a.textContent.trim(); }
                        return null;
                    }
                """)
                log.info("Кликнул первый доступный день: %s", clicked)
        else:
            # #first_date недоступен (rate-limit) — кликаем первый доступный день
            clicked = await page.evaluate("""
                () => {
                    const a = document.querySelector(
                        '#ui-datepicker-div td:not(.ui-state-disabled) a.ui-state-default'
                    );
                    if (a) { a.click(); return a.textContent.trim(); }
                    return null;
                }
            """)
            if not clicked:
                log.warning("Нет доступных дней в календаре")
                return False
            log.info("Кликнул первый доступный день: %s (дата из first_date недоступна)", clicked)
    except Exception as e:
        log.warning("Ошибка при выборе даты в календаре: %s", e)
        return False

    # Шаг 3: ждём пока AJAX заполнит #timeslot
    try:
        await page.wait_for_function(
            """() => {
                const sel = document.querySelector('#timeslot');
                if (!sel || sel.style.display === 'none') return false;
                return Array.from(sel.options).some(o => o.value && o.value !== '0');
            }""",
            timeout=12_000,
        )
    except Exception:
        log.warning("Таймслоты не появились за 12с")
        return False

    # Шаг 4: выбираем первую непустую опцию времени
    try:
        picked = await page.evaluate(
            """() => {
                const sel = document.querySelector('#timeslot');
                const opt = Array.from(sel.options).find(o => o.value && o.value !== '0');
                if (!opt) return null;
                sel.value = opt.value;
                $(sel).trigger('change');
                return opt.text.trim();
            }"""
        )
        if not picked:
            log.warning("Нет валидных таймслотов в #timeslot")
            return False
        log.info("Выбрал время: %s", picked)
        return True
    except Exception as e:
        log.warning("Не смог выбрать время: %s", e)
        return False


async def click_next(page: Page) -> bool:
    try:
        # Сохраняем снапшот структуры — на случай если селекторы не подойдут
        await _debug_snapshot(page, "slot_found")

        # Выбираем дату и время ДО нажатия кнопки
        filled = await _select_date_and_time(page)
        if not filled:
            log.warning("Не удалось выбрать дату/время — пробую жать «Далее» как есть")

        btn = page.locator("#next_button")
        await btn.wait_for(state="visible", timeout=5_000)
        await page.click("#next_button")
        log.info("Нажал кнопку «Далее» (#next_button)")
        return True
    except Exception as e:
        log.warning("Не смог нажать «Далее»: %s", e)
        return False

# ---------------------------------------------------------------------------
# Основной цикл мониторинга (браузер открыт всё время)
# ---------------------------------------------------------------------------

CAPTCHA_INTERVAL_LO = 170     # сек между решениями капчи
CAPTCHA_INTERVAL_HI = 190     # сек между решениями капчи
CAPTCHA_FAIL_THRESHOLD = 3    # пересоздаём контекст после N неудач подряд

async def monitor_loop(page: Page) -> None:
    """
    Цикл: решаем капчу → сразу проверяем #first_date → ждём 2 мин → повтор.
    """
    log.info("Мониторинг запущен (капча каждые %s–%sс)…", CAPTCHA_INTERVAL_LO, CAPTCHA_INTERVAL_HI)

    captcha_fails = 0

    while True:
        # Решаем капчу
        if not await captcha_is_checked(page):
            ok = await solve_captcha(page, "мониторинг")
            if not ok:
                captcha_fails += 1
                log.warning("Капча не решилась (неудача %d/%d)", captcha_fails, CAPTCHA_FAIL_THRESHOLD)
                if captcha_fails >= CAPTCHA_FAIL_THRESHOLD:
                    raise RuntimeError("captcha_failed")
                await asyncio.sleep(10.0)
                continue
            captcha_fails = 0
            await asyncio.sleep(random.uniform(2.0, 3.0))

        # Сразу после капчи читаем #first_date
        has_slots = await slots_available(page)
        bot_state.on_slot_check(has_slots)

        if not has_slots:
            log.info("Слотов нет — жду %s–%sс до следующей капчи…", CAPTCHA_INTERVAL_LO, CAPTCHA_INTERVAL_HI)
            await asyncio.sleep(random.uniform(CAPTCHA_INTERVAL_LO, CAPTCHA_INTERVAL_HI))
            continue

        # Слот найден — бронируем
        msg = f"🎉 НАЙДЕН СВОБОДНЫЙ СЛОТ! [{ts()}]\n{page.url}"
        log.info(msg)
        await tg(msg)

        pressed = False
        try:
            pressed = await click_next(page)
        except Exception as e:
            log.exception("Ошибка при бронировании: %s", e)

        if pressed:
            await tg(
                f"✅ Выбрал дату/время и нажал «Далее» [{ts()}] "
                f"— заходи на сайт СРОЧНО и заверши запись!\n{page.url}"
            )
        else:
            await tg(
                f"⚠️ Не смог нажать «Далее» автоматически [{ts()}] "
                f"— зайди вручную немедленно!\n{page.url}"
            )

        log.info("Слот обработан — пауза 120с")
        await asyncio.sleep(120)


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

async def main() -> None:
    import threading
    threading.Thread(target=telegram_command_listener_sync, daemon=True).start()

    vp_w = random.choice([1280, 1366, 1440, 1536, 1920])
    vp_h = random.choice([768, 800, 864, 900, 1080])
    chrome_ver = random.choice(["124.0.0.0", "125.0.0.0", "126.0.0.0", "127.0.0.0", "128.0.0.0"])

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-infobars",
                "--disable-extensions",
            ],
        )

        async def new_page():
            ctx = await browser.new_context(
                user_agent=(
                    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    f"AppleWebKit/537.36 (KHTML, like Gecko) "
                    f"Chrome/{chrome_ver} Safari/537.36"
                ),
                locale="ru-RU",
                viewport={"width": vp_w, "height": vp_h},
            )
            p = await ctx.new_page()
            await stealth_async(p)
            await p.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} };
            """)
            return ctx, p

        context, page = await new_page()

        while True:
            try:
                await open_target(page)
                await monitor_loop(page)

            except RuntimeError as e:
                if "captcha_failed" in str(e):
                    log.warning("Капча не решается — пересоздаю контекст и перезагружаю")
                    try:
                        await context.close()
                    except Exception:
                        pass
                    await asyncio.sleep(5.0)
                    context, page = await new_page()
                    continue
                raise
            except Exception as e:
                log.exception("Неожиданная ошибка: %s", e)
                await tg(f"💥 Ошибка [{ts()}]: {e}\nПерезапускаю через 30 сек")
                await asyncio.sleep(30)
                try:
                    await context.close()
                except Exception:
                    pass
                context, page = await new_page()


if __name__ == "__main__":
    asyncio.run(main())
