#!/usr/bin/env python3
"""
core/parser.py

Parser de índices EZVIZ/Hikvision (index00.bin / index00p.bin) y extractor
de clips vía ffmpeg. Lógica portada 1:1 desde la versión original del
script, sólo se movió a un módulo separado.

FIX 1: los archivos hivXXXXX.mp4 en cámaras HEVC (como la EB3 4G)
no son un stream elemental Annex-B puro: son contenedores MPEG-PS
(Program Stream) que mezclan video HEVC + audio AAC, con headers de
pack/PES intercalados entre los datos de video. Forzar el demuxer de
stream crudo (-f h264 / -f hevc) hacía que esos headers se leyeran como
si fueran datos NAL, desincronizando el CABAC a medida que avanzaba el
clip -> video "cortado"/con glitches. Ahora se deja que ffmpeg
autodetecte el contenedor real (y se agregan fallbacks explícitos).

FIX 2: un mismo segmento del índice puede abarcar VARIOS contenedores
MPEG-PS concatenados (cada uno con su propio system_header 00 00 01 BB),
no uno solo. ffmpeg autodetecta y demuxea bien el primero, pero al
llegar al inicio del segundo se corta ahí -> clips que dicen durar
minutos y en la práctica quedan de 1-2 segundos. Ahora se valida la
duración del resultado contra la duración esperada del segmento y, si
quedó corto, se parte el volcado crudo por cada system_header, se
remuxea cada trozo a .ts y se concatenan.
"""

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from struct import unpack, calcsize
from typing import Optional

log = logging.getLogger("ezviz_reader.parser")

# En Windows, subprocess.run() sin este flag abre una ventana de
# consola visible por cada llamada a ffmpeg/ffprobe (se nota sobre
# todo en la vista previa, que dispara varias seguidas). En otros
# sistemas operativos no aplica (flag = 0, sin efecto).
_SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0


class EZVIZParseError(Exception):
    """Error al parsear los archivos de índice de la SD."""


@dataclass
class Segment:
    """Representa un clip de grabación."""
    type: bytes
    status: bytes
    resolution: bytes
    startTime: int
    endTime: int
    startOffset: int
    endOffset: int
    fileNum: int
    indexFileNum: int
    filePath: str
    startTimeDt: datetime = field(init=False)
    endTimeDt: datetime = field(init=False)

    def __post_init__(self):
        mask = 0xFFFFFFFF
        self.startTimeDt = datetime.fromtimestamp(self.startTime & mask, tz=timezone.utc)
        self.endTimeDt = datetime.fromtimestamp(self.endTime & mask, tz=timezone.utc)

    @property
    def duration(self) -> float:
        return (self.endTimeDt - self.startTimeDt).total_seconds()


class EZVIZIndexParser:
    """Parser de archivos index00.bin / index00p.bin de EZVIZ/Hikvision."""

    HEADER_LEN = 1280
    FILE_LEN = 32
    SEGMENT_LEN = 80
    MAX_SEGMENTS = 256
    READ_CHUNK = 4096

    NASINFO_LEN = 68
    NASINFO_FMT = "48s4sB3I"
    HEADER_FMT = "Q4I1176s76sI"
    SEGMENT_FMT = "ss2s4s3Q4I4s4s8s4s4s4s4s"

    HEADER_KEYS = [
        'modifyTimes', 'version', 'avFiles', 'nextFileRecNo',
        'lastFileRecNo', 'curFileRec', 'unknown', 'checksum',
    ]
    SEGMENT_KEYS = [
        'type', 'status', 'resA', 'resolution', 'startTime', 'endTime',
        'firstKeyFrame_absTime', 'firstKeyFrame_stdTime', 'lastFrame_stdTime',
        'startOffset', 'endOffset', 'resB', 'infoNum', 'infoTypes',
        'infoStartTime', 'infoEndTime', 'infoStartOffset', 'infoEndOffset',
    ]

    VALID_VIDEO_TYPES = ('video', 'mp4')
    VALID_IMAGE_TYPES = ('image', 'img', 'pic')

    def __init__(self, cameradir: str, asktype: str = 'video'):
        if asktype not in (*self.VALID_VIDEO_TYPES, *self.VALID_IMAGE_TYPES):
            raise ValueError(
                f"asktype inválido: {asktype!r}. "
                f"Use uno de {self.VALID_VIDEO_TYPES + self.VALID_IMAGE_TYPES}"
            )

        self._sanity_check_struct_sizes()

        self.cameradir = cameradir
        self.asktype = asktype
        self.index_filename = (
            'index00.bin' if asktype in self.VALID_VIDEO_TYPES else 'index00p.bin'
        )

        self.info = self._get_nas_info()
        self.header = self._get_file_header()

    def _sanity_check_struct_sizes(self):
        checks = [
            (self.NASINFO_FMT, self.NASINFO_LEN, "info.bin"),
            (self.HEADER_FMT, self.HEADER_LEN, "header"),
            (self.SEGMENT_FMT, self.SEGMENT_LEN, "segment"),
        ]
        for fmt, expected, label in checks:
            actual = calcsize(fmt)
            if actual != expected:
                raise EZVIZParseError(
                    f"Tamaño de struct incorrecto para {label}: "
                    f"esperado {expected} bytes, formato produce {actual} bytes"
                )

    def _index_path(self, index_dir_num: int) -> str:
        candidate = f"{self.cameradir}/datadir{index_dir_num}/{self.index_filename}"
        if os.path.exists(candidate):
            return candidate
        candidate = f"{self.cameradir}/{self.index_filename}"
        if os.path.exists(candidate):
            return candidate
        raise EZVIZParseError(f"No se encontró {self.index_filename} en {self.cameradir}")

    def _log_directory_listing(self):
        try:
            entries = sorted(os.listdir(self.cameradir))
        except OSError as e:
            log.warning("No se pudo listar %s: %s", self.cameradir, e)
            return
        log.info("Contenido de %s: %s", self.cameradir, entries)
        for entry in entries:
            sub = os.path.join(self.cameradir, entry)
            if os.path.isdir(sub) and entry.lower().startswith('datadir'):
                try:
                    log.info("  %s/: %s", entry, sorted(os.listdir(sub)))
                except OSError:
                    pass

    def _get_nas_info(self) -> dict:
        self._log_directory_listing()
        file_name = f"{self.cameradir}/info.bin"
        if not os.path.exists(file_name):
            log.warning("info.bin no encontrado en %s, asumiendo 1 datadir", self.cameradir)
            return {
                'serialNumber': b'UNKNOWN', 'MACAddr': b'\x00\x00\x00\x00\x00\x00',
                'byRes': 0, 'f_bsize': 0, 'f_blocks': 0, 'DataDirs': 1,
            }
        try:
            with open(file_name, 'rb') as f:
                raw = f.read(self.NASINFO_LEN)
            if len(raw) < self.NASINFO_LEN:
                raise EZVIZParseError("info.bin truncado")
            keys = ['serialNumber', 'MACAddr', 'byRes', 'f_bsize', 'f_blocks', 'DataDirs']
            info = dict(zip(keys, unpack(self.NASINFO_FMT, raw)))
            log.info("info.bin OK: DataDirs=%s", info.get('DataDirs'))
            return info
        except Exception as e:
            log.warning("No se pudo leer info.bin (%s), asumiendo 1 datadir", e)
            return {
                'serialNumber': b'UNKNOWN', 'MACAddr': b'\x00\x00\x00\x00\x00\x00',
                'byRes': 0, 'f_bsize': 0, 'f_blocks': 0, 'DataDirs': 1,
            }

    def _get_file_header(self) -> dict:
        last_error: Optional[Exception] = None
        for index_dir_num in range(self.info.get('DataDirs', 1)):
            try:
                file_name = self._index_path(index_dir_num)
                log.info("Leyendo header desde %s", file_name)
                with open(file_name, 'rb') as f:
                    raw = f.read(self.HEADER_LEN)
                if len(raw) < self.HEADER_LEN:
                    raise EZVIZParseError(
                        f"{file_name} truncado: se esperaban {self.HEADER_LEN} bytes, "
                        f"se leyeron {len(raw)}"
                    )
                header = dict(zip(self.HEADER_KEYS, unpack(self.HEADER_FMT, raw)))
                log.info("Header OK en datadir%s: avFiles=%s", index_dir_num, header.get('avFiles'))
                return header
            except Exception as e:
                log.warning("No se pudo leer header en datadir%s: %s", index_dir_num, e)
                last_error = e
                continue
        if last_error:
            log.warning("No se pudo leer ningún header de índice válido: %s", last_error)
        return {'avFiles': 0}

    def get_segments(
        self,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
    ) -> list:
        segments: list[Segment] = []
        offset = self.HEADER_LEN + self.header.get('avFiles', 0) * self.FILE_LEN
        avFiles = self.header.get('avFiles', 0)
        num_datadirs = self.info.get('DataDirs', 1)

        log.info("get_segments: DataDirs=%s avFiles=%s offset=%s", num_datadirs, avFiles, offset)

        if avFiles == 0:
            log.warning(
                "avFiles=0: no se leyó ningún header de índice válido. "
                "Revisá que la carpeta seleccionada sea la raíz correcta de la SD "
                "(debe contener info.bin y/o datadirX/index00.bin)."
            )

        for index_dir_num in range(num_datadirs):
            try:
                file_name = self._index_path(index_dir_num)
            except EZVIZParseError as e:
                log.warning(str(e))
                continue

            datadir_path = f"{self.cameradir}/datadir{index_dir_num}"
            if not os.path.exists(datadir_path):
                datadir_path = self.cameradir
            ext = 'mp4' if self.index_filename == 'index00.bin' else 'pic'

            raw_read = 0
            valid = 0
            skipped_errors = 0

            try:
                with open(file_name, 'rb') as f:
                    f.seek(offset)

                    for file_num in range(avFiles):
                        for _ in range(self.MAX_SEGMENTS):
                            raw = f.read(self.SEGMENT_LEN)
                            if len(raw) < self.SEGMENT_LEN:
                                break
                            raw_read += 1

                            try:
                                values = dict(zip(self.SEGMENT_KEYS, unpack(self.SEGMENT_FMT, raw)))
                                if values['endTime'] == 0:
                                    continue

                                seg = Segment(
                                    type=values['type'],
                                    status=values['status'],
                                    resolution=values['resolution'],
                                    startTime=values['startTime'],
                                    endTime=values['endTime'],
                                    startOffset=values['startOffset'],
                                    endOffset=values['endOffset'],
                                    fileNum=file_num,
                                    indexFileNum=index_dir_num,
                                    filePath=f"{datadir_path}/hiv{file_num:05d}.{ext}",
                                )
                            except (ValueError, OverflowError, OSError) as e:
                                skipped_errors += 1
                                log.debug(
                                    "Registro inválido en datadir%s fileNum=%s: %s",
                                    index_dir_num, file_num, e
                                )
                                continue

                            valid += 1
                            if self._passes_filter(seg, from_time, to_time):
                                segments.append(seg)
            except OSError as e:
                log.warning("Error leyendo datadir%s: %s", index_dir_num, e)
                continue

            log.info(
                "datadir%s (%s): %s registros leídos, %s válidos, %s con error",
                index_dir_num, file_name, raw_read, valid, skipped_errors,
            )

        log.info("get_segments: total segmentos encontrados = %s", len(segments))
        segments.sort(key=lambda s: s.startTimeDt)
        return segments

    @staticmethod
    def _passes_filter(seg: Segment, from_time, to_time) -> bool:
        if from_time is not None and seg.startTimeDt.replace(tzinfo=None) <= from_time:
            return False
        if to_time is not None and seg.startTimeDt.replace(tzinfo=None) >= to_time:
            return False
        return True

    # ----------------------------------------------------------------
    # Mapa de segmentos (para la ventana "Ver > Mapa de segmentos")
    # ----------------------------------------------------------------
    def get_segment_map(self) -> dict:
        """Recorre el índice slot por slot (igual que get_segments) pero sin
        descartar nada: devuelve, por cada datadir y cada avFile, el estado
        de sus MAX_SEGMENTS=256 slots.

        Estado de cada slot:
          'free'    endTime == 0 (posición nunca escrita / liberada)
          'video'   registro válido, status == 0
          'alarm'   registro válido, status != 0
          'corrupt' el struct.unpack falló (registro ilegible)

        NOTA sobre 'alarm': es una heurística basada en el byte 'status'
        del registro (no hay documentación oficial de este formato que
        confirme su semántica exacta en todas las variantes de firmware
        EZVIZ/Hikvision). Tratalo como una pista visual, no como un hecho
        garantizado.

        Devuelve: {index_dir_num: [ {file_num, slots, used, first_free}, ... ]}
        donde 'slots' es una lista de 256 dicts {'state': str, 'segment': Segment|None}.
        """
        avFiles = self.header.get('avFiles', 0)
        num_datadirs = self.info.get('DataDirs', 1)
        offset = self.HEADER_LEN + avFiles * self.FILE_LEN
        ext = 'mp4' if self.index_filename == 'index00.bin' else 'pic'

        result: dict = {}

        for index_dir_num in range(num_datadirs):
            try:
                file_name = self._index_path(index_dir_num)
            except EZVIZParseError as e:
                log.warning(str(e))
                continue

            datadir_path = f"{self.cameradir}/datadir{index_dir_num}"
            if not os.path.exists(datadir_path):
                datadir_path = self.cameradir

            files_out = []
            eof = False
            try:
                with open(file_name, 'rb') as f:
                    f.seek(offset)
                    for file_num in range(avFiles):
                        slots = []
                        for _ in range(self.MAX_SEGMENTS):
                            if eof:
                                slots.append({'state': 'free', 'segment': None})
                                continue
                            raw = f.read(self.SEGMENT_LEN)
                            if len(raw) < self.SEGMENT_LEN:
                                eof = True
                                slots.append({'state': 'free', 'segment': None})
                                continue
                            try:
                                values = dict(zip(self.SEGMENT_KEYS, unpack(self.SEGMENT_FMT, raw)))
                                if values['endTime'] == 0:
                                    slots.append({'state': 'free', 'segment': None})
                                    continue
                                seg = Segment(
                                    type=values['type'],
                                    status=values['status'],
                                    resolution=values['resolution'],
                                    startTime=values['startTime'],
                                    endTime=values['endTime'],
                                    startOffset=values['startOffset'],
                                    endOffset=values['endOffset'],
                                    fileNum=file_num,
                                    indexFileNum=index_dir_num,
                                    filePath=f"{datadir_path}/hiv{file_num:05d}.{ext}",
                                )
                                state = 'alarm' if values['status'] not in (b'\x00', 0) else 'video'
                                slots.append({'state': state, 'segment': seg})
                            except (ValueError, OverflowError, OSError) as e:
                                log.debug(
                                    "Slot ilegible en datadir%s fileNum=%s: %s",
                                    index_dir_num, file_num, e
                                )
                                slots.append({'state': 'corrupt', 'segment': None})

                        used = sum(1 for s in slots if s['state'] != 'free')
                        first_free = next(
                            (i for i, s in enumerate(slots) if s['state'] == 'free'), None)
                        files_out.append({
                            'file_num': file_num,
                            'index_dir_num': index_dir_num,
                            'slots': slots,
                            'used': used,
                            'first_free': first_free,
                        })
            except OSError as e:
                log.warning("Error leyendo datadir%s para mapa de segmentos: %s", index_dir_num, e)
                continue

            result[index_dir_num] = files_out

        return result

    def extract_segment_mp4(self, segment: Segment, output_path: str, cache_path: str = '/tmp') -> str:
        return self._extract_and_transcode(
            segment, output_path, cache_path,
            extra_args=['-c:v', 'copy', '-an'],
            expected_duration=segment.duration,
        )

    def extract_segment_mp4_with_audio(self, segment: Segment, output_path: str, cache_path: str = '/tmp') -> str:
        return self._extract_and_transcode(
            segment, output_path, cache_path,
            extra_args=['-c:v', 'copy', '-c:a', 'copy'],
            expected_duration=segment.duration,
        )

    def prepare_preview(self, segment: Segment, cache_dir: Optional[str] = None) -> str:
        cache_dir = cache_dir or os.path.join(tempfile.gettempdir(), 'ezviz_preview_cache')
        os.makedirs(cache_dir, exist_ok=True)
        preview_path = os.path.join(
            cache_dir,
            f"preview_{segment.indexFileNum}_{segment.fileNum}_"
            f"{segment.startOffset}_{segment.endOffset}.mp4"
        )
        if os.path.exists(preview_path) and os.path.getsize(preview_path) > 0:
            log.info("Vista previa en caché: %s", preview_path)
            return preview_path
        return self.extract_segment_mp4(segment, preview_path, cache_path=cache_dir)

    def extract_segment_jpg(self, segment: Segment, output_path: str, cache_path: str = '/tmp',
                             position: Optional[float] = None) -> str:
        if position is None:
            position = min(segment.duration / 2, 59)
        pos_str = f"00:00:{int(position):02d}"
        return self._extract_and_transcode(
            segment, output_path, cache_path,
            extra_args=['-ss', pos_str, '-vframes', '1'],
        )

    PS_SYSTEM_HEADER = b'\x00\x00\x01\xbb'
    PS_MIN_CHUNK_BYTES = 200_000

    @staticmethod
    def _probe_duration(path: str) -> Optional[float]:
        if shutil.which('ffprobe') is None:
            return None
        try:
            result = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', path],
                capture_output=True, text=True, timeout=15,
                creationflags=_SUBPROCESS_FLAGS,
            )
            if result.returncode == 0 and result.stdout.strip():
                return float(result.stdout.strip())
        except (ValueError, OSError, subprocess.SubprocessError):
            pass
        return None

    def _split_ps_containers(self, raw_file: str) -> list:
        with open(raw_file, 'rb') as f:
            data = f.read()

        starts = [m.start() for m in re.finditer(re.escape(self.PS_SYSTEM_HEADER), data)]
        filtered = []
        for s in starts:
            if not filtered or s - filtered[-1] > self.PS_MIN_CHUNK_BYTES:
                filtered.append(s)

        boundaries = sorted(set([0, *filtered, len(data)]))
        return [(boundaries[i], boundaries[i + 1]) for i in range(len(boundaries) - 1)
                if boundaries[i + 1] > boundaries[i]]

    def _extract_multi_chunk(self, raw_file: str, chunks: list, output_path: str,
                              cache_path: str, extra_args: list) -> Optional[str]:
        ts_files = []
        try:
            for i, (s, e) in enumerate(chunks):
                if e - s < 512:
                    continue
                chunk_raw = os.path.join(cache_path, f"_chunk_{os.getpid()}_{i}.raw")
                with open(raw_file, 'rb') as src, open(chunk_raw, 'wb') as dst:
                    src.seek(s)
                    dst.write(src.read(e - s))

                ts_out = os.path.join(cache_path, f"_chunk_{os.getpid()}_{i}.ts")
                cmd = ['ffmpeg', '-y', '-fflags', '+genpts', '-i', chunk_raw,
                       '-c', 'copy', '-f', 'mpegts',
                       '-hide_banner', '-loglevel', 'error', ts_out]
                result = subprocess.run(cmd, capture_output=True, text=True,
                                         creationflags=_SUBPROCESS_FLAGS)
                os.remove(chunk_raw)
                if result.returncode == 0 and os.path.exists(ts_out) and os.path.getsize(ts_out) > 0:
                    ts_files.append(ts_out)

            if not ts_files:
                return None

            concat_input = 'concat:' + '|'.join(ts_files)
            cmd = ['ffmpeg', '-y', '-i', concat_input, *extra_args,
                   '-fflags', '+genpts', '-avoid_negative_ts', 'make_zero',
                   '-hide_banner', '-loglevel', 'error', output_path]
            result = subprocess.run(cmd, capture_output=True, text=True,
                                     creationflags=_SUBPROCESS_FLAGS)
            if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                return output_path
            return None
        finally:
            for f in ts_files:
                if os.path.exists(f):
                    os.remove(f)

    def _extract_and_transcode(self, segment: Segment, output_path: str, cache_path: str,
                                extra_args: list, expected_duration: Optional[float] = None) -> str:
        if not os.path.exists(segment.filePath):
            raise FileNotFoundError(f"No existe el archivo de video: {segment.filePath}")
        if shutil.which('ffmpeg') is None:
            raise RuntimeError("ffmpeg no está instalado o no está en el PATH")

        os.makedirs(cache_path, exist_ok=True)
        raw_file = os.path.join(
            cache_path,
            f"hik_{segment.indexFileNum}_{segment.startOffset}_{segment.endOffset}.raw"
        )
        best_backup = output_path + '.best'

        try:
            self._dump_raw_bytes(segment, raw_file)
            last_err = None
            best_duration = -1.0
            have_best = False

            attempts = [
                [],
                ['-f', 'mpeg'],
                ['-f', 'hevc'],
                ['-f', 'h264'],
            ]
            robust_flags = ['-fflags', '+genpts', '-avoid_negative_ts', 'make_zero']

            for fmt_args in attempts:
                cmd = ['ffmpeg', '-y', *fmt_args, *robust_flags, '-i', raw_file, *extra_args,
                       '-hide_banner', '-loglevel', 'error', output_path]
                result = subprocess.run(cmd, capture_output=True, text=True,
                                         creationflags=_SUBPROCESS_FLAGS)
                if not (result.returncode == 0 and os.path.exists(output_path)
                        and os.path.getsize(output_path) > 0):
                    last_err = result.stderr
                    continue

                if expected_duration is None:
                    return output_path

                got = self._probe_duration(output_path)
                log.info("Extracción (%s) -> %.2fs de %.2fs esperados",
                          ' '.join(fmt_args) or 'auto', got or -1, expected_duration)
                if got is not None and got >= expected_duration * 0.85 - 1.0:
                    return output_path
                if got is not None and got > best_duration:
                    best_duration = got
                    shutil.copyfile(output_path, best_backup)
                    have_best = True

            if expected_duration is not None:
                chunks = self._split_ps_containers(raw_file)
                if len(chunks) > 1:
                    log.info("Detectados %s sub-contenedores MPEG-PS en el segmento, "
                              "remuxando y concatenando...", len(chunks))
                    multi_result = self._extract_multi_chunk(
                        raw_file, chunks, output_path, cache_path, extra_args)
                    if multi_result:
                        got = self._probe_duration(multi_result)
                        log.info("Extracción multi-contenedor -> %.2fs de %.2fs esperados",
                                  got or -1, expected_duration)
                        if got is not None and got >= expected_duration * 0.85 - 1.0:
                            return multi_result
                        if got is not None and got > best_duration:
                            best_duration = got
                            shutil.copyfile(multi_result, best_backup)
                            have_best = True

            if have_best:
                shutil.move(best_backup, output_path)
                log.warning(
                    "No se pudo recuperar la duración completa del clip "
                    "(%.2fs de %.2fs esperados); se guardó el mejor resultado parcial.",
                    best_duration, expected_duration
                )
                return output_path

            raise RuntimeError(f"ffmpeg no pudo procesar el clip: {last_err}")
        finally:
            if os.path.exists(raw_file):
                os.remove(raw_file)
            if os.path.exists(best_backup):
                os.remove(best_backup)

    def _dump_raw_bytes(self, segment: Segment, raw_file: str):
        with open(segment.filePath, 'rb') as video_in, open(raw_file, 'wb') as video_out:
            video_in.seek(segment.startOffset)
            remaining = segment.endOffset - segment.startOffset
            while remaining > 0:
                chunk = video_in.read(min(self.READ_CHUNK, remaining))
                if not chunk:
                    break
                video_out.write(chunk)
                remaining -= len(chunk)
