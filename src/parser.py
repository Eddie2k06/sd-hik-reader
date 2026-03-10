"""
sd-hik-reader · parser.py
Toda la lógica de parseo binario: OFNI, RATS, HIV/MPEG-PS.

Investigación original sobre CS-EB3-R200 / Firmware V5.4.0 build 250520
Ver HIK_SD_Format.md para documentación completa del formato.
"""

import struct
import os
import re
from datetime import datetime, timezone

# ── Constantes ────────────────────────────────────────────────────────────────

BLOCK     = 268_435_456      # 256 MB — tamaño de cada archivo HIV
TS_MIN    = 1_577_836_800    # 2020-01-01 — validación de timestamps
TS_MAX    = 1_956_528_000    # 2032-01-01

# Flags de tipo de clip en la entrada OFNI
# El bit 0x00800000 diferencia índice activo (p.bin) de backup
FLAGS_VID = {0x0020000D, 0x00A0000D}
FLAGS_PIC = {0x00200020, 0x00A00020}

# Tamaño fijo de cada entrada OFNI (bytes)
OFNI_SIZE = 80

# Códigos de evento RATS
RATS_EVENTS = {
    0x00410003: "Movimiento + Video",
    0x00410001: "Movimiento",
    0x00000002: "Sistema",
}

# ── Helpers de formato ────────────────────────────────────────────────────────

def fmt_dt(ts: int | None) -> str:
    """
    Convierte Unix timestamp a fecha/hora legible.
    IMPORTANTE: El firmware CS-EB3 graba la hora local directamente como UTC.
    No aplicar conversión de zona horaria — usar timezone.utc puro.
    """
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d-%m-%Y %H:%M:%S")
    except (OSError, OverflowError):
        return f"ts={ts}"


def fmt_sz(b: int) -> str:
    if b <= 0:    return "—"
    if b < 1024:  return f"{b} B"
    if b < 1<<20: return f"{b/1024:.1f} KB"
    if b < 1<<30: return f"{b/(1<<20):.2f} MB"
    return f"{b/(1<<30):.2f} GB"


def fmt_dur(s: float) -> str:
    if s <= 0:    return "—"
    if s < 1:     return f"{s:.2f}s"
    if s < 60:    return f"{s:.1f}s"
    if s < 3600:  return f"{int(s)//60}m {int(s)%60:02d}s"
    return f"{int(s)//3600}h {(int(s)%3600)//60:02d}m"


# ── OFNI — índice de clips ────────────────────────────────────────────────────

def _parse_ofni_header(data: bytes, start: int) -> dict:
    """Extrae timestamps del header antes del primer OFNI."""
    header_ts = []
    for off in range(0, min(start, 256), 4):
        if off + 4 > len(data):
            break
        v = struct.unpack_from("<I", data, off)[0]
        if TS_MIN < v < TS_MAX:
            header_ts.append({"offset": off, "ts": v, "dt": fmt_dt(v)})
    return {"header_offset": start, "header_ts": header_ts}


def parse_ofni_entry(ch: bytes, slot_idx: int) -> dict | None:
    """
    Parsea una entrada OFNI de 80 bytes.
    Retorna dict con todos los campos o None si no es válida.
    """
    if len(ch) < OFNI_SIZE or ch[:4] != b"OFNI":
        return None

    # Slot vacío
    state = struct.unpack_from("<I", ch, 12)[0]
    if state == 0x7FFFFFFF:
        return None  # slot vacío — saltar

    flags        = struct.unpack_from("<I", ch, 28)[0]
    ts_s         = struct.unpack_from("<I", ch, 36)[0]
    us_s         = struct.unpack_from("<I", ch, 40)[0]
    ts_e         = struct.unpack_from("<I", ch, 44)[0]
    us_e         = struct.unpack_from("<I", ch, 48)[0]
    stream_bytes = struct.unpack_from("<I", ch, 60)[0]
    block_bytes  = struct.unpack_from("<I", ch, 64)[0]
    gl_s         = struct.unpack_from("<I", ch, 68)[0]
    gl_e         = struct.unpack_from("<I", ch, 72)[0]

    if not (TS_MIN < ts_s < TS_MAX):
        return None

    if   flags in FLAGS_VID: tipo = "Video"
    elif flags in FLAGS_PIC: tipo = "Foto"
    else:                    tipo = f"0x{flags:08X}"

    hiv_idx     = gl_s // BLOCK
    off_in_hiv  = gl_s % BLOCK
    size        = max(0, gl_e - gl_s)
    dur_s       = max(0, ts_e - ts_s)   # siempre 0 en index*p.bin
    cross_block = gl_e > (hiv_idx + 1) * BLOCK

    return {
        "slot":         slot_idx,
        "tipo":         tipo,
        "flags":        f"0x{flags:08X}",
        "state":        f"0x{state:08X}",
        "ts_s":         ts_s,
        "ts_e":         ts_e,
        "us_s":         us_s,
        "us_e":         us_e,
        "dur_s":        dur_s,
        "dur_scr":      0.667,      # duración real estimada desde SCR (~0.667s en CS-EB3)
        "stream_bytes": stream_bytes,
        "block_bytes":  block_bytes,
        "gl_s":         gl_s,
        "gl_e":         gl_e,
        "size_b":       size,
        "hiv_idx":      hiv_idx,
        "hiv_file":     f"hiv{hiv_idx:05d}.mp4",
        "off_in_hiv":   off_in_hiv,
        "cross_block":  cross_block,
        # Formateados para UI
        "dt_s":         fmt_dt(ts_s),
        "dt_e":         fmt_dt(ts_e),
        "dur_fmt":      fmt_dur(dur_s) if dur_s > 0 else fmt_dur(0.667),
        "size_fmt":     fmt_sz(size),
        "off_fmt":      fmt_sz(off_in_hiv),
    }


def parse_index(path: str) -> dict:
    """
    Parsea un archivo de índice index*.bin completo.
    Retorna dict con metadata, entries y raw hexdump del header.
    """
    fn   = os.path.basename(path)
    is_p = fn.lower().endswith("p.bin")
    r = {
        "path":          path,
        "filename":      fn,
        "priority":      "principal" if is_p else "backup",
        "size":          0,
        "size_fmt":      "—",
        "header_offset": None,
        "header_ts":     [],
        "header_hex":    "",
        "slots_total":   0,
        "entries":       [],
        "ts_first":      None,
        "ts_last":       None,
        "error":         None,
    }
    try:
        r["size"]     = os.path.getsize(path)
        r["size_fmt"] = fmt_sz(r["size"])
        with open(path, "rb") as f:
            data = f.read()

        start = data.find(b"OFNI")
        if start < 0:
            r["error"] = "Firma OFNI no encontrada"; return r

        r["header_offset"] = start
        # Hexdump primeros 64 bytes del header
        hdr = data[:min(64, start)]
        r["header_hex"] = _hexdump(hdr, 0)

        # Timestamps en el header
        for off in range(0, min(start, 256), 4):
            if off + 4 > len(data): break
            v = struct.unpack_from("<I", data, off)[0]
            if TS_MIN < v < TS_MAX:
                r["header_ts"].append({"offset": off, "ts": v, "dt": fmt_dt(v)})

        entries, slot = [], 0
        off = start
        while off + OFNI_SIZE <= len(data):
            ch = data[off:off + OFNI_SIZE]
            if ch[:4] != b"OFNI":
                break
            slot += 1
            entry = parse_ofni_entry(ch, slot - 1)
            if entry:
                entries.append(entry)
            off += OFNI_SIZE

        r["slots_total"] = slot
        r["entries"]     = entries
        if entries:
            r["ts_first"] = min(e["ts_s"] for e in entries)
            r["ts_last"]  = max(e["ts_s"] for e in entries)

    except Exception as ex:
        r["error"] = str(ex)
    return r


# ── RATS — log de eventos ─────────────────────────────────────────────────────

def parse_log(path: str) -> dict:
    """Parsea un archivo de log logXxx.bin con registros RATS de 72 bytes."""
    r = {
        "path":       path,
        "filename":   os.path.basename(path),
        "size":       0,
        "size_fmt":   "—",
        "header_ts":  None,
        "header_dt":  None,
        "entries":    [],
        "error":      None,
    }
    try:
        r["size"]     = os.path.getsize(path)
        r["size_fmt"] = fmt_sz(r["size"])
        with open(path, "rb") as f:
            data = f.read()

        # Header: primer uint32 = último timestamp de escritura
        if len(data) >= 4:
            v = struct.unpack_from("<I", data, 0)[0]
            if TS_MIN < v < TS_MAX:
                r["header_ts"] = v
                r["header_dt"] = fmt_dt(v)

        entries, idx = [], 0
        pos = 0
        while True:
            pos = data.find(b"RATS", pos)
            if pos < 0 or pos + 72 > len(data):
                break
            ts  = struct.unpack_from("<I", data, pos + 8)[0]
            cod = struct.unpack_from("<I", data, pos + 12)[0]
            if TS_MIN < ts < TS_MAX:
                entries.append({
                    "idx":    idx,
                    "offset": pos,
                    "ts":     ts,
                    "dt":     fmt_dt(ts),
                    "codigo": f"0x{cod:08X}",
                    "tipo":   RATS_EVENTS.get(cod, f"Desconocido (0x{cod:08X})"),
                })
                idx += 1
            pos += 72   # registro RATS = 72 bytes
        r["entries"] = entries

    except Exception as ex:
        r["error"] = str(ex)
    return r


# ── HIV — MPEG-PS raw ─────────────────────────────────────────────────────────

def read_scr(data: bytes, pos: int) -> float | None:
    """
    Parsea el SCR (System Clock Reference) de un MPEG-PS pack header.
    pos = offset de '00 00 01 BA' dentro de data.
    Retorna segundos (float) o None.
    """
    b = data[pos + 4:pos + 9]
    if len(b) < 5:
        return None
    scr = (((b[0] & 0x38) >> 3) << 30 |
           ((b[0] & 0x03))       << 28 |
           (b[1]                 << 20) |
           ((b[2] & 0xf8) >> 3)  << 15 |
           ((b[2] & 0x03))       << 13 |
           (b[3]                 <<  5) |
           ((b[4] & 0xf8) >> 3))
    return scr / 90000.0


def read_ps_header(hiv_path: str, max_bytes: int = 8192) -> bytes:
    """
    Lee la cabecera del sistema del HIV (primer pack MPEG-PS).
    Contiene: System Header (00 00 01 BB) + PS Map (00 00 01 BC) + IDR/SPS/PPS.
    Necesaria para que ffmpeg pueda decodificar los P-frames de un chunk.
    """
    with open(hiv_path, "rb") as f:
        data = f.read(max_bytes)
    second = data.find(b"\x00\x00\x01\xba", 4)
    if second > 0:
        return data[:second]
    return data[:512]


def read_chunk(hiv_path: str, gl_s: int, gl_e: int) -> bytes:
    """
    Lee los bytes de un chunk del índice desde el HIV.
    Maneja el caso cross-block (chunk que cruza dos archivos HIV).
    """
    folder   = os.path.dirname(hiv_path)
    hiv_idx  = gl_s // BLOCK
    off      = gl_s % BLOCK
    size     = gl_e - gl_s
    cross    = gl_e > (hiv_idx + 1) * BLOCK

    if not cross:
        with open(hiv_path, "rb") as f:
            f.seek(off)
            return f.read(size)
    else:
        # Cruce de bloque: leer de hiv{N} y hiv{N+1}
        hiv_b = os.path.join(folder, f"hiv{hiv_idx+1:05d}.mp4")
        with open(hiv_path, "rb") as f:
            f.seek(off)
            p1 = f.read((hiv_idx + 1) * BLOCK - gl_s)
        with open(hiv_b, "rb") as f:
            p2 = f.read(gl_e - (hiv_idx + 1) * BLOCK)
        return p1 + p2


def chunk_duration(chunk: bytes) -> float:
    """
    Calcula la duración real de un chunk leyendo los SCR del MPEG-PS.
    Retorna duración en segundos.
    """
    scrs = []
    for m in re.finditer(b"\x00\x00\x01\xba", chunk):
        scr = read_scr(chunk, m.start())
        if scr is not None:
            scrs.append(scr)
    if len(scrs) < 2:
        return 0.667   # fallback conocido del CS-EB3
    dur = scrs[-1] - scrs[0]
    fps = (len(scrs) - 1) / dur if dur > 0 else 15.0
    return dur + 1.0 / fps


def analyze_hiv_header(hiv_path: str, max_bytes: int = 256) -> dict:
    """
    Analiza los primeros bytes del HIV para confirmar que es MPEG-PS.
    Retorna dict con diagnóstico.
    """
    r = {
        "is_mpeg_ps": False,
        "first_scr":  None,
        "hex_preview": "",
        "error": None,
    }
    try:
        with open(hiv_path, "rb") as f:
            data = f.read(max_bytes)
        r["hex_preview"] = _hexdump(data[:64], 0)
        if data[:4] == b"\x00\x00\x01\xba":
            r["is_mpeg_ps"] = True
            r["first_scr"]  = read_scr(data, 0)
    except Exception as ex:
        r["error"] = str(ex)
    return r


# ── Hex viewer ────────────────────────────────────────────────────────────────

def _hexdump(data: bytes, base_offset: int = 0) -> str:
    """Genera un hexdump legible estilo HxD."""
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i:i+16]
        hex_part  = " ".join(f"{b:02x}" for b in chunk)
        hex_part += "   " * (16 - len(chunk))
        # Grupos de 8 separados por espacio extra
        h1, h2 = hex_part[:23], hex_part[24:47]
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{base_offset+i:08x}  {h1}  {h2}  |{ascii_part}|")
    return "\n".join(lines)


def read_hex_region(path: str, offset: int, length: int = 512) -> dict:
    """
    Lee una región de cualquier archivo binario y retorna hexdump + bytes raw.
    Usado por el visor HEX de la UI.
    """
    r = {
        "path":   path,
        "offset": offset,
        "length": 0,
        "hex":    "",
        "raw":    b"",
        "error":  None,
    }
    try:
        size = os.path.getsize(path)
        offset = max(0, min(offset, size))
        length = min(length, size - offset)
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read(length)
        r["raw"]    = data
        r["length"] = len(data)
        r["hex"]    = _hexdump(data, offset)
    except Exception as ex:
        r["error"] = str(ex)
    return r


# ── Scanner de carpeta ────────────────────────────────────────────────────────

def scan_folder(folder: str) -> dict:
    """
    Escanea una carpeta de SD Hikvision.
    Detecta y parsea todos los archivos index*.bin, log*.bin y hiv*.mp4.
    Retorna un reporte completo.
    """
    report = {
        "folder":     folder,
        "scanned_at": datetime.now().isoformat(),
        "indexes":    [],
        "logs":       [],
        "hiv_files":  [],
        "other":      [],
        "summary":    {},
        "best":       None,
        "error":      None,
    }

    if not os.path.isdir(folder):
        report["error"] = f"No es una carpeta válida: {folder}"
        return report

    idx_files, log_files, hiv_list, other = [], [], [], []
    try:
        for fn in sorted(os.listdir(folder)):
            fl = fn.lower()
            fp = os.path.join(folder, fn)
            if not os.path.isfile(fp):
                continue
            sz = os.path.getsize(fp)
            if   fl.startswith("index") and fl.endswith(".bin"):
                idx_files.append((fn, fp, sz))
            elif fl.startswith("log")   and fl.endswith(".bin"):
                log_files.append((fn, fp, sz))
            elif fl.startswith("hiv")   and fl.endswith(".mp4"):
                hiv_list.append((fn, fp, sz))
            else:
                other.append((fn, fp, sz))
    except PermissionError as ex:
        report["error"] = f"Sin permisos: {ex}"
        return report

    # Ordenar: primero *p.bin (más completo)
    idx_files.sort(key=lambda x: (
        next((int(c) for c in x[0] if c.isdigit()), 9),
        0 if x[0].lower().endswith("p.bin") else 1
    ))

    best = None
    for fn, fp, sz in idx_files:
        d = parse_index(fp)
        report["indexes"].append(d)
        if d["entries"] and (best is None or len(d["entries"]) > len(best["entries"])):
            best = d

    for fn, fp, sz in log_files:
        report["logs"].append(parse_log(fp))

    report["hiv_files"] = [
        {"name": fn, "path": fp, "size": sz, "size_fmt": fmt_sz(sz)}
        for fn, fp, sz in hiv_list
    ]
    report["other"]  = [{"name": fn, "path": fp, "size": sz} for fn, fp, sz in other]
    report["best"]   = best

    ents   = best["entries"] if best else []
    videos = [e for e in ents if e["tipo"] == "Video"]
    fotos  = [e for e in ents if e["tipo"] == "Foto"]
    cross  = [e for e in ents if e["cross_block"]]

    report["summary"] = {
        "best_index":  best["filename"] if best else "—",
        "total":       len(ents),
        "videos":      len(videos),
        "fotos":       len(fotos),
        "cross_block": len(cross),
        "hiv_count":   len(hiv_list),
        "hiv_size":    fmt_sz(sum(s for _, _, s in hiv_list)),
        "date_from":   fmt_dt(best["ts_first"]) if best and best["ts_first"] else "—",
        "date_to":     fmt_dt(best["ts_last"])  if best and best["ts_last"]  else "—",
        "log_events":  sum(len(l["entries"]) for l in report["logs"]),
        "tz_note":     "Hora local cámara (grabada sin offset UTC)",
    }
    return report
