#!/usr/bin/env python3
"""
core/sd_check.py

Chequeo previo ("preflight") de una tarjeta SD ya indexada:

1. Archivos faltantes: el índice referencia archivos de video
   (hivXXXXX.mp4) que pueden no existir físicamente en la SD (tarjeta
   parcialmente corrompida, sectores reescritos, extracción previa
   incompleta, etc.).
2. Consistencia índice/contenedor: cada segmento dice ocupar un rango
   de bytes [startOffset, endOffset) dentro de su archivo. Si ese
   rango no entra en el tamaño real del archivo en disco, el índice
   quedó desincronizado respecto al contenido real (por ejemplo, el
   archivo fue truncado o sobrescrito parcialmente). Es un chequeo
   barato que no necesita ffmpeg ni decodificar nada.

Esto NUNCA bloquea: sólo informa. Los segmentos con problemas se
excluyen del resumen "ok", pero siguen apareciendo en la lista de
clips — el usuario puede intentar descargar igual lo que sirva.
"""

import os
from dataclasses import dataclass, field
from typing import List, Sequence


@dataclass
class SegmentIssue:
    segment_index: int
    file_path: str
    reason: str  # 'missing_file' | 'offset_out_of_range'


@dataclass
class PreflightReport:
    total_segments: int
    missing_files: List[str] = field(default_factory=list)
    segments_missing_file: int = 0
    segments_offset_mismatch: int = 0
    issues: List[SegmentIssue] = field(default_factory=list)

    @property
    def ok_segments(self) -> int:
        return self.total_segments - self.segments_missing_file - self.segments_offset_mismatch

    @property
    def has_issues(self) -> bool:
        return bool(self.missing_files) or self.segments_offset_mismatch > 0


def run_preflight(segments: Sequence) -> PreflightReport:
    """Recorre `segments` (lista de core.parser.Segment) validando
    existencia de archivo y consistencia de offsets. No abre ni
    decodifica video, sólo os.path.getsize — es rápido incluso con
    miles de segmentos."""
    report = PreflightReport(total_segments=len(segments))
    file_sizes = {}  # filePath -> tamaño real en disco, o None si no existe

    for i, seg in enumerate(segments):
        path = seg.filePath
        if path not in file_sizes:
            try:
                file_sizes[path] = os.path.getsize(path)
            except OSError:
                file_sizes[path] = None

        size = file_sizes[path]
        if size is None:
            report.segments_missing_file += 1
            report.issues.append(SegmentIssue(i, path, 'missing_file'))
            continue

        if seg.endOffset > size or seg.startOffset >= seg.endOffset:
            report.segments_offset_mismatch += 1
            report.issues.append(SegmentIssue(i, path, 'offset_out_of_range'))

    report.missing_files = sorted(p for p, s in file_sizes.items() if s is None)
    return report
