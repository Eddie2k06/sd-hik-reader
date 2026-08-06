#!/usr/bin/env python3
"""
core/config.py

Configuración persistente de la app en JSON: última(s) carpeta(s)
usadas (origen SD, descargas, extracción, informes), tamaño de
ventana, y qué paneles/pestañas quedaron ocultos desde el menú Ver.

Se guarda en <tmp del sistema>/sd_hik_reader/config.json.

Nota: al estar en el directorio temporal del sistema, el SO puede
limpiar este archivo (por ejemplo al reiniciar Windows). Si en algún
momento se prefiere que sobreviva siempre, el único cambio necesario
es CONFIG_DIR más abajo (por ejemplo a %LOCALAPPDATA%).
"""

import json
import logging
import os
import sys
import tempfile

log = logging.getLogger("ezviz_reader.config")

CONFIG_DIR = os.path.join(tempfile.gettempdir(), "sd_hik_reader")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# Carpeta raíz del proyecto (o carpeta temporal de extracción de
# PyInstaller --onefile, vía sys._MEIPASS), para ubicar recursos
# empaquetados como sd-card.ico sin importar desde dónde se corra.
_APP_ROOT = getattr(sys, '_MEIPASS', os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

APP_ICON_PATH = os.path.join(_APP_ROOT, 'sd-card.ico')


def resource_path(*parts) -> str:
    """Ruta absoluta a un recurso empaquetado con la app (sd-card.ico,
    etc.), funcionando tanto desde código fuente como empaquetado con
    PyInstaller --onefile."""
    return os.path.join(_APP_ROOT, *parts)

DEFAULT_CONFIG = {
    "window": {
        "width": 1250,
        "height": 820,
    },
    "panels": {
        "show_info": True,
        "show_filter": True,
        "show_timeline": True,
        "show_preview": True,
        "show_detail": True,
    },
    "paths": {
        "last_source_dir": "",
        "last_download_dir": "",
        "last_extract_dir": "",
        "last_report_dir": "",
    },
}


def _merge_defaults(data: dict, defaults: dict) -> dict:
    """Completa en `data` las claves que falten respecto a `defaults`,
    recursivamente, sin pisar lo que ya esté presente y sea válido.
    Así, si se agrega una clave nueva en una versión futura, la
    config vieja del usuario no rompe nada."""
    merged = dict(defaults)
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(defaults.get(key), dict):
            merged[key] = _merge_defaults(value, defaults[key])
        else:
            merged[key] = value
    return merged


def load_config() -> dict:
    """Lee la config guardada. Si no existe o está corrupta, devuelve
    los valores por defecto (nunca lanza excepción)."""
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("config.json no contiene un objeto JSON")
        return _merge_defaults(data, DEFAULT_CONFIG)
    except (FileNotFoundError, json.JSONDecodeError, ValueError, OSError) as e:
        log.info("Config no encontrada o inválida (%s); uso valores por defecto", e)
        return json.loads(json.dumps(DEFAULT_CONFIG))  # copia profunda


def save_config(data: dict) -> None:
    """Guarda la config. Escribe primero a un archivo temporal y
    hace rename atómico, para no dejar un config.json corrupto si
    la app se cierra a mitad de la escritura."""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp_path = CONFIG_PATH + ".tmp"
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, CONFIG_PATH)
    except OSError as e:
        log.warning("No se pudo guardar la configuración: %s", e)


def get_last_dir(key: str) -> str:
    """Atajo para leer una sola ruta guardada (ej. 'last_source_dir'),
    sin tener que cargar y navegar todo el dict a mano."""
    return load_config().get('paths', {}).get(key, '') or ''


def set_last_dir(key: str, path: str) -> None:
    """Atajo para guardar una sola ruta, preservando el resto de la
    config tal como está en disco (recarga antes de escribir)."""
    if not path:
        return
    cfg = load_config()
    cfg.setdefault('paths', {})[key] = path
    save_config(cfg)
