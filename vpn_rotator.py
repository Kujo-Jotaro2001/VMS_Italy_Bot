"""
Ротация WireGuard-туннелей через CLI wg-quick (macOS).

Требует:
  * установленный WireGuard: brew install wireguard-tools
  * запуск с sudo или настроенный sudoers для wg-quick
  * .conf файлы в VPN_CONFIGS
"""

import logging
import os
import shutil
import subprocess
import time

log = logging.getLogger(__name__)

_DOWNLOADS = os.path.expanduser("~/Downloads")

VPN_CONFIGS: list[str] = [
    os.path.join(_DOWNLOADS, "Belarus.conf"),
    os.path.join(_DOWNLOADS, "Netherlands.conf"),
    os.path.join(_DOWNLOADS, "Switzerland.conf"),
    os.path.join(_DOWNLOADS, "Hungary.conf"),
]

# Timezone, соответствующий каждому конфигу (по индексу)
VPN_TIMEZONES: list[str] = [
    "Europe/Minsk",        # Serbia
    "Europe/Amsterdam",         # Singapore
    "Europe/Zurich",          # Switzerland
    "Europe/Budapest",        # Hungary
]

_current_idx: int = -1  # индекс активного конфига в VPN_CONFIGS


def current_timezone() -> str:
    """Возвращает timezone активного VPN-конфига."""
    if _current_idx >= 0 and _current_idx < len(VPN_TIMEZONES):
        return VPN_TIMEZONES[_current_idx]
    return "Europe/Rome"  # fallback — целевой сайт итальянский


def _wg_quick() -> str:
    exe = shutil.which("wg-quick")
    if exe:
        return exe
    raise FileNotFoundError("wg-quick не найден — установи WireGuard: brew install wireguard-tools")


def _tunnel_name(conf_path: str) -> str:
    return os.path.splitext(os.path.basename(conf_path))[0]


def _uninstall(conf_path: str) -> None:
    exe = _wg_quick()
    tunnel_name = _tunnel_name(conf_path)
    try:
        # Передаём полный путь к конфигу — wg-quick его принимает и для down
        r = subprocess.run(
            ["sudo", exe, "down", conf_path],
            capture_output=True, text=True, timeout=30,
        )
        log.info("VPN down %s: rc=%s %s", tunnel_name, r.returncode, (r.stderr or r.stdout).strip()[:200])
    except Exception as e:
        log.warning("VPN down %s: %s", tunnel_name, e)


def _install(conf_path: str) -> bool:
    exe = _wg_quick()
    try:
        r = subprocess.run(
            ["sudo", exe, "up", conf_path],
            capture_output=True, text=True, timeout=30,
        )
        ok = r.returncode == 0
        log.info("VPN up %s: rc=%s %s", conf_path, r.returncode, (r.stderr or r.stdout).strip()[:200])
        return ok
    except Exception as e:
        log.warning("VPN up %s: %s", conf_path, e)
        return False


def _uninstall_all_known() -> None:
    """Гасит все туннели из VPN_CONFIGS перед переключением."""
    for conf in VPN_CONFIGS:
        _uninstall(conf)
    time.sleep(2.0)


def rotate() -> str | None:
    """
    Переключает на следующий конфиг по кругу.
    Возвращает имя туннеля или None при ошибке.
    """
    global _current_idx
    if not VPN_CONFIGS:
        log.warning("VPN_CONFIGS пуст — нечего ротировать")
        return None

    _uninstall_all_known()

    _current_idx = (_current_idx + 1) % len(VPN_CONFIGS)
    conf = VPN_CONFIGS[_current_idx]
    name = _tunnel_name(conf)

    for attempt in range(2):
        if _install(conf):
            break
        log.warning("_install попытка %d не удалась — жду 3с и повторяю", attempt + 1)
        time.sleep(3.0)
    else:
        return None

    time.sleep(4.0)
    log.info("VPN переключён на %s", name)
    return name


def shutdown() -> None:
    """Гасит активный туннель при завершении бота."""
    global _current_idx
    if _current_idx >= 0:
        _uninstall(VPN_CONFIGS[_current_idx])
        _current_idx = -1
