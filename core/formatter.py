#!/usr/bin/env python3
"""
core/formatter.py

Formateo de BAJO NIVEL de una tarjeta SD: sobrescribe todo el dispositivo
(o volumen) con un patrón fijo (por defecto ceros), cluster por cluster,
informando progreso. Es una operación DESTRUCTIVA e IRREVERSIBLE — la
UI debe pedir confirmación explícita antes de llamar a esto (ver
gui/dialogs.py: FormatDialog).

Notas de uso:
- En Windows, si `path` es la raíz de una unidad montada (ej. "D:\\"),
  NO se puede escribir con open() normal (eso abre la carpeta, no el
  volumen) — internamente se convierte a la ruta de dispositivo de
  volumen crudo ("\\\\.\\D:") y se abre vía CreateFile + bloqueo de
  volumen (FSCTL_LOCK_VOLUME). Esto casi siempre requiere permisos de
  administrador igual, aunque el volumen esté "sólo montado".
- Para formatear el DISPOSITIVO CRUDO completo (ej. "\\\\.\\PhysicalDrive2"
  en Windows o "/dev/sdX" en Linux) hacen falta permisos de
  administrador/root, y el volumen no debe estar montado/en uso.
"""

import os
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional

IS_WINDOWS = sys.platform.startswith('win')

DEFAULT_CLUSTER_SIZE = 4 * 1024 * 1024  # 4 MiB: bloque de escritura grande.
# Nota: esto NO es el "cluster" real del sistema de archivos FAT32 (típ.
# 32 KiB) — es sólo el tamaño del buffer que se escribe por syscall.
# Con SD/USB reales, escribir en bloques de 32 KiB deja el proceso
# limitado por la cantidad de syscalls (miles de write() por GB) en vez
# de por la velocidad real del dispositivo. Con bloques de varios MiB
# se llega mucho más cerca de la velocidad de escritura sostenida real
# de la tarjeta. El usuario puede seguir ajustando este tamaño desde
# el diálogo de Formatear si lo necesita.


class FormatError(Exception):
    pass


class FormatCancelled(FormatError):
    pass


@dataclass
class FormatProgress:
    bytes_written: int
    total_bytes: int
    cluster_index: int
    total_clusters: int
    speed_bps: float
    elapsed_seconds: float

    @property
    def percent(self) -> float:
        return (self.bytes_written / self.total_bytes * 100) if self.total_bytes else 0.0

    @property
    def eta_seconds(self) -> float:
        remaining = self.total_bytes - self.bytes_written
        return (remaining / self.speed_bps) if self.speed_bps > 0 else float('inf')


def _win_volume_device_path(path: str) -> Optional[str]:
    """Si `path` es la raíz de una unidad tipo 'D:\\' o 'D:', devuelve
    la ruta de dispositivo de volumen crudo '\\\\.\\D:' (sin barra
    final) que Windows exige para escritura de bajo nivel. Si no
    matchea ese patrón (ya es un path de dispositivo, o un path
    POSIX), devuelve None y el llamador sigue con open() normal."""
    p = path.rstrip('\\/')
    if len(p) == 2 and p[1] == ':' and p[0].isalpha():
        return f"\\\\.\\{p}"
    return None


class _WinVolumeFile:
    """Envoltorio de un HANDLE de volumen de Windows abierto vía
    CreateFile, expuesto como objeto tipo-archivo (.write/.flush/
    .fileno/.close) para que el resto del código no tenga que
    distinguir entre esto y un archivo normal.

    Antes de escribir, intenta bloquear el volumen (FSCTL_LOCK_VOLUME):
    Windows normalmente rechaza escrituras directas al dispositivo
    mientras el volumen está montado/en uso si no se hace esto. Si el
    bloqueo falla igual seguimos: algunos casos (SD sin sistema de
    archivos reconocible) permiten escribir sin bloquear."""

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x1
    FILE_SHARE_WRITE = 0x2
    OPEN_EXISTING = 3
    FSCTL_LOCK_VOLUME = 0x00090018
    FSCTL_UNLOCK_VOLUME = 0x0009001C
    INVALID_HANDLE_VALUE = 0xFFFFFFFFFFFFFFFF

    def __init__(self, device_path: str):
        import ctypes
        self._ctypes = ctypes
        self.device_path = device_path
        self._kernel32 = ctypes.windll.kernel32
        self._kernel32.CreateFileW.restype = ctypes.c_void_p
        self._handle = None
        self._fileobj = None

    def open(self):
        ctypes = self._ctypes
        handle = self._kernel32.CreateFileW(
            ctypes.c_wchar_p(self.device_path),
            self.GENERIC_READ | self.GENERIC_WRITE,
            self.FILE_SHARE_READ | self.FILE_SHARE_WRITE,
            None, self.OPEN_EXISTING, 0, None,
        )
        if not handle or handle == self.INVALID_HANDLE_VALUE:
            err = ctypes.get_last_error()
            raise FormatError(
                f"No se pudo abrir '{self.device_path}' para escritura de bajo nivel. "
                f"Verificá que el dispositivo no esté en uso, que hayas elegido la "
                f"unidad correcta, y que la app corra como ADMINISTRADOR (la escritura "
                f"directa a un volumen casi siempre lo requiere en Windows aunque esté "
                f"'sólo montado'). Código de error de Windows: {err}"
            )
        self._handle = handle

        bytes_returned = ctypes.c_ulong(0)
        self._kernel32.DeviceIoControl(
            handle, self.FSCTL_LOCK_VOLUME, None, 0, None, 0,
            ctypes.byref(bytes_returned), None
        )  # best-effort: si falla, igual intentamos escribir más abajo

        import msvcrt
        fd = msvcrt.open_osfhandle(handle, 0)
        self._fileobj = os.fdopen(fd, 'r+b')
        return self._fileobj

    def write(self, data):
        return self._fileobj.write(data)

    def flush(self):
        return self._fileobj.flush()

    def fileno(self):
        return self._fileobj.fileno()

    def close(self):
        ctypes = self._ctypes
        try:
            self._fileobj.flush()
        except Exception:
            pass
        if self._handle:
            bytes_returned = ctypes.c_ulong(0)
            self._kernel32.DeviceIoControl(
                self._handle, self.FSCTL_UNLOCK_VOLUME, None, 0, None, 0,
                ctypes.byref(bytes_returned), None
            )
        # os.fdopen()/msvcrt.open_osfhandle() se adueña del HANDLE: cerrar
        # el file object ya cierra el HANDLE subyacente (no hace falta, y
        # sería incorrecto, un CloseHandle() aparte acá).
        try:
            self._fileobj.close()
        except Exception:
            pass


def low_level_format(
    path: str,
    total_bytes: int,
    cluster_size: int = DEFAULT_CLUSTER_SIZE,
    pattern: bytes = b'\x00',
    progress_callback: Optional[Callable[[FormatProgress], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    progress_every_n_clusters: int = 4,
) -> int:
    """
    Sobrescribe `total_bytes` bytes del archivo/dispositivo en `path` con
    el patrón dado, en bloques de `cluster_size`.

    - progress_callback(FormatProgress): se llama cada
      `progress_every_n_clusters` clusters, para no saturar la UI.
    - cancel_check(): si devuelve True, se interrumpe (levanta
      FormatCancelled). El archivo queda parcialmente sobrescrito.

    Devuelve la cantidad total de bytes efectivamente escritos.
    """
    if total_bytes <= 0:
        raise FormatError("Tamaño de destino inválido (0 bytes)")
    if cluster_size <= 0:
        raise FormatError("Tamaño de cluster inválido")
    if not pattern:
        pattern = b'\x00'

    # Buffer de un cluster completo, repitiendo el patrón si hace falta.
    reps = (cluster_size // len(pattern)) + 1
    full_buf = (pattern * reps)[:cluster_size]

    total_clusters = (total_bytes + cluster_size - 1) // cluster_size

    written = 0
    cluster_index = 0
    start_t = time.time()
    last_report_t = start_t

    win_vol = None
    device_path = _win_volume_device_path(path) if IS_WINDOWS else None
    try:
        if device_path is not None:
            # 'path' es una letra de unidad montada (ej. "D:\\"): abrirla
            # con open() normal falla siempre (es una carpeta, no un
            # archivo). Hace falta el path de dispositivo de volumen
            # crudo + CreateFile + bloqueo de volumen.
            win_vol = _WinVolumeFile(device_path)
            f = win_vol.open()
        else:
            mode = 'r+b' if os.path.exists(path) else 'wb'
            f = open(path, mode)
    except OSError as e:
        raise FormatError(
            f"No se pudo abrir '{path}' para escritura. "
            f"Verificá que el dispositivo no esté en uso y que tengas permisos "
            f"de administrador si es un dispositivo crudo. Detalle: {e}"
        ) from e

    try:
        while written < total_bytes:
            if cancel_check is not None and cancel_check():
                raise FormatCancelled("Formateo cancelado por el usuario")

            chunk_size = min(cluster_size, total_bytes - written)
            f.write(full_buf[:chunk_size])
            written += chunk_size
            cluster_index += 1

            now = time.time()
            if progress_callback is not None and (
                cluster_index % progress_every_n_clusters == 0
                or written >= total_bytes
                or now - last_report_t > 0.5
            ):
                elapsed = now - start_t
                speed = written / elapsed if elapsed > 0 else 0.0
                progress_callback(FormatProgress(
                    bytes_written=written,
                    total_bytes=total_bytes,
                    cluster_index=cluster_index,
                    total_clusters=total_clusters,
                    speed_bps=speed,
                    elapsed_seconds=elapsed,
                ))
                last_report_t = now

        f.flush()
        os.fsync(f.fileno())
    finally:
        if win_vol is not None:
            win_vol.close()
        else:
            f.close()

    return written


def format_eta_text(seconds: float) -> str:
    if seconds == float('inf') or seconds < 0:
        return "calculando..."
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}h {m:02d}m {s:02d}s"
    return f"{m:02d}m {s:02d}s"


def format_speed_text(bps: float) -> str:
    mbps = bps / (1024 * 1024)
    return f"{mbps:.1f} MB/s"
