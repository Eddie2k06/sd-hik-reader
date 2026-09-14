#!/usr/bin/env python3
"""
core/deps_check.py

Chequeo de módulos Python opcionales que usa la app (Pillow, OpenCV,
reportlab) al iniciar. Si falta alguno, la GUI ofrece instalarlo con
pip (mismo intérprete que corre la app) antes de seguir. ffmpeg/ffprobe
NO se manejan acá: no son paquetes de pip, tienen su propio chequeo en
core/ffmpeg_setup.py (se instalan aparte, con su propio instalador).

- OPTIONAL_DEPS: la lista de paquetes opcionales conocidos, con el
  nombre de import, el nombre de pip y qué función de la app se pierde
  si no está.
- check_missing(): de esa lista, cuáles no se pueden importar ahora.
- install_packages(...): corre 'pip install' para cada nombre pedido,
  uno por uno (para poder reportar cuál falló sin que un solo error
  tire abajo el resto).
"""

import importlib
import logging
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

log = logging.getLogger("ezviz_reader.deps_check")


@dataclass
class OptionalDep:
    import_name: str    # nombre usado en `import ...`
    pip_name: str        # nombre del paquete en PyPI
    feature: str          # qué función de la app depende de esto


OPTIONAL_DEPS: List[OptionalDep] = [
    OptionalDep('cv2', 'opencv-python-headless', "Vista previa embebida de video"),
    OptionalDep('PIL', 'pillow', "Vista previa embebida, miniaturas e ícono de la app"),
    OptionalDep('reportlab', 'reportlab', "Informe de Hashear en PDF (si falta, se puede generar en TXT)"),
]


def check_missing() -> List[OptionalDep]:
    """Devuelve la sublista de OPTIONAL_DEPS que no se pueden importar
    en este intérprete ahora mismo."""
    missing = []
    for dep in OPTIONAL_DEPS:
        try:
            importlib.import_module(dep.import_name)
        except ImportError:
            missing.append(dep)
        except Exception:
            # Un módulo presente pero roto (ej. DLL de OpenCV faltante)
            # también cuenta como "no disponible" para la app.
            log.warning("Módulo opcional '%s' presente pero no importa bien", dep.import_name, exc_info=True)
            missing.append(dep)
    return missing


def is_frozen() -> bool:
    """True si estamos corriendo como .exe empaquetado (PyInstaller):
    ahí no hay pip disponible para instalar nada en caliente."""
    return getattr(sys, 'frozen', False)


def install_packages(
    pip_names: List[str],
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Tuple[List[str], List[str]]:
    """Instala cada paquete de `pip_names` por separado con
    `sys.executable -m pip install`. Devuelve (instalados_ok, fallidos).
    Sigue con el resto aunque uno falle."""
    ok: List[str] = []
    failed: List[str] = []

    def emit(msg: str):
        if log_callback is not None:
            log_callback(msg)

    if is_frozen():
        emit(
            "Esta es la versión empaquetada (.exe) de la app y no tiene pip "
            "disponible para instalar paquetes."
        )
        return ok, list(pip_names)

    for name in pip_names:
        if cancel_check is not None and cancel_check():
            emit("Instalación cancelada.")
            break
        emit(f"Instalando {name}...")
        try:
            result = subprocess.run(
                [sys.executable, '-m', 'pip', 'install', '--quiet',
                 '--disable-pip-version-check', name],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode == 0:
                ok.append(name)
                emit(f"✅ {name} instalado correctamente")
            else:
                failed.append(name)
                detail = (result.stderr or result.stdout or '').strip()
                emit(f"❌ {name}: {detail[-300:] if detail else 'error desconocido de pip'}")
        except FileNotFoundError:
            failed.append(name)
            emit(f"❌ No se encontró pip en este intérprete para instalar {name}")
        except subprocess.TimeoutExpired:
            failed.append(name)
            emit(f"❌ {name}: tiempo de espera agotado")
        except Exception as e:
            failed.append(name)
            log.exception("Error inesperado instalando %s", name)
            emit(f"❌ {name}: {e}")

    return ok, failed
