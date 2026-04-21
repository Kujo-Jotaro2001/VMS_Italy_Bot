"""
Решение reCAPTCHA v2 image challenge через YOLOv8 (COCO).

Поддерживаемые категории (есть в COCO датасете):
  автомобили, грузовики, автобусы, мотоциклы, велосипеды,
  светофоры, пожарные гидранты, самолёты, лодки, люди.

Категории без поддержки (нет в COCO): пешеходные переходы, мосты,
  дымоходы, горы — для них вернём False и упадём на ручное решение.
"""

import asyncio
import io
import logging
import os
import random
import re
from datetime import datetime
from typing import Optional

from PIL import Image

# Папка для дебаг-скриншотов — АБСОЛЮТНЫЙ путь рядом с этим файлом
DEBUG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captcha_debug")
os.makedirs(DEBUG_DIR, exist_ok=True)
print(f"[captcha_solver] DEBUG_DIR = {DEBUG_DIR}", flush=True)

# Морфологический анализатор русского (лениво загружается)
_morph = None

def _get_morph():
    global _morph
    if _morph is None:
        import pymorphy3
        _morph = pymorphy3.MorphAnalyzer()
    return _morph

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Модель (загружается один раз при первом вызове)
# ---------------------------------------------------------------------------

_model = None

def _load_model():
    global _model
    if _model is None:
        # PyTorch 2.6+ по умолчанию грузит только "безопасные" веса.
        # Ultralytics сохраняет свои классы — разрешаем их явно.
        # Альтернатива: monkey-patch torch.load на weights_only=False.
        import torch
        try:
            from ultralytics.nn.tasks import DetectionModel
            torch.serialization.add_safe_globals([DetectionModel])
        except Exception:
            pass
        # Подстраховка — патчим torch.load если add_safe_globals не помог
        _orig_load = torch.load
        def _patched_load(*a, **kw):
            kw.setdefault("weights_only", False)
            return _orig_load(*a, **kw)
        torch.load = _patched_load

        from ultralytics import YOLO
        log.info("Загружаю YOLOv8x… (первый раз скачает ~136 МБ, самая точная модель)")
        _model = YOLO("yolov8x.pt")
        log.info("YOLOv8x загружен ✅")
    return _model

# Имена классов COCO (для дебаг-логов)
COCO_NAMES = {
    0: "person", 1: "bicycle", 2: "car", 3: "motorcycle", 4: "airplane",
    5: "bus", 6: "train", 7: "truck", 8: "boat", 9: "traffic light",
    10: "fire hydrant", 11: "stop sign",
}

# ---------------------------------------------------------------------------
# Маппинг текста задания → ID классов COCO
# ---------------------------------------------------------------------------

# COCO class IDs нужных объектов
COCO = {
    "person":        0,
    "bicycle":       1,
    "car":           2,
    "motorcycle":    3,
    "airplane":      4,
    "bus":           5,
    "train":         6,
    "truck":         7,
    "boat":          8,
    "traffic light": 9,
    "fire hydrant":  10,
    "stop sign":     11,
}

# Словарь лемм (нормальных форм) → COCO class_id.
# Русские — как их вернёт pymorphy3.parse(word)[0].normal_form
# Английские — простой корень/слово в нижнем регистре.
LEMMA_TO_CLASSES: dict[str, list[int]] = {
    # Русский — нормальная форма (именительный падеж, единственное число)
    "автомобиль":   [COCO["car"]],
    "машина":       [COCO["car"]],
    "легковой":     [COCO["car"]],
    "такси":        [COCO["car"]],
    "грузовик":     [COCO["truck"]],
    "грузовой":     [COCO["truck"]],
    "автобус":      [COCO["bus"]],
    "мотоцикл":     [COCO["motorcycle"]],
    "велосипед":    [COCO["bicycle"]],
    "светофор":     [COCO["traffic light"]],
    "гидрант":      [COCO["fire hydrant"]],
    "стоп":         [COCO["stop sign"]],
    "самолёт":      [COCO["airplane"]],
    "самолет":      [COCO["airplane"]],
    "лодка":        [COCO["boat"]],
    "корабль":      [COCO["boat"]],
    "катер":        [COCO["boat"]],
    "человек":      [COCO["person"]],
    "люди":         [COCO["person"]],
    "пешеход":      [COCO["person"]],
    "транспорт":    [COCO["car"], COCO["truck"], COCO["bus"], COCO["motorcycle"]],
    # English — стемированные/базовые формы
    "car":          [COCO["car"]],
    "truck":        [COCO["truck"]],
    "bus":          [COCO["bus"]],
    "motorcycle":   [COCO["motorcycle"]],
    "bicycle":      [COCO["bicycle"]],
    "bike":         [COCO["bicycle"]],
    "traffic":      [COCO["traffic light"]],  # "traffic light" — поймаем по "traffic"
    "hydrant":      [COCO["fire hydrant"]],
    "stop":         [COCO["stop sign"]],
    "airplane":     [COCO["airplane"]],
    "plane":        [COCO["airplane"]],
    "boat":         [COCO["boat"]],
    "vehicle":      [COCO["car"], COCO["truck"], COCO["bus"], COCO["motorcycle"]],
    "person":       [COCO["person"]],
    "people":       [COCO["person"]],
    "pedestrian":   [COCO["person"]],
}

# Кириллица → используем pymorphy3; латиница → нижний регистр (базовая форма).
_CYRILLIC_RE = re.compile(r'[а-яёА-ЯЁ]')

def _lemmatize_word(word: str) -> str:
    """Приводит слово к нормальной форме. Для русского — pymorphy3."""
    if _CYRILLIC_RE.search(word):
        morph = _get_morph()
        return morph.parse(word)[0].normal_form
    return word.lower()

# Разделяет слитые слова на границе строчная→заглавная:
# "велосипедыКогда" → "велосипеды Когда", "busWhen" → "bus When"
_CAMEL_SPLIT_RE = re.compile(r"([a-zа-яё])([A-ZА-ЯЁ])")

def _normalize_text(text: str) -> str:
    return _CAMEL_SPLIT_RE.sub(r"\1 \2", text)

def get_target_classes(task_text: str) -> list[int]:
    """
    Разбивает текст задания на слова, приводит каждое к нормальной форме
    (pymorphy3 для русского), ищет совпадение в LEMMA_TO_CLASSES.
    """
    text = _normalize_text(task_text)
    words = re.findall(r"[A-Za-zА-Яа-яЁё]+", text)
    lemmas = []
    for w in words:
        lemma = _lemmatize_word(w)
        lemmas.append(lemma)
        if lemma in LEMMA_TO_CLASSES:
            class_ids = LEMMA_TO_CLASSES[lemma]
            log.info("Слово '%s' → лемма '%s' → классы COCO %s", w, lemma, class_ids)
            return class_ids
    log.warning("Ни одна лемма не распознана. Леммы текста: %s", lemmas)
    return []

# ---------------------------------------------------------------------------
# Определение объектов на тайле
# ---------------------------------------------------------------------------

CONFIDENCE_THRESHOLD = 0.15   # порог уверенности YOLO (было 0.25)
MIN_CELL_OVERLAP = 0.08        # мин. доля перекрытия bbox и ячейки (8%) для клика
MIN_UPSCALE_SIZE = 640         # апскейлим маленькие тайлы до этого размера

def _upscale_if_small(img: Image.Image) -> Image.Image:
    """Если картинка меньше MIN_UPSCALE_SIZE — апскейлим LANCZOS'ом."""
    w, h = img.size
    if min(w, h) >= MIN_UPSCALE_SIZE:
        return img
    scale = MIN_UPSCALE_SIZE / min(w, h)
    new_size = (int(w * scale), int(h * scale))
    return img.resize(new_size, Image.LANCZOS)

def _detect_boxes(
    img: Image.Image,
    target_classes: list[int],
    debug_name: Optional[str] = None,
) -> list[tuple[float, float, float, float]]:
    """
    Возвращает bounding boxes (x1,y1,x2,y2) объектов target_classes
    в КООРДИНАТАХ ИСХОДНОЙ картинки (до апскейла).
    Если debug_name задан — сохраняет картинку в captcha_debug/.
    """
    model = _load_model()
    orig_w, orig_h = img.size
    up = _upscale_if_small(img)
    up_w, up_h = up.size

    results = model(up, verbose=False, imgsz=640, conf=0.05)

    target_boxes = []
    all_dets = []
    for r in results:
        for box in r.boxes:
            cls_id = int(box.cls[0])
            conf = float(box.conf[0])
            cls_name = COCO_NAMES.get(cls_id, f"cls_{cls_id}")
            all_dets.append(f"{cls_name}:{conf:.2f}")
            if cls_id in target_classes and conf >= CONFIDENCE_THRESHOLD:
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                # Возвращаем координаты в исходном размере
                sx, sy = orig_w / up_w, orig_h / up_h
                target_boxes.append((x1 * sx, y1 * sy, x2 * sx, y2 * sy))

    if debug_name:
        path = os.path.join(DEBUG_DIR, f"{debug_name}.png")
        try:
            up.save(path)
            print(f"  [{debug_name}] saved={path}  YOLO={all_dets[:6] or 'empty'}", flush=True)
        except Exception as e:
            print(f"  [{debug_name}] SAVE FAILED ({e}) path={path}", flush=True)
        log.info("  [%s] YOLO увидел: %s", debug_name, all_dets[:6] or "ничего")

    return target_boxes

# ---------------------------------------------------------------------------
# Режим 3x3: ОТДЕЛЬНЫЕ картинки в каждом тайле
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Человекоподобное взаимодействие — чтобы reCAPTCHA не флажила как бота
# ---------------------------------------------------------------------------

async def _human_delay(lo: float = 0.3, hi: float = 1.2) -> None:
    """Случайная пауза — люди не кликают с одинаковыми интервалами."""
    await asyncio.sleep(random.uniform(lo, hi))

async def _human_click(page, element) -> None:
    """
    Кликает с эмуляцией движения мыши:
      1. находит центр элемента + случайное смещение (не в центр)
      2. двигает мышь дугой (несколько промежуточных точек)
      3. небольшая пауза → клик
    """
    try:
        box = await element.bounding_box()
        if box is None:
            await element.click()
            return
        # Случайная точка внутри элемента (не по центру)
        margin = 0.25
        tx = box["x"] + box["width"]  * random.uniform(margin, 1 - margin)
        ty = box["y"] + box["height"] * random.uniform(margin, 1 - margin)

        # Плавное движение мыши через 4-6 промежуточных точек
        steps = random.randint(4, 7)
        await page.mouse.move(tx, ty, steps=steps)
        await asyncio.sleep(random.uniform(0.08, 0.22))
        await page.mouse.click(tx, ty)
    except Exception:
        try:
            await element.click()
        except Exception:
            pass

async def _click_reload(challenge) -> bool:
    """Жмёт кнопку обновления капчи — даёт новое задание (возможно другой категории)."""
    for sel in ["#recaptcha-reload-button", "button.rc-button-reload"]:
        try:
            btn = challenge.locator(sel).first
            if await btn.is_visible(timeout=1_500):
                await btn.click()
                return True
        except Exception:
            pass
    return False

async def _find_tiles(challenge):
    """
    Находит тайлы через разные селекторы — reCAPTCHA в разных версиях
    использует td/div/.task. Возвращает (locator, count, selector_used).
    """
    candidates = [
        "td.rc-imageselect-tile",
        "div.rc-imageselect-tile",
        ".rc-imageselect-tile",
        "td.rc-imageselect-tileselected, td.rc-imageselect-tile",
        "table.rc-imageselect-table-33 td",
        "table.rc-imageselect-table-44 td",
        'table[class*="rc-imageselect-table"] td',
        "div.task",
    ]
    for sel in candidates:
        loc = challenge.locator(sel)
        try:
            cnt = await loc.count()
            if cnt > 0:
                return loc, cnt, sel
        except Exception:
            continue
    return None, 0, None

async def _solve_3x3(page, challenge, target_classes: list[int], round_num: int) -> int:
    """Каждый тайл — отдельная фотография. Скринимся каждый, YOLO по каждому."""
    tiles, count, sel_used = await _find_tiles(challenge)
    print(f"  Найдено тайлов: {count} (селектор: {sel_used!r})", flush=True)
    log.info("3x3 режим, тайлов: %d (селектор: %s)", count, sel_used)

    if count == 0:
        return 0

    ts_tag = datetime.now().strftime("%H%M%S")
    clicked = 0
    for i in range(count):
        tile = tiles.nth(i)
        try:
            class_attr = await tile.get_attribute("class") or ""
            if "rc-imageselect-dynamic-selected" in class_attr:
                continue

            img_bytes = await tile.screenshot()
            img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            debug_name = f"{ts_tag}_r{round_num}_tile{i}"
            if _detect_boxes(img, target_classes, debug_name=debug_name):
                print(f"  → Тайл {i}: совпадение, кликаю", flush=True)
                log.info("  Тайл %d: совпадение → кликаю", i)
                await _human_click(page, tile)
                clicked += 1
                await _human_delay(0.6, 1.4)
        except Exception as e:
            print(f"  Тайл {i}: ошибка — {e}", flush=True)
            log.warning("  Тайл %d: ошибка — %s", i, e)
    return clicked

# ---------------------------------------------------------------------------
# Режим 4x4: ОДНА большая картинка, разрезанная на 16 квадратов
# ---------------------------------------------------------------------------

async def _solve_4x4(page, challenge, target_classes: list[int], round_num: int) -> int:
    """
    4x4 challenge: одна картинка целиком. Скриним её, YOLO детектирует
    bounding box, кликаем тайлы которые bbox перекрывает.
    """
    table = challenge.locator('table[class*="rc-imageselect-table"]').first
    img_bytes = await table.screenshot()
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    W, H = img.size
    log.info("4x4 режим, размер картинки: %dx%d", W, H)

    ts_tag = datetime.now().strftime("%H%M%S")
    boxes = _detect_boxes(img, target_classes, debug_name=f"{ts_tag}_r{round_num}_4x4")
    if not boxes:
        log.warning("YOLO не нашёл объектов на 4x4 картинке")
        return 0

    # Ячейки 4x4
    cell_w = W / 4
    cell_h = H / 4
    cell_area = cell_w * cell_h
    to_click: set[int] = set()

    for (x1, y1, x2, y2) in boxes:
        for row in range(4):
            for col in range(4):
                cx1, cy1 = col * cell_w, row * cell_h
                cx2, cy2 = cx1 + cell_w, cy1 + cell_h
                # Площадь пересечения
                ow = max(0, min(x2, cx2) - max(x1, cx1))
                oh = max(0, min(y2, cy2) - max(y1, cy1))
                overlap = ow * oh
                if overlap / cell_area >= MIN_CELL_OVERLAP:
                    to_click.add(row * 4 + col)

    log.info("Кликаю ячейки (индексы): %s", sorted(to_click))

    tiles, tile_count, _ = await _find_tiles(challenge)
    for idx in sorted(to_click):
        if idx >= tile_count:
            continue
        try:
            await _human_click(page, tiles.nth(idx))
            await _human_delay(0.4, 1.0)
        except Exception as e:
            log.warning("  Тайл %d: ошибка клика — %s", idx, e)

    return len(to_click)

# ---------------------------------------------------------------------------
# Определение режима и основной цикл
# ---------------------------------------------------------------------------

async def _detect_mode(challenge) -> str:
    """Возвращает '4x4' или '3x3' на основе класса таблицы/картинки."""
    try:
        if await challenge.locator("img.rc-image-tile-44").first.is_visible(timeout=1_000):
            return "4x4"
    except Exception:
        pass
    try:
        if await challenge.locator('table[class*="rc-imageselect-table-44"]').first.is_visible(timeout=1_000):
            return "4x4"
    except Exception:
        pass
    # По умолчанию считаем 3x3
    return "3x3"

MAX_ROUNDS = 10

async def solve_image_challenge(page) -> bool:
    """
    Решает reCAPTCHA image challenge (поддерживает 3x3 и 4x4).
    Возвращает True если капча прошла, False если не смогли.
    """
    challenge = page.frame_locator('iframe[src*="bframe"]')

    try:
        await challenge.locator('table[class*="rc-imageselect-table"]').wait_for(
            state="visible", timeout=6_000
        )
    except Exception:
        log.warning("Таблица тайлов не появилась")
        return False

    for round_num in range(MAX_ROUNDS):
        print(f"\n─── Раунд {round_num + 1} ───", flush=True)
        log.info("─── Раунд %d ───", round_num + 1)

        ts_round = datetime.now().strftime("%H%M%S")

        # Сохраняем скриншот всей таблицы challenge ДО любых действий
        try:
            table_loc = challenge.locator('table[class*="rc-imageselect-table"]').first
            full_png = await table_loc.screenshot()
            full_path = os.path.join(DEBUG_DIR, f"{ts_round}_r{round_num+1}_FULL.png")
            with open(full_path, "wb") as f:
                f.write(full_png)
            print(f"  → сохранён полный скрин: {full_path}", flush=True)
        except Exception as e:
            print(f"  ❌ не смог сохранить полный скрин: {e}", flush=True)

        # Читаем задание
        task_parts = []
        for sel in [
            ".rc-imageselect-desc-no-canonical",
            ".rc-imageselect-desc",
            ".rc-imageselect-desc-wrapper",
            "#rc-imageselect-instructions",
        ]:
            try:
                t = await challenge.locator(sel).first.text_content(timeout=2_000)
                if t and t.strip():
                    task_parts.append(t.strip())
            except Exception:
                pass
        task_text = " | ".join(task_parts)
        print(f"  Задание (raw): {task_text!r}", flush=True)
        log.info("Задание (raw): '%s'", task_text)

        # Сохраняем текст задания в файл
        try:
            with open(os.path.join(DEBUG_DIR, f"{ts_round}_r{round_num+1}_task.txt"), "w") as f:
                f.write(task_text)
        except Exception:
            pass

        target_classes = get_target_classes(task_text)
        if not target_classes:
            print(f"  ⚠️  Категория не распознана (нет в COCO), жму reload…", flush=True)
            log.warning("Неизвестная категория, reload")
            reloaded = await _click_reload(challenge)
            if not reloaded:
                print("  ❌ Кнопка reload не найдена, сдаюсь", flush=True)
                return False
            await _human_delay(1.5, 2.5)
            continue

        print(f"  Классы для поиска (COCO): {target_classes}", flush=True)

        # Определяем режим (3x3 или 4x4) и решаем
        mode = await _detect_mode(challenge)
        print(f"  Режим: {mode}", flush=True)
        log.info("Режим: %s", mode)

        if mode == "4x4":
            clicked = await _solve_4x4(page, challenge, target_classes, round_num + 1)
        else:
            clicked = await _solve_3x3(page, challenge, target_classes, round_num + 1)

        # Если ничего не кликнули — сохраняем HTML challenge-фрейма для диагностики
        if clicked == 0:
            try:
                # Достаём страницу из challenge (через первый элемент)
                # и дампим HTML внутрь bframe-iframe
                html_content = await page.locator('iframe[src*="bframe"]').first.evaluate(
                    "el => el.contentDocument ? el.contentDocument.documentElement.outerHTML : null"
                )
                if html_content:
                    html_path = os.path.join(DEBUG_DIR, f"{ts_round}_r{round_num+1}_frame.html")
                    with open(html_path, "w", encoding="utf-8") as f:
                        f.write(html_content)
                    print(f"  💾 HTML challenge-фрейма → {html_path}", flush=True)
            except Exception as e:
                print(f"  ❌ не смог достать HTML: {e}", flush=True)

        log.info("Всего кликнуто тайлов: %d", clicked)

        # Если ничего не кликнули — reload, а не пустой verify (пустой ответ = красный флаг)
        if clicked == 0:
            print("  ↩️  Ничего не кликнуто — жму reload для новых картинок", flush=True)
            log.info("clicked=0 → reload")
            reloaded = await _click_reload(challenge)
            if not reloaded:
                log.warning("Reload не найден — сдаюсь")
                return False
            await _human_delay(1.5, 2.5)
            continue

        # Человеческая пауза после последнего клика — перед нажатием verify
        await _human_delay(0.8, 2.2)

        # Жмём «Подтвердить/Пропустить» через человекоподобный клик
        try:
            verify_btn = challenge.locator("#recaptcha-verify-button")
            await _human_click(page, verify_btn)
            log.info("Нажал «Подтвердить»")
        except Exception as e:
            log.warning("Не смог нажать «Подтвердить»: %s", e)
            return False

        await _human_delay(2.5, 4.0)

        # Прошло?
        main_frame = page.frame_locator('iframe[title*="reCAPTCHA"]').first
        try:
            aria = await main_frame.locator("#recaptcha-anchor").get_attribute(
                "aria-checked", timeout=2_000
            )
            if aria == "true":
                log.info("✅ Капча решена YOLOv8 за %d раунд(ов)!", round_num + 1)
                return True
        except Exception:
            pass

        # Новое задание?
        try:
            await challenge.locator('table[class*="rc-imageselect-table"]').wait_for(
                state="visible", timeout=3_000
            )
            log.info("Новое задание, продолжаю…")
            await asyncio.sleep(1)
            continue
        except Exception:
            break

    log.warning("Не смогли решить капчу за %d раундов", MAX_ROUNDS)
    return False
