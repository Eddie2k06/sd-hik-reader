#!/usr/bin/env python3
"""
core/ffmpeg_setup.py

Detección de ffmpeg/ffprobe y, en Windows, instalación automática si
no están.

- find_ffmpeg(): busca en el PATH y también en la carpeta local
  donde esta misma app deja instalado ffmpeg si lo descargó antes
  (%LOCALAPPDATA%\\sd_hik_reader\\ffmpeg), por si esta sesión de la
  app todavía no lo ve en el PATH del proceso.
- ensure_on_session_path(): si está en esa carpeta local pero no en
  el PATH de este proceso, lo agrega en memoria — para que funcione
  ya, sin tener que reabrir la app después de instalarlo.
- download_and_install(...): descarga el build "essentials" oficial
  recomendado por ffmpeg.org para Windows (gyan.dev), lo extrae, y
  agrega la carpeta bin al PATH del usuario actual (registro
  HKCU\\Environment — no toca el PATH de sistema, no requiere admin).
  Sólo Windows: en Linux/Mac levanta FFmpegInstallError sugiriendo el
  gestor de paquetes del sistema.
"""

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.request import Request, urlopen

log = logging.getLogger("ezviz_reader.ffmpeg_setup")

IS_WINDOWS = sys.platform.startswith('win')

# Build "essentials" oficial recomendado por la documentación de
# ffmpeg.org para Windows (compilado por gyan.dev / "CODEX FFMPEG").
FFMPEG_WIN_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

# Archivo de una sola línea de texto plano con el número de la última
# versión release publicada (ej. "8.1.2"), publicado por el mismo sitio.
FFMPEG_VERSION_URL = "https://www.gyan.dev/ffmpeg/builds/release-version"

APP_DATA_DIR = os.path.join(os.environ.get('LOCALAPPDATA', tempfile.gettempdir()), 'sd_hik_reader')
FFMPEG_INSTALL_DIR = os.path.join(APP_DATA_DIR, 'ffmpeg')


class FFmpegInstallError(Exception):
    pass


class FFmpegInstallCancelled(FFmpegInstallError):
    pass


@dataclass
class DownloadProgress:
    bytes_done: int
    bytes_total: int
    stage: str  # 'download' | 'extract' | 'path'

    @property
    def percent(self) -> float:
        return (self.bytes_done / self.bytes_total * 100) if self.bytes_total else 0.0


def _bundled_bin_dir() -> Optional[str]:
    """Si ya instalamos ffmpeg antes, ubica la carpeta bin\\ con los
    .exe (el zip de gyan.dev trae todo dentro de una carpeta
    versionada, ej. ffmpeg-7.1-essentials_build\\bin)."""
    if not os.path.isdir(FFMPEG_INSTALL_DIR):
        return None
    for name in os.listdir(FFMPEG_INSTALL_DIR):
        candidate = os.path.join(FFMPEG_INSTALL_DIR, name, 'bin')
        if os.path.isfile(os.path.join(candidate, 'ffmpeg.exe')):
            return candidate
    return None


def _win_registry_path_dirs() -> list:
    """Lee el PATH tal como está persistido AHORA MISMO en el registro
    de Windows (sistema + usuario), en vez de depender del PATH que
    este proceso heredó al arrancar. Hace falta para el caso típico:
    el usuario agregó/cambió algo en "Variables de entorno" después
    de abrir la terminal desde la que corre la app -- el proceso ya
    tiene su copia del PATH tomada y nunca la refresca sola, así que
    shutil.which() por sí solo puede no ver un ffmpeg instalado
    recién. Devuelve las carpetas en el mismo orden de precedencia
    real de Windows: primero sistema, después usuario."""
    if not IS_WINDOWS:
        return []
    import winreg

    def read_path(hive, key_path):
        try:
            key = winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ)
            try:
                value, _ = winreg.QueryValueEx(key, 'Path')
                return value
            finally:
                winreg.CloseKey(key)
        except (FileNotFoundError, OSError):
            return ''

    system_path = read_path(
        winreg.HKEY_LOCAL_MACHINE,
        r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment')
    user_path = read_path(winreg.HKEY_CURRENT_USER, 'Environment')

    dirs = []
    for raw in (system_path, user_path):
        for p in (raw or '').split(';'):
            p = p.strip()
            if not p:
                continue
            try:
                p = os.path.expandvars(p)
            except Exception:
                pass
            dirs.append(p)
    return dirs


def _find_ffmpeg_in_registry_path() -> Optional[str]:
    """Busca ffmpeg.exe recorriendo el PATH persistido en el registro
    (sistema + usuario) en este momento, no el PATH heredado por el
    proceso al arrancar."""
    exe_name = 'ffmpeg.exe' if IS_WINDOWS else 'ffmpeg'
    for d in _win_registry_path_dirs():
        if os.path.isfile(os.path.join(d, exe_name)):
            return d
    return None


def find_ffmpeg() -> Optional[str]:
    """Devuelve la carpeta que contiene ffmpeg, probando en orden:
    1) En Windows: PATH persistido en el registro leído AHORA MISMO
       (sistema, después usuario) -- es la fuente de verdad más
       confiable. Va primero a propósito: si sólo mirásemos el PATH
       heredado por el proceso (paso 2) y éste ya "acierta" con algo
       -- aunque sea una instalación vieja que ese proceso ve porque
       la consola/terminal se abrió antes de instalar/actualizar algo
       -- nunca llegaríamos a chequear el registro, que sí tiene el
       dato actualizado. Por eso el chequeo del registro no puede ser
       sólo un fallback para cuando el paso 2 no encuentra nada: tiene
       que ir primero.
    2) PATH heredado por este proceso (`shutil.which`) -- cubre casos
       que no están en el registro, como un venv activado sólo en
       esta sesión (Linux/Mac, o Windows si no hubiera dado con nada
       en el paso 1).
    3) La carpeta local donde esta misma app dejó instalado ffmpeg si
       lo descargó antes.
    Devuelve None si no aparece en ningún lado."""
    if IS_WINDOWS:
        found_dir = _find_ffmpeg_in_registry_path()
        if found_dir:
            return found_dir
    found = shutil.which('ffmpeg')
    if found:
        return os.path.dirname(found)
    return _bundled_bin_dir()


def ensure_on_session_path() -> bool:
    """Si ffmpeg está en el registro de Windows (PATH persistido,
    sistema/usuario) o en nuestra carpeta local, pero todavía no
    visible en el PATH de este proceso, lo agrega en memoria. Devuelve
    True si ffmpeg queda disponible (ya sea porque lo estaba, o porque
    se acaba de agregar).

    Usa la misma lógica que find_ffmpeg() (incluyendo el chequeo
    directo al registro, no sólo el PATH heredado al arrancar el
    proceso) — antes sólo miraba shutil.which()+carpeta local, así que
    podía preguntar "¿instalar ffmpeg?" al abrir la app aunque ya
    estuviera instalado, si el PATH se había actualizado después de
    abrir la terminal."""
    if shutil.which('ffmpeg'):
        return True
    bin_dir = find_ffmpeg()
    if bin_dir and bin_dir not in os.environ.get('PATH', ''):
        os.environ['PATH'] = bin_dir + os.pathsep + os.environ.get('PATH', '')
    return shutil.which('ffmpeg') is not None


def add_to_user_path(bin_dir: str) -> None:
    """Windows: agrega `bin_dir` al PATH del usuario actual (registro,
    no de sistema — no requiere permisos de administrador), y avisa a
    Windows del cambio para que otras apps/consolas que se abran de
    ahora en más ya lo vean, sin tener que cerrar sesión."""
    import ctypes
    import winreg

    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment', 0,
                          winreg.KEY_READ | winreg.KEY_WRITE)
    try:
        try:
            current, _ = winreg.QueryValueEx(key, 'Path')
        except FileNotFoundError:
            current = ''
        parts = [p for p in current.split(';') if p]
        # La quitamos si ya estaba (evita duplicados) y la ponemos
        # PRIMERA. Si sólo se agregara al final (como antes), y el
        # usuario tiene otra instalación de ffmpeg más adelante en el
        # PATH, esa vieja le seguiría ganando en cualquier chequeo
        # futuro -- la actualización "no se notaría nunca".
        parts = [p for p in parts
                 if os.path.normcase(os.path.normpath(p)) != os.path.normcase(os.path.normpath(bin_dir))]
        parts.insert(0, bin_dir)
        winreg.SetValueEx(key, 'Path', 0, winreg.REG_EXPAND_SZ, ';'.join(parts))
    finally:
        winreg.CloseKey(key)

    HWND_BROADCAST = 0xFFFF
    WM_SETTINGCHANGE = 0x1A
    SMTO_ABORTIFHUNG = 0x0002
    ctypes.windll.user32.SendMessageTimeoutW(
        HWND_BROADCAST, WM_SETTINGCHANGE, 0, 'Environment', SMTO_ABORTIFHUNG, 5000, None)


def download_and_install(
    progress_callback: Optional[Callable[[DownloadProgress], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> str:
    """Descarga e instala ffmpeg (sólo Windows). Devuelve la carpeta
    bin resultante. Levanta FFmpegInstallError / FFmpegInstallCancelled
    en caso de fallo/cancelación — no deja el PATH en un estado a
    medias (el registro sólo se toca al final, con todo ya extraído
    y verificado)."""
    if not IS_WINDOWS:
        raise FFmpegInstallError(
            "La instalación automática sólo está implementada para Windows. "
            "En Linux instalá con tu gestor de paquetes (por ejemplo "
            "'sudo apt install ffmpeg') y en macOS con Homebrew "
            "('brew install ffmpeg')."
        )

    os.makedirs(APP_DATA_DIR, exist_ok=True)
    zip_path = os.path.join(APP_DATA_DIR, 'ffmpeg-download.zip')

    # -- descarga --
    try:
        req = Request(FFMPEG_WIN_URL, headers={'User-Agent': 'sd_hik_reader'})
        with urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get('Content-Length', 0))
            done = 0
            with open(zip_path, 'wb') as f:
                while True:
                    if cancel_check is not None and cancel_check():
                        raise FFmpegInstallCancelled("Descarga cancelada por el usuario")
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if progress_callback is not None:
                        progress_callback(DownloadProgress(done, total, 'download'))
    except FFmpegInstallCancelled:
        if os.path.exists(zip_path):
            os.remove(zip_path)
        log.info("Instalación de ffmpeg cancelada por el usuario")
        raise
    except Exception as e:
        if os.path.exists(zip_path):
            os.remove(zip_path)
        log.exception("Falló la descarga de ffmpeg desde %s", FFMPEG_WIN_URL)
        raise FFmpegInstallError(f"No se pudo descargar ffmpeg: {e}") from e

    # -- extracción --
    try:
        if progress_callback is not None:
            progress_callback(DownloadProgress(0, 1, 'extract'))
        if os.path.isdir(FFMPEG_INSTALL_DIR):
            shutil.rmtree(FFMPEG_INSTALL_DIR, ignore_errors=True)
        os.makedirs(FFMPEG_INSTALL_DIR, exist_ok=True)
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(FFMPEG_INSTALL_DIR)
    except Exception as e:
        log.exception("Falló la extracción del paquete de ffmpeg (%s)", zip_path)
        raise FFmpegInstallError(f"No se pudo extraer el paquete descargado: {e}") from e
    finally:
        if os.path.exists(zip_path):
            os.remove(zip_path)

    bin_dir = _bundled_bin_dir()
    if bin_dir is None:
        log.error("Extracción OK pero no se encontró ffmpeg.exe dentro de %s", FFMPEG_INSTALL_DIR)
        raise FFmpegInstallError(
            "Se descargó el paquete pero no se encontró ffmpeg.exe adentro; "
            "puede que la estructura del build haya cambiado en el servidor."
        )

    # -- variables de entorno --
    if progress_callback is not None:
        progress_callback(DownloadProgress(0, 1, 'path'))
    os.environ['PATH'] = bin_dir + os.pathsep + os.environ.get('PATH', '')
    try:
        add_to_user_path(bin_dir)
    except Exception as e:
        # No es fatal: ya quedó usable en esta sesión (os.environ);
        # sólo no persiste para la próxima vez que se abra la app.
        log.warning("No se pudo agregar ffmpeg al PATH permanente: %s", e, exc_info=True)

    log.info("ffmpeg instalado correctamente en %s", bin_dir)

    return bin_dir


# ==========================================================================
# Chequeo de versión (instalada vs. última disponible) y de si ffmpeg
# está persistido en las variables de entorno (no sólo en esta sesión).
# ==========================================================================

def get_installed_version(bin_dir: Optional[str] = None) -> Optional[str]:
    """Corre 'ffmpeg -version' y devuelve el número de versión
    instalado (ej. '7.1.1'), o None si ffmpeg no está o el comando
    falló. Si no se pasa `bin_dir`, usa el que esté en el PATH."""
    if bin_dir:
        exe = os.path.join(bin_dir, 'ffmpeg.exe' if IS_WINDOWS else 'ffmpeg')
        if not os.path.isfile(exe):
            exe = shutil.which('ffmpeg')
    else:
        exe = shutil.which('ffmpeg')
    if not exe:
        return None
    try:
        kwargs = {}
        if IS_WINDOWS:
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(
            [exe, '-version'], capture_output=True, text=True, timeout=10, **kwargs)
        first_line = (result.stdout or '').splitlines()[0] if result.stdout else ''
        m = re.search(r'ffmpeg version (\S+)', first_line)
        if not m:
            return None
        m2 = re.match(r'[\d.]+', m.group(1))
        return m2.group(0).rstrip('.') if m2 else None
    except Exception:
        log.exception("No se pudo obtener la versión instalada de ffmpeg")
        return None


def get_latest_version(timeout: float = 10.0) -> Optional[str]:
    """Consulta gyan.dev (CODEX FFMPEG) por la última versión release
    publicada para Windows. Devuelve None si falla (sin red, etc.) —
    no es fatal, sólo no se puede comparar contra la instalada."""
    try:
        req = Request(FFMPEG_VERSION_URL, headers={'User-Agent': 'sd_hik_reader'})
        with urlopen(req, timeout=timeout) as resp:
            return resp.read().decode('utf-8').strip()
    except Exception:
        log.warning("No se pudo consultar la última versión de ffmpeg (gyan.dev)", exc_info=True)
        return None


def _version_tuple(v: str):
    parts = []
    for p in re.split(r'[.\-]', v):
        m = re.match(r'\d+', p)
        if m:
            parts.append(int(m.group(0)))
    return tuple(parts)


def is_outdated(installed: Optional[str], latest: Optional[str]) -> bool:
    """True si `installed` es una versión anterior a `latest`. Si falta
    alguno de los dos datos, devuelve False (no se puede afirmar que
    esté desactualizado)."""
    if not installed or not latest:
        return False
    try:
        return _version_tuple(installed) < _version_tuple(latest)
    except Exception:
        return installed != latest


def _dir_in_path_string(bin_dir: str, path_string: str) -> bool:
    norm = os.path.normcase(os.path.normpath(bin_dir))
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


def path_registration_status(bin_dir: Optional[str]) -> dict:
    """Chequea si `bin_dir` está registrado de forma persistente (en el
    registro de Windows, no sólo en el PATH de este proceso) en las
    variables de entorno de usuario y/o de sistema. La lectura no
    requiere permisos de administrador (sólo escribir en la de sistema
    los requeriría, y esta app nunca escribe ahí). En plataformas
    distintas de Windows devuelve ambas en False (no aplica)."""
    result = {'user': False, 'system': False}
    if not IS_WINDOWS or not bin_dir:
        return result
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


@dataclass
class FFmpegStatus:
    installed: bool
    bin_dir: Optional[str]
    installed_version: Optional[str]
    latest_version: Optional[str]
    outdated: bool
    in_user_path: bool
    in_system_path: bool

    @property
    def in_persistent_path(self) -> bool:
        return self.in_user_path or self.in_system_path


def check_status() -> FFmpegStatus:
    """Chequeo completo: si ffmpeg está instalado (en esta sesión), qué
    versión, cuál es la última disponible en gyan.dev, si está
    desactualizado, y si la carpeta está registrada de forma
    persistente en las variables de entorno (usuario y/o sistema) —
    en vez de haber quedado sólo agregada en memoria para esta sesión
    de la app."""
    bin_dir = find_ffmpeg()
    installed_version = get_installed_version(bin_dir) if bin_dir else None
    latest_version = get_latest_version()
    outdated = is_outdated(installed_version, latest_version)
    path_status = path_registration_status(bin_dir)
    return FFmpegStatus(
        installed=bin_dir is not None,
        bin_dir=bin_dir,
        installed_version=installed_version,
        latest_version=latest_version,
        outdated=outdated,
        in_user_path=path_status['user'],
        in_system_path=path_status['system'],
    )
