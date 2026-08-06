#!/usr/bin/env python3
"""
core/drives.py

Detección de unidades disponibles para el menú "SD":
- En Windows: recorre letras de unidad y clasifica por tipo (removible,
  fija, red, CD, etc.) usando GetDriveTypeW.
- En Linux/Mac: busca puntos de montaje típicos (/media, /run/media,
  /Volumes).

No requiere dependencias externas (sólo ctypes / os / string).
"""

import ctypes
import os
import string
import sys
from dataclasses import dataclass
from typing import List

IS_WINDOWS = sys.platform.startswith('win')

# Constantes de GetDriveTypeW (Windows)
DRIVE_UNKNOWN = 0
DRIVE_NO_ROOT_DIR = 1
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6

DRIVE_TYPE_LABELS = {
    DRIVE_UNKNOWN: "Desconocida",
    DRIVE_NO_ROOT_DIR: "Sin unidad",
    DRIVE_REMOVABLE: "Removible (SD/USB)",
    DRIVE_FIXED: "Fija",
    DRIVE_REMOTE: "Red",
    DRIVE_CDROM: "CD/DVD",
    DRIVE_RAMDISK: "RAM disk",
}


@dataclass
class DriveInfo:
    path: str
    label: str
    drive_type: int
    is_removable: bool
    has_ezviz_index: bool
    total_bytes: int = 0
    free_bytes: int = 0

    @property
    def type_label(self) -> str:
        return DRIVE_TYPE_LABELS.get(self.drive_type, "Desconocida")

    def display_text(self) -> str:
        size_txt = f" — {self.total_bytes / (1024**3):.1f} GB" if self.total_bytes else ""
        marker = " [índice EZVIZ detectado]" if self.has_ezviz_index else ""
        return f"{self.path}  ({self.type_label}){size_txt}{marker}"


def looks_like_ezviz_root(path: str) -> bool:
    """Chequeo rápido y superficial: ¿esta carpeta parece la raíz de una
    tarjeta EZVIZ/Hikvision? (info.bin en la raíz, o datadirN/index00.bin)."""
    try:
        if os.path.exists(os.path.join(path, 'info.bin')):
            return True
        for entry in os.listdir(path):
            if entry.lower().startswith('datadir'):
                sub = os.path.join(path, entry)
                if os.path.isdir(sub) and os.path.exists(os.path.join(sub, 'index00.bin')):
                    return True
    except OSError:
        pass
    return False


# Alias privado retrocompatible (algunas partes internas usaban el nombre viejo).
_looks_like_ezviz_root = looks_like_ezviz_root


def _disk_usage(path: str):
    try:
        import shutil as _shutil
        usage = _shutil.disk_usage(path)
        return usage.total, usage.free
    except OSError:
        return 0, 0


def list_windows_drives() -> List[DriveInfo]:
    drives = []
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for i, letter in enumerate(string.ascii_uppercase):
        if not (bitmask >> i) & 1:
            continue
        root = f"{letter}:\\"
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
        total, free = _disk_usage(root) if drive_type in (DRIVE_REMOVABLE, DRIVE_FIXED) else (0, 0)
        drives.append(DriveInfo(
            path=root,
            label=letter,
            drive_type=drive_type,
            is_removable=(drive_type == DRIVE_REMOVABLE),
            has_ezviz_index=_looks_like_ezviz_root(root) if drive_type in (DRIVE_REMOVABLE, DRIVE_FIXED) else False,
            total_bytes=total,
            free_bytes=free,
        ))
    return drives


def list_posix_mounts() -> List[DriveInfo]:
    drives = []
    candidate_roots = ['/media', '/run/media', '/Volumes', '/mnt']
    seen = set()
    for root in candidate_roots:
        if not os.path.isdir(root):
            continue
        try:
            for entry in os.listdir(root):
                sub = os.path.join(root, entry)
                if os.path.isdir(sub):
                    # /run/media/<usuario>/<volumen>
                    children = [sub]
                    try:
                        children += [os.path.join(sub, c) for c in os.listdir(sub) if os.path.isdir(os.path.join(sub, c))]
                    except OSError:
                        pass
                    for path in children:
                        if path in seen:
                            continue
                        seen.add(path)
                        total, free = _disk_usage(path)
                        if total == 0:
                            continue
                        drives.append(DriveInfo(
                            path=path,
                            label=os.path.basename(path),
                            drive_type=DRIVE_REMOVABLE,
                            is_removable=True,
                            has_ezviz_index=_looks_like_ezviz_root(path),
                            total_bytes=total,
                            free_bytes=free,
                        ))
        except OSError:
            continue
    return drives


def list_available_drives() -> List[DriveInfo]:
    """Punto de entrada único: devuelve la lista de unidades candidatas
    para el menú SD, priorizando las que parecen tener un índice EZVIZ."""
    try:
        drives = list_windows_drives() if IS_WINDOWS else list_posix_mounts()
    except Exception:
        drives = []
    drives.sort(key=lambda d: (not d.has_ezviz_index, not d.is_removable, d.path))
    return drives


# --------------------------------------------------------------------------
# Tamaño / geometría de un dispositivo crudo (usado también por el
# formateador de bajo nivel)
# --------------------------------------------------------------------------
def get_path_size_bytes(path: str) -> int:
    """Tamaño en bytes de un volumen montado (carpeta) o de un
    dispositivo crudo (\\\\.\\PhysicalDriveN en Windows, /dev/sdX en Linux)."""
    if os.path.isdir(path):
        total, _free = _disk_usage(path)
        return total

    if IS_WINDOWS:
        return _win_raw_device_size(path)
    return _posix_raw_device_size(path)


def _win_raw_device_size(path: str) -> int:
    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x1
    FILE_SHARE_WRITE = 0x2
    OPEN_EXISTING = 3

    handle = ctypes.windll.kernel32.CreateFileW(
        ctypes.c_wchar_p(path), GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE, None, OPEN_EXISTING, 0, None
    )
    if handle == -1 or handle == 0xFFFFFFFF:
        raise OSError(f"No se pudo abrir el dispositivo {path} (¿faltan permisos de administrador?)")

    try:
        IOCTL_DISK_GET_LENGTH_INFO = 0x7405C
        length = ctypes.c_ulonglong(0)
        bytes_returned = ctypes.c_ulong(0)
        ok = ctypes.windll.kernel32.DeviceIoControl(
            handle, IOCTL_DISK_GET_LENGTH_INFO, None, 0,
            ctypes.byref(length), ctypes.sizeof(length),
            ctypes.byref(bytes_returned), None
        )
        if not ok:
            raise OSError(f"DeviceIoControl falló para {path}")
        return length.value
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _posix_raw_device_size(path: str) -> int:
    import fcntl
    BLKGETSIZE64 = 0x80081272
    with open(path, 'rb') as f:
        buf = ctypes.create_string_buffer(8)
        fcntl.ioctl(f.fileno(), BLKGETSIZE64, buf)
        return int.from_bytes(buf.raw, byteorder='little')


# --------------------------------------------------------------------------
# Sistema de archivos e info consolidada para la ventana "Ver"
# --------------------------------------------------------------------------
def get_filesystem_type(path: str) -> str:
    """Nombre del sistema de archivos del volumen que contiene `path`
    (ej. 'FAT32', 'exFAT', 'NTFS', 'ext4'...). Devuelve 'N/D' si no se
    puede determinar."""
    if not path:
        return "N/D"

    if IS_WINDOWS:
        try:
            root = os.path.splitdrive(os.path.abspath(path))[0] + '\\'
            fs_name_buf = ctypes.create_unicode_buffer(261)
            vol_name_buf = ctypes.create_unicode_buffer(261)
            ok = ctypes.windll.kernel32.GetVolumeInformationW(
                ctypes.c_wchar_p(root), vol_name_buf, 261, None, None, None,
                fs_name_buf, 261,
            )
            if ok:
                return fs_name_buf.value or "N/D"
        except Exception:
            pass
        return "N/D"

    # POSIX: buscar la línea de /proc/mounts cuyo punto de montaje sea el
    # prefijo más largo del path (mejor aproximación disponible sin
    # dependencias externas).
    try:
        target = os.path.realpath(path)
        best_match = ""
        best_fs = "N/D"
        with open('/proc/mounts', 'r') as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mount_point, fs_type = parts[1], parts[2]
                if target.startswith(mount_point) and len(mount_point) > len(best_match):
                    best_match, best_fs = mount_point, fs_type
        return best_fs
    except OSError:
        return "N/D"


@dataclass
class PathInfo:
    """Info consolidada de un destino (volumen montado) para la ventana
    'Ver'. A diferencia de DriveInfo, no requiere que el path sea la raíz
    de una unidad detectada por list_available_drives()."""
    path: str
    exists: bool
    drive_type: int
    total_bytes: int
    free_bytes: int
    filesystem: str
    has_ezviz_index: bool

    @property
    def type_label(self) -> str:
        return DRIVE_TYPE_LABELS.get(self.drive_type, "Desconocida")

    @property
    def used_bytes(self) -> int:
        return max(self.total_bytes - self.free_bytes, 0)

    @property
    def used_pct(self) -> float:
        return (self.used_bytes / self.total_bytes * 100) if self.total_bytes else 0.0


def describe_path(path: str) -> PathInfo:
    """Reúne toda la info de volumen disponible para `path` (usada por la
    ventana 'Ver'). No lanza excepción: si algo falla, devuelve valores
    por defecto en ese campo."""
    exists = bool(path) and os.path.isdir(path)
    if not exists:
        return PathInfo(path=path, exists=False, drive_type=DRIVE_UNKNOWN,
                         total_bytes=0, free_bytes=0, filesystem="N/D",
                         has_ezviz_index=False)

    drive_type = DRIVE_UNKNOWN
    try:
        if IS_WINDOWS:
            root = os.path.splitdrive(os.path.abspath(path))[0] + '\\'
            drive_type = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
        else:
            drive_type = DRIVE_REMOVABLE  # mejor esfuerzo en POSIX
    except Exception:
        pass

    total, free = _disk_usage(path)
    return PathInfo(
        path=path,
        exists=True,
        drive_type=drive_type,
        total_bytes=total,
        free_bytes=free,
        filesystem=get_filesystem_type(path),
        has_ezviz_index=looks_like_ezviz_root(path),
    )
