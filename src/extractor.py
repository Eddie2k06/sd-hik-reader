"""
sd-hik-reader · extractor.py
Extracción de clips desde archivos HIV (MPEG-PS raw) usando ffmpeg.

Estrategia documentada en HIK_SD_Format.md §4.7:
  1. Leer el primer pack del HIV (System Header + PS Map + IDR/SPS/PPS)
  2. Leer los bytes exactos gl_s → gl_e del chunk deseado
  3. Concatenar en .mpg temporal
  4. ffmpeg convierte a MP4 estándar reproducible
"""

import os
import shutil
import subprocess
import tempfile

from .parser import read_ps_header, read_chunk, chunk_duration, fmt_sz

# ── Localización de ffmpeg ────────────────────────────────────────────────────

_WINDOWS_PATHS = [
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
    r"C:\tools\ffmpeg\bin\ffmpeg.exe",
]

def find_ffmpeg() -> str | None:
    """Busca ffmpeg en PATH y ubicaciones comunes de Windows."""
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    for p in _WINDOWS_PATHS:
        if os.path.isfile(p):
            return p
    return None


def ffmpeg_version(ffmpeg_path: str) -> str:
    """Retorna la versión de ffmpeg como string."""
    try:
        r = subprocess.run(
            [ffmpeg_path, "-version"],
            capture_output=True, text=True, timeout=5
        )
        first = r.stdout.splitlines()[0] if r.stdout else ""
        return first.split("Copyright")[0].strip()
    except Exception:
        return "desconocida"


# ── Extracción principal ──────────────────────────────────────────────────────

def extract_clip(
    hiv_path: str,
    gl_s: int,
    gl_e: int,
    out_path: str,
    resolution: str | None = None,
    progress_cb=None,
) -> dict:
    """
    Extrae un clip del HIV y lo guarda como MP4 estándar.

    Parámetros:
        hiv_path    : ruta al archivo hivXXXXX.mp4 principal
        gl_s        : offset global de inicio del chunk (de OFNI +68)
        gl_e        : offset global de fin del chunk   (de OFNI +72)
        out_path    : ruta de salida .mp4
        resolution  : None = copia directa | "WxH" = reescala con libx265
        progress_cb : callable(msg: str) para reportar progreso

    Retorna dict con: success, out_size, duration, log, error
    """
    result = {
        "success":  False,
        "out_path": out_path,
        "out_size": 0,
        "duration": 0.0,
        "log":      [],
        "error":    None,
    }

    def log(msg):
        result["log"].append(msg)
        if progress_cb:
            progress_cb(msg)

    # 1. Verificar ffmpeg
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        result["error"] = (
            "ffmpeg no encontrado.\n"
            "Descargalo desde https://ffmpeg.org/download.html\n"
            "y agregá la carpeta bin/ al PATH del sistema."
        )
        return result

    # 2. Leer cabecera del sistema del HIV (primer pack MPEG-PS)
    log("Leyendo cabecera MPEG-PS del sistema…")
    try:
        ps_header = read_ps_header(hiv_path)
        log(f"  Cabecera: {len(ps_header)} bytes")
    except Exception as ex:
        result["error"] = f"Error leyendo cabecera HIV: {ex}"
        return result

    # 3. Leer chunk del clip (con soporte cross-block)
    chunk_size = gl_e - gl_s
    log(f"Leyendo chunk: gl_s={gl_s:,}  gl_e={gl_e:,}  ({fmt_sz(chunk_size)})")
    try:
        chunk_data = read_chunk(hiv_path, gl_s, gl_e)
        log(f"  Chunk leído: {len(chunk_data):,} bytes")
    except Exception as ex:
        result["error"] = f"Error leyendo chunk del HIV: {ex}"
        return result

    # 4. Calcular duración desde SCR
    dur = chunk_duration(chunk_data)
    result["duration"] = dur
    log(f"  Duración SCR: {dur:.3f}s")

    # 5. Escribir MPEG-PS temporal
    tmp_path = None
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".mpg", delete=False)
        tmp.write(ps_header)
        tmp.write(chunk_data)
        tmp.flush()
        tmp.close()
        tmp_path = tmp.name
        log(f"  Temporal: {tmp_path} ({fmt_sz(len(ps_header) + len(chunk_data))})")

        # 6. Construir comando ffmpeg
        if resolution:
            w, h = resolution.split("x")
            cmd = [
                ffmpeg, "-y",
                "-i", tmp_path,
                "-vf", f"scale={w}:{h}",
                "-c:v", "libx265", "-preset", "fast", "-crf", "23",
                "-an",
                "-movflags", "+faststart",
                out_path,
            ]
        else:
            cmd = [
                ffmpeg, "-y",
                "-i", tmp_path,
                "-c:v", "copy",
                "-an",
                "-movflags", "+faststart",
                out_path,
            ]

        log(f"Ejecutando ffmpeg: {' '.join(cmd)}")

        # 7. Ejecutar ffmpeg
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                result["log"].append(line)
                if progress_cb:
                    progress_cb(line)
        proc.wait()

        if proc.returncode != 0:
            tail = "\n".join(result["log"][-15:])
            result["error"] = f"ffmpeg falló (código {proc.returncode}):\n{tail}"
            return result

        # 8. Verificar salida
        if not os.path.exists(out_path):
            result["error"] = "ffmpeg no generó archivo de salida."
            return result

        out_size = os.path.getsize(out_path)
        if out_size < 1024:
            result["error"] = (
                f"Archivo de salida demasiado pequeño ({out_size} bytes).\n"
                f"El HIV puede estar dañado o el chunk no contiene video válido.\n\n"
                f"Últimas líneas ffmpeg:\n" + "\n".join(result["log"][-8:])
            )
            return result

        result["success"]  = True
        result["out_size"] = out_size
        log(f"✔ Clip guardado: {out_path} ({fmt_sz(out_size)})")

    except Exception as ex:
        result["error"] = str(ex)

    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    return result
