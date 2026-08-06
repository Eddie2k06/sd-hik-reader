#!/usr/bin/env python3
"""
core/codex_setup.py

Detección de Codex CLI (el agente de código de OpenAI para la
terminal, paquete npm "@openai/codex") y, en Windows, instalación o
actualización automática usando el instalador oficial de OpenAI.

- find_codex(): busca 'codex' en el PATH.
- get_installed_version(): corre 'codex --version' y devuelve el
  número de versión instalado (o None si no está / falló).
- get_latest_version(): consulta el registry de npm para saber cuál
  es la última versión publicada de @openai/codex.
- check_status(): junta todo lo anterior en un CodexStatus (instalado,
  ruta, versión instalada, última versión, si está desactualizado).
- download_and_install(...): corre el instalador oficial de OpenAI
  (PowerShell en Windows, chatgpt.com/codex/install.ps1). El mismo
  instalador sirve tanto para una instalación nueva como para
  actualizar una instalación existente (la sobreescribe con la
  última versión). Sólo Windows: en Linux/Mac levanta
  CodexInstallError sugiriendo el script install.sh.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.request import Request, urlopen

log = logging.getLogger("ezviz_reader.codex_setup")

IS_WINDOWS = sys.platform.startswith('win')

NPM_LATEST_URL = "https://registry.npmjs.org/@openai/codex/latest"
INSTALL_PS1_URL = "https://chatgpt.com/codex/install.ps1"
INSTALL_SH_URL = "https://chatgpt.com/codex/install.sh"


class CodexInstallError(Exception):
    pass


class CodexInstallCancelled(CodexInstallError):
    pass


def find_codex() -> Optional[str]:
    """Devuelve la ruta al ejecutable de codex si está en el PATH del
    proceso actual, o None si no se encuentra."""
    return shutil.which('codex')


def _dir_in_path_string(dir_path: str, path_string: str) -> bool:
    norm = os.path.normcase(os.path.normpath(dir_path))
    for p in (path_string or '').split(';'):
        p = p.strip()
        if not p:
            continue
        try:
            p = os.path.expandvars(p)
        except Exception:
            pass
        if os.path.normcase(os.path.normpath(p)) == norm:
            return True
    return False


def path_registration_status(codex_path: Optional[str]) -> dict:
    """Chequea si la carpeta que contiene `codex_path` está registrada
    de forma persistente (registro de Windows) en el PATH de usuario
    y/o de sistema -- no sólo visible en el PATH de este proceso. La
    lectura no requiere permisos de administrador. Fuera de Windows,
    o si no se pasó `codex_path`, devuelve ambas en False."""
    result = {'user': False, 'system': False}
    if not IS_WINDOWS or not codex_path:
        return result
    bin_dir = os.path.dirname(codex_path)
    import winreg

    def read_path(hive, key_path):
        try:
            key = winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ)
            try:
                value, _ = winreg.QueryValueEx(key, 'Path')
                return value
            finally:
                winreg.CloseKey(key)
        except FileNotFoundError:
            return ''

    try:
        user_path = read_path(winreg.HKEY_CURRENT_USER, 'Environment')
        result['user'] = _dir_in_path_string(bin_dir, user_path)
    except Exception:
        log.warning("No se pudo leer el PATH de usuario del registro", exc_info=True)
    try:
        system_path = read_path(
            winreg.HKEY_LOCAL_MACHINE,
            r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment')
        result['system'] = _dir_in_path_string(bin_dir, system_path)
    except Exception:
        log.warning("No se pudo leer el PATH de sistema del registro", exc_info=True)
    return result


def _parse_version(text: str) -> Optional[str]:
    """Extrae un número de versión tipo 0.146.0 de la salida de
    'codex --version' (ej. 'codex-cli 0.146.0')."""
    m = re.search(r'(\d+\.\d+\.\d+)', text or '')
    return m.group(1) if m else None


def get_installed_version(codex_path: Optional[str] = None) -> Optional[str]:
    """Corre 'codex --version' y devuelve el número de versión
    instalado, o None si codex no está o el comando falló."""
    exe = codex_path or find_codex()
    if not exe:
        return None
    try:
        kwargs = {}
        if IS_WINDOWS:
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(
            [exe, '--version'], capture_output=True, text=True, timeout=10, **kwargs)
        return _parse_version((result.stdout or '') + (result.stderr or ''))
    except Exception:
        log.exception("No se pudo obtener la versión instalada de Codex")
        return None


def get_latest_version(timeout: float = 10.0) -> Optional[str]:
    """Consulta el registry de npm para la última versión publicada de
    @openai/codex. Devuelve None si falla (sin red, etc.) — no es
    fatal, sólo no se puede comparar contra la instalada."""
    try:
        req = Request(NPM_LATEST_URL, headers={'User-Agent': 'sd_hik_reader'})
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        return data.get('version')
    except Exception:
        log.warning("No se pudo consultar la última versión de Codex en npm", exc_info=True)
        return None


def _version_tuple(v: str):
    parts = []
    for p in v.split('.'):
        m = re.match(r'\d+', p)
        parts.append(int(m.group(0)) if m else 0)
    return tuple(parts)


def is_outdated(installed: Optional[str], latest: Optional[str]) -> bool:
    """True si `installed` es una versión anterior a `latest`. Si falta
    alguno de los dos datos (sin red, o no está instalado), devuelve
    False: no se puede afirmar que esté desactualizado."""
    if not installed or not latest:
        return False
    try:
        return _version_tuple(installed) < _version_tuple(latest)
    except Exception:
        return installed != latest


@dataclass
class CodexStatus:
    installed: bool
    path: Optional[str]
    installed_version: Optional[str]
    latest_version: Optional[str]
    outdated: bool
    in_user_path: bool = False
    in_system_path: bool = False

    @property
    def in_persistent_path(self) -> bool:
        return self.in_user_path or self.in_system_path


def check_status() -> CodexStatus:
    """Chequeo completo: si Codex está instalado, qué versión, cuál es
    la última disponible en npm, si la instalada está vieja, y si la
    carpeta está registrada de forma persistente en las variables de
    entorno (usuario y/o sistema)."""
    path = find_codex()
    installed_version = get_installed_version(path) if path else None
    latest_version = get_latest_version()
    outdated = is_outdated(installed_version, latest_version)
    path_status = path_registration_status(path)
    return CodexStatus(
        installed=path is not None,
        path=path,
        installed_version=installed_version,
        latest_version=latest_version,
        outdated=outdated,
        in_user_path=path_status['user'],
        in_system_path=path_status['system'],
    )


def download_and_install(
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> str:
    """Instala (o actualiza, si ya existe) Codex CLI corriendo el
    instalador oficial de OpenAI. Sólo Windows por ahora. Devuelve la
    ruta del ejecutable ya instalado, o levanta CodexInstallError /
    CodexInstallCancelled."""
    if not IS_WINDOWS:
        raise CodexInstallError(
            "La instalación automática desde la app sólo está implementada "
            "para Windows. En Linux/Mac instalá con: "
            "curl -fsSL https://chatgpt.com/codex/install.sh | sh"
        )

    def emit(line: str):
        line = (line or '').rstrip()
        if line and log_callback is not None:
            log_callback(line)

    # -NonInteractive + CODEX_NON_INTERACTIVE=1: el instalador no debe
    # quedar esperando una tecla dentro de un subprocess sin consola
    # interactiva.
    cmd = [
        'powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'ByPass',
        '-Command',
        f'$env:CODEX_NON_INTERACTIVE="1"; irm {INSTALL_PS1_URL} | iex',
    ]

    emit("Conectando con el instalador oficial de Codex (OpenAI)...")
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as e:
        log.exception("No se pudo lanzar el instalador de Codex")
        raise CodexInstallError(f"No se pudo iniciar el instalador: {e}") from e

    try:
        for line in proc.stdout:
            if cancel_check is not None and cancel_check():
                proc.terminate()
                raise CodexInstallCancelled("Instalación cancelada por el usuario")
            emit(line)
        proc.wait(timeout=180)
    except CodexInstallCancelled:
        raise
    except Exception as e:
        proc.kill()
        log.exception("Error inesperado corriendo el instalador de Codex")
        raise CodexInstallError(f"Error corriendo el instalador: {e}") from e

    if proc.returncode != 0:
        raise CodexInstallError(
            f"El instalador terminó con código {proc.returncode}. "
            f"Revisá el detalle de arriba."
        )

    # El instalador puede haber tocado el PATH del usuario (registro)
    # sin que este proceso lo vea todavía; probamos ubicaciones típicas
    # para que quede usable ya, sin reabrir la app.
    exe = shutil.which('codex')
    if not exe:
        candidates = [
            os.path.join(os.environ.get('USERPROFILE', ''), '.local', 'bin', 'codex.exe'),
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'codex', 'codex.exe'),
            os.path.join(os.environ.get('USERPROFILE', ''), '.codex', 'bin', 'codex.exe'),
        ]
        for c in candidates:
            if os.path.isfile(c):
                bin_dir = os.path.dirname(c)
                if bin_dir not in os.environ.get('PATH', ''):
                    os.environ['PATH'] = bin_dir + os.pathsep + os.environ.get('PATH', '')
                exe = shutil.which('codex')
                if exe:
                    break

    if not exe:
        raise CodexInstallError(
            "El instalador terminó pero no se encontró codex.exe en el PATH. "
            "Puede que necesites abrir una terminal nueva (o reiniciar la app) "
            "para que el cambio de PATH surta efecto."
        )

    log.info("Codex CLI instalado/actualizado correctamente: %s", exe)
    return exe
