#!/usr/bin/env python3
"""
core/hashing.py

Calcula hashes de los archivos ya extraídos y genera un informe (.txt
y/o .pdf) con el detalle, pensado como respaldo de cadena de custodia
sobre los clips/imágenes descargados de la SD.

Algoritmos soportados: MD5, SHA-1, SHA-256, SHA-512 (vía hashlib) y
CRC32 (vía zlib). SHA-256 es el recomendado por defecto para cadena
de custodia; el resto son opcionales.
"""

import hashlib
import logging
import os
import subprocess
import sys
import time
import zlib
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Optional, Sequence

log = logging.getLogger("ezviz_reader.hashing")

ALL_ALGORITHMS = ('sha256', 'sha1', 'md5', 'sha512', 'crc32')
DEFAULT_ALGORITHMS = ('sha256',)
CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB

# Extensiones que se excluyen por defecto al tildar "Excluir temporales"
DEFAULT_EXCLUDE_SUFFIXES = ('.tmp', '.log')


class HashCancelled(Exception):
    pass


@dataclass
class FileHashEntry:
    path: str
    size_bytes: int
    modified: str
    hashes: dict  # {'sha256': '...', 'md5': '...', ...}


@dataclass
class HashProgress:
    file_index: int
    total_files: int
    current_path: str
    file_bytes_done: int
    file_bytes_total: int
    total_bytes_done: int
    total_bytes_all: int
    speed_bps: float
    elapsed_seconds: float

    @property
    def percent(self) -> float:
        return (self.total_bytes_done / self.total_bytes_all * 100) if self.total_bytes_all else 0.0

    @property
    def file_percent(self) -> float:
        return (self.file_bytes_done / self.file_bytes_total * 100) if self.file_bytes_total else 0.0

    @property
    def eta_seconds(self) -> float:
        remaining = self.total_bytes_all - self.total_bytes_done
        return (remaining / self.speed_bps) if self.speed_bps > 0 else float('inf')


def collect_files(
    target: str,
    recursive: bool = True,
    exclude_suffixes: Sequence[str] = (),
) -> List[str]:
    """Si `target` es una carpeta, devuelve los archivos dentro (recursivo
    salvo que recursive=False). Si es un archivo, devuelve una lista de un
    elemento. `exclude_suffixes` filtra por extensión (case-insensitive)."""
    if os.path.isfile(target):
        return [target]

    exclude = tuple(s.lower() for s in exclude_suffixes)

    def _keep(name: str) -> bool:
        return not exclude or not name.lower().endswith(exclude)

    files = []
    if recursive:
        for dirpath, _dirnames, filenames in os.walk(target):
            for name in filenames:
                if _keep(name):
                    files.append(os.path.join(dirpath, name))
    else:
        with os.scandir(target) as it:
            for entry in it:
                if entry.is_file() and _keep(entry.name):
                    files.append(entry.path)
    files.sort()
    return files


def count_subfolders(target: str) -> int:
    """Cuenta subcarpetas (recursivo) bajo `target`, para el resumen de
    la pantalla 'carpeta seleccionada'."""
    if os.path.isfile(target):
        return 0
    total = 0
    for _dirpath, dirnames, _filenames in os.walk(target):
        total += len(dirnames)
    return total


def hash_file(
    path: str,
    algorithms: Sequence[str] = DEFAULT_ALGORITHMS,
    chunk_progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> dict:
    hashers = {}
    want_crc32 = False
    for algo in algorithms:
        if algo == 'crc32':
            want_crc32 = True
        else:
            hashers[algo] = hashlib.new(algo)
    crc_value = 0

    total_size = os.path.getsize(path)
    done = 0
    with open(path, 'rb') as f:
        while True:
            if cancel_check is not None and cancel_check():
                raise HashCancelled(f"Hasheo cancelado por el usuario en '{path}'")
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            for h in hashers.values():
                h.update(chunk)
            if want_crc32:
                crc_value = zlib.crc32(chunk, crc_value)
            done += len(chunk)
            if chunk_progress_cb is not None:
                chunk_progress_cb(done, total_size)

    result = {algo: h.hexdigest() for algo, h in hashers.items()}
    if want_crc32:
        result['crc32'] = format(crc_value & 0xFFFFFFFF, '08x')
    return result


def build_hash_entries(
    files: Sequence[str],
    algorithms: Sequence[str] = DEFAULT_ALGORITHMS,
    progress_callback: Optional[Callable[[HashProgress], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    progress_every_bytes: int = 8 * 1024 * 1024,
) -> List[FileHashEntry]:
    """Hashea todos los `files` con los `algorithms` pedidos.

    `progress_callback(HashProgress)` se llama periódicamente (por
    archivo y por chunk dentro del archivo actual) para poder animar
    una barra general + el progreso del archivo en curso en la UI.
    `cancel_check()` interrumpe (levanta HashCancelled) al chequearse
    entre archivos y entre chunks.
    """
    total_files = len(files)
    sizes = []
    for p in files:
        try:
            sizes.append(os.path.getsize(p))
        except OSError:
            sizes.append(0)
    total_bytes_all = sum(sizes)

    entries: List[FileHashEntry] = []
    total_bytes_done = 0
    start_t = time.time()
    last_report_bytes = 0

    for i, path in enumerate(files, start=1):
        if cancel_check is not None and cancel_check():
            raise HashCancelled("Hasheo cancelado por el usuario")

        file_size = sizes[i - 1]

        def on_chunk(done_in_file, size_in_file, _i=i, _path=path):
            nonlocal total_bytes_done, last_report_bytes
            running_total = total_bytes_done + done_in_file
            if progress_callback is not None and (
                running_total - last_report_bytes >= progress_every_bytes
                or done_in_file >= size_in_file
            ):
                elapsed = time.time() - start_t
                speed = running_total / elapsed if elapsed > 0 else 0.0
                progress_callback(HashProgress(
                    file_index=_i, total_files=total_files, current_path=_path,
                    file_bytes_done=done_in_file, file_bytes_total=size_in_file,
                    total_bytes_done=running_total, total_bytes_all=total_bytes_all,
                    speed_bps=speed, elapsed_seconds=elapsed,
                ))
                last_report_bytes = running_total

        try:
            stat = os.stat(path)
            hashes = hash_file(
                path, algorithms,
                chunk_progress_cb=on_chunk,
                cancel_check=cancel_check,
            )
            entries.append(FileHashEntry(
                path=path,
                size_bytes=stat.st_size,
                modified=datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                hashes=hashes,
            ))
        except HashCancelled:
            raise
        except OSError as e:
            entries.append(FileHashEntry(
                path=path, size_bytes=0, modified='-', hashes={'error': str(e)},
            ))

        total_bytes_done += file_size

    return entries


def write_hash_report(
    entries: Sequence[FileHashEntry],
    output_path: str,
    algorithms: Sequence[str] = DEFAULT_ALGORITHMS,
    source_description: str = "",
) -> str:
    """Escribe un informe de texto plano con el hash de cada archivo.
    Devuelve el path escrito."""
    ok_count = sum(1 for e in entries if 'error' not in e.hashes)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write("INFORME DE VERIFICACION DE INTEGRIDAD (HASH)\n")
        f.write("=" * 78 + "\n")
        f.write(f"Generado       : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        if source_description:
            f.write(f"Origen         : {source_description}\n")
        f.write(f"Archivos       : {len(entries)}  (OK: {ok_count}, errores: {len(entries) - ok_count})\n")
        f.write(f"Algoritmos     : {', '.join(a.upper() for a in algorithms)}\n")
        f.write("=" * 78 + "\n\n")

        for entry in entries:
            f.write(f"Archivo: {entry.path}\n")
            f.write(f"  Tamaño: {entry.size_bytes} bytes\n")
            f.write(f"  Modificado: {entry.modified}\n")
            if 'error' in entry.hashes:
                f.write(f"  ERROR: {entry.hashes['error']}\n")
            else:
                for algo in algorithms:
                    f.write(f"  {algo.upper()}: {entry.hashes.get(algo, '-')}\n")
            f.write("\n")

        f.write("-" * 78 + "\n")
        f.write(f"Fin del informe — {len(entries)} archivo(s) procesado(s)\n")

    return output_path


def reportlab_available() -> bool:
    """Chequeo rápido, sin efectos secundarios, de si ya se puede
    generar el PDF (reportlab importable en este intérprete)."""
    try:
        import reportlab  # noqa: F401
        return True
    except ImportError:
        return False


class ReportlabInstallError(Exception):
    pass


def install_reportlab(log_callback: Optional[Callable[[str], None]] = None) -> None:
    """Instala `reportlab` en caliente vía pip, usando el mismo
    intérprete que corre la app (sys.executable -m pip). Pensado para
    llamarse desde un hilo de fondo (la instalación tarda unos
    segundos y no debe bloquear la UI).

    `log_callback`, si se pasa, recibe líneas de texto de progreso
    (ej. para mostrarlas en un cuadro de estado).

    Levanta ReportlabInstallError con un mensaje legible si la
    instalación falla (sin conexión, pip no disponible, entorno
    de sólo lectura, etc.). No hace nada si reportlab ya está
    instalado.
    """
    if reportlab_available():
        return

    if getattr(sys, 'frozen', False):
        # Ejecutable empaquetado (PyInstaller): sys.executable es el
        # .exe de la app, no un intérprete de Python con pip. No hay
        # forma de instalar un paquete nuevo en caliente acá; hace
        # falta reconstruir el .exe con reportlab ya instalado en el
        # entorno de build (pip install reportlab + volver a generar
        # con SD_READER.spec) o correr la app desde código fuente.
        raise ReportlabInstallError(
            "Esta es la versión empaquetada (.exe) de la app, que no tiene pip "
            "disponible para instalar paquetes nuevos en caliente.\n\n"
            "Para poder generar el PDF hay que volver a compilar el ejecutable "
            "después de instalar reportlab en el entorno de build:\n"
            "    pip install reportlab\n"
            "    pyinstaller SD_READER.spec\n\n"
            "Como alternativa, mientras tanto se puede generar el informe en TXT."
        )

    def notify(msg: str):
        log.info(msg)
        if log_callback:
            log_callback(msg)

    notify("Instalando reportlab (pip install reportlab)…")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "reportlab"],
            capture_output=True, text=True, timeout=180,
        )
    except FileNotFoundError as e:
        raise ReportlabInstallError(
            f"No se encontró el intérprete de Python para instalar el paquete "
            f"({sys.executable}). Instalá reportlab manualmente: pip install reportlab"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise ReportlabInstallError(
            "La instalación tardó demasiado (¿sin conexión a internet?). "
            "Probá instalarlo manualmente: pip install reportlab"
        ) from e

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-800:]
        raise ReportlabInstallError(
            "pip no pudo instalar reportlab. Detalle:\n" + (detail or "(sin salida)")
        )

    # invalidar caches de import para que este mismo proceso vea el
    # paquete recién instalado sin necesidad de reiniciar la app.
    import importlib
    importlib.invalidate_caches()
    if not reportlab_available():
        raise ReportlabInstallError(
            "pip terminó sin error pero el paquete sigue sin poder importarse. "
            "Puede que haga falta reiniciar la app."
        )
    notify("reportlab instalado correctamente.")


def write_hash_report_pdf(
    entries: Sequence[FileHashEntry],
    output_path: str,
    algorithms: Sequence[str] = DEFAULT_ALGORITHMS,
    source_description: str = "",
) -> str:
    """Genera el mismo informe en PDF, con una sección final de 'cadena
    de custodia' que incluye el hash SHA-256 del propio PDF (calculado
    en una segunda pasada, una vez cerrado el documento).

    Requiere `reportlab` (dependencia opcional — ver requirements.txt).
    Devuelve el path escrito.
    """
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
        )
    except ImportError as e:
        raise ImportError(
            "Para generar el informe en PDF hace falta instalar reportlab:\n"
            "    pip install reportlab"
        ) from e

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TitleCustom", parent=styles["Title"], fontSize=16, spaceAfter=2,
        textColor=colors.HexColor("#12233d"))
    subtitle_style = ParagraphStyle(
        "SubtitleCustom", parent=styles["Normal"], fontSize=9.5,
        textColor=colors.HexColor("#5a6472"), spaceAfter=14)
    h2 = ParagraphStyle(
        "H2Custom", parent=styles["Heading2"], fontSize=11, spaceBefore=14, spaceAfter=6,
        textColor=colors.HexColor("#12233d"))
    label = ParagraphStyle("Label", parent=styles["Normal"], fontSize=8.5,
                            textColor=colors.HexColor("#5a6472"))
    value = ParagraphStyle("Value", parent=styles["Normal"], fontSize=9.5,
                            textColor=colors.HexColor("#1a1a1a"))
    footer_style = ParagraphStyle("Footer", parent=styles["Normal"], fontSize=7.5,
                                   textColor=colors.HexColor("#8a8f96"))

    def build(story_extra_custody=None):
        story = []
        story.append(Paragraph("Informe de Verificación de Integridad (Hash)", title_style))
        story.append(Paragraph("Generado por sd-hik-reader · módulo Hashear", subtitle_style))
        story.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#12233d")))
        story.append(Spacer(1, 10))

        ok_count = sum(1 for e in entries if 'error' not in e.hashes)
        total_size = sum(e.size_bytes for e in entries)
        meta_data = [
            [Paragraph("Fecha y hora", label),
             Paragraph(datetime.now().strftime('%d/%m/%Y %H:%M:%S'), value),
             Paragraph("Algoritmos", label),
             Paragraph(", ".join(a.upper() for a in algorithms), value)],
            [Paragraph("Carpeta procesada", label),
             Paragraph(source_description or "-", value),
             Paragraph("Archivos", label),
             Paragraph(f"{len(entries)} (OK: {ok_count})", value)],
            [Paragraph("Tamaño total", label),
             Paragraph(f"{total_size / (1024*1024):.1f} MB", value),
             Paragraph("", label), Paragraph("", value)],
        ]
        meta_table = Table(meta_data, colWidths=[70, 175, 60, 130])
        meta_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(meta_table)
        story.append(Spacer(1, 4))

        story.append(Paragraph("Detalle por archivo", h2))

        # Anchos de columna que siempre entran en el ancho útil de la
        # página (A4 menos márgenes), sin importar cuántos algoritmos
        # se eligieron — evita que la tabla se desborde y el texto de
        # los hashes se superponga entre columnas.
        page_width, _ = A4
        usable_width = page_width - 2 * (18 * mm)
        fname_w, size_w = 120, 55
        n_algos = max(len(algorithms), 1)
        hash_w = max(45, (usable_width - fname_w - size_w) / n_algos)
        col_widths = [fname_w, size_w] + [hash_w] * n_algos

        # Fuente monoespaciada chica + wordWrap='CJK': permite partir
        # el hash en cualquier punto (no tiene espacios) para que
        # entre en la columna en vez de desbordarse.
        hash_font_size = 7 if n_algos <= 2 else (6.3 if n_algos <= 4 else 5.6)
        hash_style = ParagraphStyle(
            "HashCell", parent=styles["Normal"], fontName="Courier",
            fontSize=hash_font_size, leading=hash_font_size + 1.6,
            textColor=colors.HexColor("#1a1a1a"), wordWrap='CJK')
        fname_style = ParagraphStyle(
            "FnameCell", parent=styles["Normal"], fontSize=7,
            leading=8.5, textColor=colors.HexColor("#1a1a1a"), wordWrap='CJK')
        error_style = ParagraphStyle(
            "ErrorCell", parent=styles["Normal"], fontSize=6.8,
            leading=8.2, textColor=colors.HexColor("#c42b1c"), wordWrap='CJK')

        col_labels = ["Archivo", "Tamaño"] + [a.upper() for a in algorithms]
        table_data = [col_labels]
        for e in entries:
            row = [Paragraph(os.path.basename(e.path), fname_style),
                   Paragraph(f"{e.size_bytes:,} B", fname_style)]
            if 'error' in e.hashes:
                row += [Paragraph("ERROR: " + e.hashes['error'], error_style)] + [""] * (n_algos - 1)
            else:
                row += [Paragraph(e.hashes.get(a, '-'), hash_style) for a in algorithms]
            table_data.append(row)

        t = Table(table_data, colWidths=col_widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#12233d")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 8),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f4f7")]),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c9ced4")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(t)
        story.append(Spacer(1, 14))

        story.append(HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#c9ced4")))
        story.append(Spacer(1, 6))
        story.append(Paragraph("Cadena de custodia", h2))
        if story_extra_custody:
            story.append(Paragraph(story_extra_custody, value))
        else:
            story.append(Paragraph(
                "El hash SHA-256 de este PDF se calcula en una segunda pasada, "
                "una vez generado el documento, y se agrega en la versión final.",
                footer_style))
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            "Este informe certifica el estado de los archivos al momento indicado "
            "arriba. Cualquier modificación posterior de los archivos originales "
            "invalidará la correspondencia con los valores aquí registrados.",
            footer_style))
        return story

    # 1era pasada: generar el PDF sin el hash de sí mismo.
    doc = SimpleDocTemplate(
        output_path, pagesize=A4,
        topMargin=20 * mm, bottomMargin=18 * mm, leftMargin=18 * mm, rightMargin=18 * mm,
        title="Informe de verificacion de hash")
    doc.build(build())

    # 2da pasada: hashear el PDF recién escrito e incrustar ese hash.
    with open(output_path, 'rb') as f:
        self_hash = hashlib.sha256(f.read()).hexdigest()
    custody_text = f"Hash SHA-256 del presente informe: <font name='Courier' size='7.5'>{self_hash}</font>"

    doc2 = SimpleDocTemplate(
        output_path, pagesize=A4,
        topMargin=20 * mm, bottomMargin=18 * mm, leftMargin=18 * mm, rightMargin=18 * mm,
        title="Informe de verificacion de hash")
    doc2.build(build(story_extra_custody=custody_text))

    return output_path
