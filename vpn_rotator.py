"""
WireGuard VPN rotator — переключает страну каждые VPN_ROTATE_INTERVAL секунд.

Ожидает файлы конфигов в директории VPN_CONFIGS_DIR (по умолчанию ./wireguard_configs/).
Имена файлов: любые *.conf (например: France.conf, Japan.conf, Serbia.conf).

Требует wg-quick и прав sudo без пароля. Добавь через sudo visudo:
  farida ALL=(ALL) NOPASSWD: /opt/homebrew/bin/wg-quick, /opt/homebrew/bin/wg
"""

import asyncio
import glob
import logging
import os
import platform
import subprocess
import time

log = logging.getLogger(__name__)

IS_MACOS = platform.system() == "Darwin"

# --- Конфигурация (переопределяется из config.py) ---
VPN_CONFIGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wireguard_configs")
VPN_ROTATE_INTERVAL = 20 * 60   # 20 минут
VPN_CONNECT_TIMEOUT = 30        # секунд на поднятие интерфейса

VPN_ROTATION_ORDER: list[str] = []  # пустой = алфавитный порядок

try:
    import config as _cfg
    if getattr(_cfg, "VPN_CONFIGS_DIR", ""):
        VPN_CONFIGS_DIR = _cfg.VPN_CONFIGS_DIR
    if getattr(_cfg, "VPN_ROTATE_INTERVAL", None):
        VPN_ROTATE_INTERVAL = _cfg.VPN_ROTATE_INTERVAL
    if getattr(_cfg, "VPN_ROTATION_ORDER", None):
        VPN_ROTATION_ORDER = _cfg.VPN_ROTATION_ORDER
except ImportError:
    pass


# текущий активный конфиг (полный путь) и имя интерфейса
_current_conf: str | None = None
_current_iface: str | None = None
_current_index: int = -1
_last_rotation_at: float = 0.0  # timestamp последней успешной ротации (любой)
_consecutive_failures: int = 0  # подряд неудачных rotate_vpn (для детектирования деградации сети)


def _list_configs() -> list[str]:
    pattern = os.path.join(VPN_CONFIGS_DIR, "*.conf")
    all_confs = {os.path.splitext(os.path.basename(c))[0]: c for c in glob.glob(pattern)}
    if VPN_ROTATION_ORDER:
        ordered = [all_confs[name] for name in VPN_ROTATION_ORDER if name in all_confs]
        # добавляем конфиги которых нет в списке порядка (на случай новых файлов)
        ordered += sorted(c for name, c in all_confs.items() if name not in VPN_ROTATION_ORDER)
        return ordered
    return sorted(all_confs.values())


def _iface_name(conf_path: str) -> str:
    """Имя интерфейса = имя файла без расширения, макс 15 символов."""
    base = os.path.splitext(os.path.basename(conf_path))[0]
    return base[:15]


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (result.stdout + result.stderr).strip()
        return result.returncode, out
    except subprocess.TimeoutExpired:
        return 1, f"timeout after {timeout}s"
    except Exception as e:
        return 1, str(e)


def _iface_exists(iface: str) -> bool:
    """Проверяет существование интерфейса: ifconfig на macOS, ip link на Linux."""
    if IS_MACOS:
        rc, _ = _run(["ifconfig", iface], timeout=5)
    else:
        rc, _ = _run(["ip", "link", "show", iface], timeout=5)
    return rc == 0


def _wg_quick_down(conf_path: str) -> bool:
    """Отключает VPN по полному пути к конфигу и ждёт исчезновения интерфейса."""
    iface = _iface_name(conf_path)
    log.info("VPN: отключаю %s…", iface)
    # wg-quick down требует полный путь к конфигу (особенно на macOS)
    rc, out = _run(["sudo", "wg-quick", "down", conf_path], timeout=VPN_CONNECT_TIMEOUT)
    if rc != 0:
        if any(x in out for x in ("does not exist", "No such", "is not a WireGuard")):
            log.debug("VPN: %s уже был выключен", iface)
            return True
        log.warning("VPN: wg-quick down вернул %d: %s", rc, out)
        return False

    # Ждём пока интерфейс реально исчезнет (до 10с)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not _iface_exists(iface):
            log.info("VPN: %s выключен", iface)
            return True
        time.sleep(0.5)

    log.warning("VPN: %s всё ещё жив после wg-quick down — продолжаем", iface)
    return False


def _reset_macos_network() -> None:
    """
    Аварийный сброс сети на macOS: убивает зависшие wireguard-go процессы,
    удаляет мёртвые utun интерфейсы и восстанавливает DNS.
    """
    if not IS_MACOS:
        return
    log.warning("VPN: сброс сети macOS…")

    # Убиваем зависшие wireguard-go процессы
    rc, out = _run(["pgrep", "-x", "wireguard-go"], timeout=5)
    if rc == 0 and out.strip():
        pids = out.strip().split()
        log.warning("VPN: убиваю wireguard-go (pid: %s)", pids)
        for pid in pids:
            _run(["sudo", "/bin/kill", "-9", pid], timeout=5)
        time.sleep(2)

    # Удаляем мёртвые utun интерфейсы (те что не принадлежат активному WireGuard)
    active_ifaces = set(_active_wg_ifaces())
    rc, out = _run(["ifconfig", "-l"], timeout=5)
    if rc == 0:
        all_ifaces = out.split()
        dead_utuns = [i for i in all_ifaces if i.startswith("utun") and i not in active_ifaces]
        for iface in dead_utuns:
            rc2, _ = _run(["sudo", "ifconfig", iface, "down"], timeout=5)
            if rc2 == 0:
                log.info("VPN: убрал мёртвый интерфейс %s", iface)

    # Восстанавливаем DNS на всех сетевых сервисах
    rc, out = _run(["networksetup", "-listallnetworkservices"], timeout=5)
    if rc == 0:
        services = [s.strip() for s in out.splitlines() if s.strip() and not s.startswith("*")]
        for svc in services:
            rc2, _ = _run(["networksetup", "-setdnsservers", svc, "Empty"], timeout=5)
            if rc2 == 0:
                log.info("VPN: DNS сброшен для '%s'", svc)
    time.sleep(2)
    log.warning("VPN: сброс сети завершён")


def _check_connectivity(retries: int = 10, delay: float = 3.0) -> bool:
    """Проверяет доступность сети HTTP-запросом после поднятия VPN."""
    for i in range(1, retries + 1):
        rc, _ = _run(
            ["curl", "-s", "--max-time", "5", "-o", "/dev/null",
             "-w", "%{http_code}", "https://ifconfig.me"],
            timeout=8,
        )
        if rc == 0:
            log.info("VPN: сеть доступна (попытка %d)", i)
            return True
        log.info("VPN: нет сети, жду %.0fс (попытка %d/%d)…", delay, i, retries)
        time.sleep(delay)
    log.warning("VPN: сеть не появилась за %.0fс", retries * delay)
    return False


def _wg_quick_up(conf_path: str) -> bool:
    iface = _iface_name(conf_path)
    log.info("VPN: подключаю %s…", iface)
    rc, out = _run(["sudo", "wg-quick", "up", conf_path], timeout=VPN_CONNECT_TIMEOUT)
    if rc != 0:
        if "already exists" in out:
            log.info("VPN: %s уже активен", iface)
            return True
        log.warning("VPN: wg-quick up вернул %d: %s", rc, out)
        return False
    log.info("VPN: %s поднят — проверяю доступность сети…", iface)
    if not _check_connectivity():
        log.warning("VPN: %s поднят, но интернет не отвечает", iface)
        return False
    return True


def rotate_vpn() -> str | None:
    """
    Переключает VPN последовательно по кругу.
    Возвращает имя интерфейса или None при ошибке.
    Любой успешный вызов обновляет _last_rotation_at — это сдвигает плановый таймер.
    Если все конфиги подряд не могут поднять сеть — делает аварийный сброс DNS.
    """
    global _current_conf, _current_iface, _current_index, _last_rotation_at, _consecutive_failures

    configs = _list_configs()
    if not configs:
        log.error("VPN: нет конфигов в %s", VPN_CONFIGS_DIR)
        return None

    # После первой же неудачи сразу сбрасываем сеть — не ждём полного круга
    if _consecutive_failures > 0:
        log.warning("VPN: %d неудач подряд — сбрасываю сеть перед следующей попыткой",
                    _consecutive_failures)
        _reset_macos_network()

    _current_index = (_current_index + 1) % len(configs)
    next_conf = configs[_current_index]
    next_iface = _iface_name(next_conf)
    log.info("VPN: ротация %d/%d → %s", _current_index + 1, len(configs), next_iface)

    # Сначала отключаем текущий и ждём что он пропал
    if _current_conf is not None:
        _wg_quick_down(_current_conf)

    # Только потом поднимаем новый
    ok = _wg_quick_up(next_conf)
    if ok:
        _current_conf = next_conf
        _current_iface = next_iface
        _last_rotation_at = time.time()
        _consecutive_failures = 0
        log.info("VPN: активен %s", next_iface)
        return next_iface
    else:
        log.error("VPN: не удалось поднять %s", next_iface)
        _consecutive_failures += 1
        _current_conf = None
        _current_iface = None
        if _consecutive_failures >= len(configs):
            log.error("VPN: все %d стран не дали сеть — завершаю процесс для автоперезапуска", len(configs))
            import sys
            sys.exit(2)
        return None


async def rotate_vpn_async() -> str | None:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, rotate_vpn)


def last_rotation_at() -> float:
    """Timestamp последней успешной ротации (для синхронизации внешних таймеров)."""
    return _last_rotation_at


def _active_wg_ifaces() -> list[str]:
    rc, out = _run(["sudo", "wg", "show", "interfaces"], timeout=10)
    if rc != 0 or not out:
        return []
    return out.split()


def teardown_vpn() -> None:
    """Отключает все активные WireGuard интерфейсы при завершении бота."""
    global _current_conf, _current_iface

    # Сначала пробуем через wg show — находит всё что живёт
    ifaces = _active_wg_ifaces()
    if ifaces:
        log.info("VPN: завершение — отключаю: %s", ifaces)
        # Ищем конфиг для каждого интерфейса чтобы передать полный путь
        all_configs = _list_configs()
        conf_by_iface = {_iface_name(c): c for c in all_configs}
        for iface in ifaces:
            conf = conf_by_iface.get(iface)
            if conf:
                _wg_quick_down(conf)
            else:
                # Конфига нет — пробуем по имени интерфейса (Linux fallback)
                _run(["sudo", "wg-quick", "down", iface], timeout=VPN_CONNECT_TIMEOUT)
    elif _current_conf is not None:
        log.info("VPN: завершение — отключаю %s", _current_iface)
        _wg_quick_down(_current_conf)

    _current_conf = None
    _current_iface = None


async def vpn_rotation_loop(on_rotate=None) -> None:
    """Фоновый цикл ротации VPN каждые VPN_ROTATE_INTERVAL секунд."""
    configs = _list_configs()
    log.info(
        "VPN ротатор запущен: конфигов=%d, интервал=%dмин, директория=%s",
        len(configs), VPN_ROTATE_INTERVAL // 60, VPN_CONFIGS_DIR,
    )
    if not configs:
        log.error("VPN: конфиги не найдены — ротатор выключен")
        return

    async def _call_callback(iface: str) -> None:
        if on_rotate:
            try:
                if asyncio.iscoroutinefunction(on_rotate):
                    await on_rotate(iface)
                else:
                    on_rotate(iface)
            except Exception:
                pass

    # Первое подключение сразу при старте
    iface = await rotate_vpn_async()
    if iface:
        await _call_callback(iface)

    while True:
        await asyncio.sleep(VPN_ROTATE_INTERVAL)
        log.info("VPN: время ротации (каждые %d мин)", VPN_ROTATE_INTERVAL // 60)
        iface = await rotate_vpn_async()
        if iface:
            await _call_callback(iface)
