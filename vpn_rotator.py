"""
Ротация WireGuard-туннелей Red Shield VPN через CLI wireguard.exe.

Требует:
  * установленный WireGuard для Windows (wireguard.exe в PATH или по стандартному пути)
  * запуск бота от администратора (install/uninstall service)
  * .conf файлы в VPN_CONFIGS
"""

import logging
import os
import shutil
import subprocess
import time

log = logging.getLogger(__name__)

VPN_CONFIGS: list[str] = [
    r"C:\Users\79097\Downloads\United_kingdom.conf",
    r"C:\Users\79097\Downloads\Czech_republic.conf",
    r"C:\Users\79097\Downloads\Serbia.conf",
]

_current_idx: int = -1  # индекс активного конфига в VPN_CONFIGS


def _wireguard_exe() -> str:
    exe = shutil.which("wireguard.exe") or shutil.which("wireguard")
    if exe:
        return exe
    for candidate in (
        r"C:\Program Files\WireGuard\wireguard.exe",
        r"C:\Program Files (x86)\WireGuard\wireguard.exe",
    ):
        if os.path.isfile(candidate):
            return candidate
    raise FileNotFoundError("wireguard.exe не найден — установи WireGuard для Windows")


def _tunnel_name(conf_path: str) -> str:
    return os.path.splitext(os.path.basename(conf_path))[0]


def _uninstall(tunnel_name: str) -> None:
    exe = _wireguard_exe()
    try:
        r = subprocess.run(
            [exe, "/uninstalltunnelservice", tunnel_name],
            capture_output=True, text=True, timeout=30,
        )
        log.info("VPN uninstall %s: rc=%s %s", tunnel_name, r.returncode, (r.stderr or r.stdout).strip()[:200])
    except Exception as e:
        log.warning("VPN uninstall %s: %s", tunnel_name, e)


def _install(conf_path: str) -> bool:
    exe = _wireguard_exe()
    try:
        r = subprocess.run(
            [exe, "/installtunnelservice", conf_path],
            capture_output=True, text=True, timeout=30,
        )
        ok = r.returncode == 0
        log.info("VPN install %s: rc=%s %s", conf_path, r.returncode, (r.stderr or r.stdout).strip()[:200])
        return ok
    except Exception as e:
        log.warning("VPN install %s: %s", conf_path, e)
        return False


def _uninstall_all_known() -> None:
    """Сносит все туннели из VPN_CONFIGS и ждёт пока Windows их реально уберёт."""
    for conf in VPN_CONFIGS:
        _uninstall(_tunnel_name(conf))
    # Ждём пока SCM (Service Control Manager) реально уберёт сервисы
    time.sleep(3.0)


def rotate() -> str | None:
    """
    Переключает на следующий конфиг по кругу.
    Возвращает имя туннеля или None при ошибке.
    """
    global _current_idx
    if not VPN_CONFIGS:
        log.warning("VPN_CONFIGS пуст — нечего ротировать")
        return None

    # Сносим ВСЕ известные туннели — защита от залипших с прошлых запусков
    _uninstall_all_known()

    _current_idx = (_current_idx + 1) % len(VPN_CONFIGS)
    conf = VPN_CONFIGS[_current_idx]
    name = _tunnel_name(conf)

    # Пробуем установить, с одной повторной попыткой если "already running"
    for attempt in range(2):
        if _install(conf):
            break
        log.warning("_install попытка %d не удалась — жду 3с и повторяю", attempt + 1)
        time.sleep(3.0)
    else:
        return None

    # WireGuard поднимает сервис пару секунд; даём сети перестроиться
    time.sleep(4.0)
    log.info("VPN переключён на %s", name)
    return name


def shutdown() -> None:
    """Сносит активный туннель при завершении бота."""
    global _current_idx
    if _current_idx >= 0:
        _uninstall(_tunnel_name(VPN_CONFIGS[_current_idx]))
        _current_idx = -1
