#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import csv
import shutil
import struct
import hashlib
import tempfile
import threading
import platform
import subprocess
import datetime
import tkinter as tk

from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from datetime import timezone
from typing import Optional

UTC = timezone.utc

# ── Constantes índice HIK ─────────────────────────────────────────────────────
INDEX_HDR = 1024
INDEX_REC = 80
TS_MIN = 1_500_000_000
TS_MAX = 2_100_000_000
RESET_THRESH = 1_000_000   # 1 MB
PACK_START = b"\x00\x00\x01\xba"


# ──────────────────────────────────────────────────────────────────────────────
# Helpers parser
# ──────────────────────────────────────────────────────────────────────────────

def _valid_ts(v: int) -> bool:
    return TS_MIN < v < TS_MAX


def discover_indices(sd_root: Path) -> list[Path]:
    found = []
    idx_dir = sd_root / "index"
    if idx_dir.exists():
        found.extend(sorted(idx_dir.glob("index*.bin")))
    found.extend(sorted(sd_root.glob("index*.bin")))

    uniq = []
    seen = set()
    for p in found:
        rp = str(p.resolve())
        if rp not in seen:
            uniq.append(p)
            seen.add(rp)
    return uniq


def parse_index(index_path: Path) -> list[dict]:
    data = index_path.read_bytes()
    if len(data) < INDEX_HDR:
        raise ValueError(f"Índice demasiado pequeño: {index_path.name}")

    body = data[INDEX_HDR:]
    n = len(body) // INDEX_REC

    clips = []
    prev_off = -1
    file_no = 0

    for i in range(n):
        raw = body[i * INDEX_REC:(i + 1) * INDEX_REC]
        if len(raw) < INDEX_REC:
            continue

        u = struct.unpack_from("<20I", raw)
        ts0 = u[0]
        ts_real = u[2]
        off_s = u[6]
        off_e = u[7]

        if not _valid_ts(ts0):
            continue

        if prev_off >= 0 and off_s < prev_off - RESET_THRESH:
            file_no += 1
        prev_off = off_s

        ts_ini = ts_real if _valid_ts(ts_real) else ts0
        dt_ini = datetime.datetime.fromtimestamp(ts_ini, tz=UTC)

        clips.append({
            "rec_idx": i,
            "file_no": file_no,
            "ts_ini": ts_ini,
            "off_start": off_s,
            "off_end": off_e,
            "dt_ini": dt_ini,
            "dt_fin": None,
            "duration": None,
            "mp4_name": f"hiv{file_no:05d}.mp4",
            "size": max(0, off_e - off_s),
            "scr_done": False,
            "integrity": "",
            "source_index": index_path.name,
        })

    return clips


# ──────────────────────────────────────────────────────────────────────────────
# Stream helpers
# ──────────────────────────────────────────────────────────────────────────────

def find_mp4(sd_root: Path, file_no: int) -> Optional[Path]:
    name = f"hiv{file_no:05d}.mp4"
    for p in (
        sd_root / "hiv" / name,
        sd_root / "record" / name,
        sd_root / name,
    ):
        if p.exists():
            return p
    return None


def align_ps_start(src_path: Path, off_start: int, size: int, probe_size: int = 65536) -> tuple[int, int]:
    if size <= 0:
        return off_start, 0

    try:
        with open(src_path, "rb") as f:
            f.seek(off_start)
            probe = f.read(min(probe_size, size))
    except OSError:
        return off_start, size

    shift = probe.find(PACK_START)
    if shift < 0:
        return off_start, size

    real_start = off_start + shift
    real_size = max(0, (off_start + size) - real_start)
    return real_start, real_size


def extract_clip(clip: dict, sd_root: Path, out_path: Path) -> int:
    src_file = find_mp4(sd_root, clip["file_no"])
    if src_file is None:
        raise FileNotFoundError(f"No se encontró {clip['mp4_name']} en {sd_root}")

    size = clip["size"]
    if size <= 0:
        raise ValueError(f"Clip sin datos (off={clip['off_start']}→{clip['off_end']})")

    real_start, real_size = align_ps_start(src_file, clip["off_start"], size)
    if real_size <= 0:
        raise ValueError("Clip vacío tras alinear inicio de stream")

    CHUNK = 4 * 1024 * 1024
    written = 0

    with open(src_file, "rb") as src, open(out_path, "wb") as dst:
        src.seek(real_start)
        remaining = real_size
        while remaining > 0:
            chunk = src.read(min(CHUNK, remaining))
            if not chunk:
                break
            dst.write(chunk)
            written += len(chunk)
            remaining -= len(chunk)

    if written <= 0:
        raise ValueError("No se pudo extraer contenido del clip")

    return written


def _find_scrs(data: bytes) -> list[float]:
    scrs = []
    pos = 0
    while True:
        idx = data.find(PACK_START, pos)
        if idx < 0 or idx + 10 > len(data):
            break
        b = data[idx + 4:idx + 10]
        raw = (b[0] << 40) | (b[1] << 32) | (b[2] << 24) | (b[3] << 16) | (b[4] << 8) | b[5]
        scr = ((raw >> 43) & 0x7) << 30
        scr |= ((raw >> 27) & 0x7FFF) << 15
        scr |= ((raw >> 11) & 0x7FFF)
        val = scr / 90000.0
        if 0 <= val < 86400 * 7:
            scrs.append(val)
        pos = idx + 4
    return scrs


def scr_duration(src_path: Path, off_start: int, off_end: int) -> Optional[float]:
    size = off_end - off_start
    if size <= 0:
        return None

    try:
        real_start, real_size = align_ps_start(src_path, off_start, size)
    except Exception:
        real_start, real_size = off_start, size

    if real_size <= 0:
        return None

    scan = min(262144, real_size)

    try:
        with open(src_path, "rb") as f:
            f.seek(real_start)
            head = f.read(scan)
            if real_size > scan * 2:
                f.seek(real_start + real_size - scan)
                tail = f.read(scan)
            else:
                tail = head
    except OSError:
        return None

    sh = _find_scrs(head)
    st = _find_scrs(tail)
    if not sh or not st:
        return None

    dur = max(st) - min(sh)
    if dur < 0 or dur > 7200:
        return None
    return dur


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def probe_streams(src_path: Path) -> tuple[bool, str]:
    if not ffprobe_available():
        return False, "ffprobe no disponible"

    try:
        res = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "stream=index,codec_type,codec_name",
                "-of", "compact=p=0:nk=1",
                str(src_path),
            ],
            capture_output=True,
            text=True,
        )
        ok = res.returncode == 0 and bool(res.stdout.strip())
        detail = (res.stdout or res.stderr or "").strip()
        return ok, detail
    except Exception as e:
        return False, str(e)


def convert_to_mp4(src_ps: Path, dst_mp4: Path) -> subprocess.CompletedProcess:
    res = subprocess.run(
        [
            "ffmpeg", "-y",
            "-fflags", "+genpts",
            "-i", str(src_ps),
            "-map", "0:v:0?",
            "-map", "0:a:0?",
            "-c", "copy",
            "-movflags", "+faststart",
            str(dst_mp4),
        ],
        capture_output=True,
        text=True,
    )
    if res.returncode == 0:
        return res

    return subprocess.run(
        [
            "ffmpeg", "-y",
            "-fflags", "+genpts",
            "-err_detect", "ignore_err",
            "-i", str(src_ps),
            "-map", "0:v:0?",
            "-map", "0:a:0?",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(dst_mp4),
        ],
        capture_output=True,
        text=True,
    )


def detect_gaps(clips: list[dict], threshold_s: float = 60.0) -> list[dict]:
    gaps = []
    with_fin = [c for c in clips if c["dt_fin"] is not None]
    with_fin.sort(key=lambda c: c["dt_ini"])
    for i in range(1, len(with_fin)):
        prev = with_fin[i - 1]
        curr = with_fin[i]
        gap_s = (curr["dt_ini"] - prev["dt_fin"]).total_seconds()
        if gap_s >= threshold_s:
            gaps.append({
                "dt_start": prev["dt_fin"],
                "dt_end": curr["dt_ini"],
                "duration_s": gap_s,
            })
    return gaps


def file_hashes(path: Path, algos=("sha256",), chunk_size: int = 1024 * 1024) -> dict:
    hs = {a: hashlib.new(a) for a in algos}
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            for h in hs.values():
                h.update(chunk)
    return {name: h.hexdigest() for name, h in hs.items()}


# ──────────────────────────────────────────────────────────────────────────────
# Formato / helpers UI
# ──────────────────────────────────────────────────────────────────────────────

def fmt_dt(dt):
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d  %H:%M:%S")


def fmt_size(n):
    if n >= 1_048_576:
        return f"{n/1_048_576:.1f} MB"
    if n >= 1024:
        return f"{n/1024:.0f} KB"
    return f"{n} B"


def fmt_dur(s):
    if s is None or s == 0:
        return "—"
    s = int(round(s))
    if s >= 3600:
        return f"{s//3600}h {(s%3600)//60}m {s%60}s"
    if s >= 60:
        return f"{(s%3600)//60}m {s%60}s"
    return f"{s}s"


def clip_filename(clip: dict, ext: str = "ps") -> str:
    dt = clip["dt_ini"]
    dur = int(round(clip["duration"])) if clip["duration"] is not None else 0
    return f"clip_{dt.strftime('%Y%m%d_%H%M%S')}_{dur}s.{ext}"


def open_file(path: str):
    try:
        if platform.system() == "Windows":
            os.startfile(path)
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        messagebox.showerror("Error al abrir", str(e))


# ──────────────────────────────────────────────────────────────────────────────
# Ventana de hashes
# ──────────────────────────────────────────────────────────────────────────────

class HashWindow(tk.Toplevel):
    def __init__(self, master, initial_dir: Path):
        super().__init__(master)
        self.title("Hashes / Integridad")
        self.geometry("800x480")
        self.minsize(600, 300)
        self.transient(master)

        self._results: list[dict] = []
        self._var_dir = tk.StringVar(value=str(initial_dir))
        self._var_sha256 = tk.BooleanVar(value=True)
        self._var_md5 = tk.BooleanVar(value=True)
        self._var_only_mp4 = tk.BooleanVar(value=True)
        self._var_include_ps = tk.BooleanVar(value=False)
        self._var_status = tk.StringVar(value="Listo.")
        self._var_prog = tk.DoubleVar(value=0)

        self._build_ui()

    def _build_ui(self):
        top = tk.Frame(self, padx=8, pady=8)
        top.pack(fill="x")

        tk.Label(top, text="Carpeta:").pack(side="left")
        tk.Entry(top, textvariable=self._var_dir, width=65).pack(side="left", padx=4, fill="x", expand=True)
        tk.Button(top, text="Abrir…", command=self._browse_dir).pack(side="left", padx=2)
        tk.Button(top, text="Calcular hashes", command=self._run_hashes, relief="groove", bg="#e0ffe0").pack(side="left", padx=6)
        tk.Button(top, text="Exportar CSV", command=self._export_csv, relief="groove").pack(side="left", padx=2)

        opts = tk.Frame(self, padx=8, pady=4)
        opts.pack(fill="x")
        tk.Checkbutton(opts, text="SHA-256", variable=self._var_sha256).pack(side="left")
        tk.Checkbutton(opts, text="MD5", variable=self._var_md5).pack(side="left", padx=(8, 16))
        tk.Checkbutton(opts, text="Solo .mp4", variable=self._var_only_mp4).pack(side="left")
        tk.Checkbutton(opts, text="Incluir .ps", variable=self._var_include_ps).pack(side="left", padx=(8, 0))

        mid = tk.Frame(self, padx=8, pady=6)
        mid.pack(fill="both", expand=True)

        cols = ("archivo", "ext", "tamanio", "sha256", "md5")
        self._tree = ttk.Treeview(mid, columns=cols, show="headings")
        headers = [
            ("archivo", "Archivo", 260, "w"),
            ("ext", "Ext", 60, "c"),
            ("tamanio", "Tamaño", 100, "e"),
            ("sha256", "SHA-256", 340, "w"),
            ("md5", "MD5", 240, "w"),
        ]
        for col, hdr, w, anc in headers:
            self._tree.heading(col, text=hdr, anchor=anc)
            self._tree.column(col, width=w, anchor=anc, stretch=(col in {"archivo", "sha256", "md5"}))

        vsb = ttk.Scrollbar(mid, orient="vertical", command=self._tree.yview)
        hsb = ttk.Scrollbar(mid, orient="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        mid.rowconfigure(0, weight=1)
        mid.columnconfigure(0, weight=1)

        bot = tk.Frame(self, padx=8, pady=6)
        bot.pack(fill="x", side="bottom")
        tk.Label(bot, textvariable=self._var_status, anchor="w", fg="gray").pack(side="left")
        self._pbar = ttk.Progressbar(bot, variable=self._var_prog, maximum=100, length=180)
        self._pbar.pack(side="right")

    def _browse_dir(self):
        d = filedialog.askdirectory(title="Carpeta con archivos extraídos", parent=self)
        if d:
            self._var_dir.set(d)

    def _collect_files(self) -> list[Path]:
        root = Path(self._var_dir.get().strip())
        if not root.exists() or not root.is_dir():
            raise ValueError("La carpeta indicada no existe o no es válida")

        files = []
        for p in sorted(root.iterdir()):
            if not p.is_file():
                continue
            ext = p.suffix.lower()
            if self._var_only_mp4.get() and ext == ".mp4":
                files.append(p)
            elif self._var_include_ps.get() and ext == ".ps":
                files.append(p)
            elif not self._var_only_mp4.get() and not self._var_include_ps.get() and ext in {".mp4", ".ps"}:
                files.append(p)
        return files

    def _run_hashes(self):
        algos = []
        if self._var_sha256.get():
            algos.append("sha256")
        if self._var_md5.get():
            algos.append("md5")
        if not algos:
            messagebox.showwarning("Sin hash", "Seleccione al menos un algoritmo.", parent=self)
            return

        try:
            files = self._collect_files()
        except Exception as e:
            messagebox.showerror("Error", str(e), parent=self)
            return

        if not files:
            messagebox.showinfo("Sin archivos", "No se encontraron archivos según el filtro actual.", parent=self)
            return

        self._tree.delete(*self._tree.get_children())
        self._results = []
        self._var_prog.set(0)
        self._pbar.configure(maximum=len(files))
        self._var_status.set(f"Calculando hashes de {len(files)} archivo(s)…")

        threading.Thread(target=self._hash_worker, args=(files, tuple(algos)), daemon=True).start()

    def _hash_worker(self, files: list[Path], algos: tuple[str, ...]):
        results = []
        for i, path in enumerate(files, 1):
            hs = file_hashes(path, algos=algos)
            row = {
                "archivo": path.name,
                "ext": path.suffix.lower(),
                "size_bytes": path.stat().st_size,
                "sha256": hs.get("sha256", ""),
                "md5": hs.get("md5", ""),
                "full_path": str(path),
            }
            results.append(row)

            def _ui(row=row, i=i):
                self._results.append(row)
                self._tree.insert("", "end", values=(
                    row["archivo"],
                    row["ext"],
                    fmt_size(row["size_bytes"]),
                    row["sha256"],
                    row["md5"],
                ))
                self._var_prog.set(i)
                self._var_status.set(f"[{i}/{len(files)}] {row['archivo']}")

            self.after(0, _ui)

        def _done():
            self._var_status.set(f"Listo. {len(results)} archivo(s) hasheados.")

        self.after(0, _done)

    def _export_csv(self):
        if not self._results:
            messagebox.showinfo("Sin datos", "Primero calcule los hashes.", parent=self)
            return

        out = filedialog.asksaveasfilename(
            parent=self,
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("Todos", "*.*")],
            initialfile="hashes.csv",
            title="Guardar CSV de hashes",
        )
        if not out:
            return

        fields = ["archivo", "ext", "size_bytes", "sha256", "md5", "full_path"]
        try:
            with open(out, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                for row in self._results:
                    w.writerow(row)
            messagebox.showinfo("CSV exportado", f"Hashes exportados a:\n{out}", parent=self)
        except Exception as e:
            messagebox.showerror("Error", str(e), parent=self)


# ──────────────────────────────────────────────────────────────────────────────
# GUI principal
# ──────────────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("sd-hik-reader  v6.2")
        self.geometry("1024x500")
        self.minsize(600, 300)

        self._clips: list[dict] = []
        self._iid_map: dict[str, dict] = {}
        self._sd_root: Optional[Path] = None
        self._indices: list[Path] = []

        self._sort_col = "inicio"
        self._sort_rev = False

        self._build_ui()

    def _build_ui(self):
        f1 = tk.Frame(self, pady=6, padx=8)
        f1.pack(fill="x")

        tk.Label(f1, text="SD / carpeta:").pack(side="left")
        self._var_sd = tk.StringVar()
        tk.Entry(f1, textvariable=self._var_sd, width=46).pack(side="left", padx=4)
        tk.Button(f1, text="Abrir…", command=self._browse_sd).pack(side="left", padx=2)

        tk.Label(f1, text="Índice:").pack(side="left", padx=(10, 4))
        self._var_index = tk.StringVar()
        self._cmb_index = ttk.Combobox(f1, textvariable=self._var_index, state="readonly", width=18)
        self._cmb_index.pack(side="left")

        tk.Button(f1, text="Escanear", command=self._scan_indices, relief="groove").pack(side="left", padx=6)
        tk.Button(f1, text="Cargar", command=self._load, relief="groove", bg="#e0ffe0").pack(side="left", padx=4)

        self._lbl_info = tk.Label(f1, text="", fg="gray")
        self._lbl_info.pack(side="left", padx=8)

        f2 = tk.Frame(self, pady=4, padx=8)
        f2.pack(fill="x")

        meses = ["---", "Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
        dias = ["--"] + [f"{d:02d}" for d in range(1, 32)]
        anios = ["----"] + [str(a) for a in range(2000, 2051)]
        horas = ["--"] + [f"{h:02d}" for h in range(0, 24)]
        mins = ["--"] + [f"{m:02d}" for m in range(0, 60)]

        def make_date_combo(parent):
            dd = ttk.Combobox(parent, values=dias, width=3, state="readonly")
            mm = ttk.Combobox(parent, values=meses, width=4, state="readonly")
            yy = ttk.Combobox(parent, values=anios, width=6, state="readonly")
            dd.set("--")
            mm.set("---")
            yy.set("----")
            return dd, mm, yy

        def make_time_combo(parent):
            hh = ttk.Combobox(parent, values=horas, width=3, state="readonly")
            mi = ttk.Combobox(parent, values=mins, width=3, state="readonly")
            hh.set("--")
            mi.set("--")
            return hh, mi

        tk.Label(f2, text="Inicio:").pack(side="left")
        self._fd_dd, self._fd_mm, self._fd_yy = make_date_combo(f2)
        self._fd_dd.pack(side="left", padx=(4, 1))
        self._fd_mm.pack(side="left", padx=1)
        self._fd_yy.pack(side="left", padx=(1, 4))
        self._fd_hh, self._fd_mi = make_time_combo(f2)
        self._fd_hh.pack(side="left", padx=1)
        tk.Label(f2, text=":", fg="gray").pack(side="left")
        self._fd_mi.pack(side="left", padx=(0, 10))

        tk.Label(f2, text="Fin:").pack(side="left")
        self._fh_dd, self._fh_mm, self._fh_yy = make_date_combo(f2)
        self._fh_dd.pack(side="left", padx=(4, 1))
        self._fh_mm.pack(side="left", padx=1)
        self._fh_yy.pack(side="left", padx=(1, 4))
        self._fh_hh, self._fh_mi = make_time_combo(f2)
        self._fh_hh.pack(side="left", padx=1)
        tk.Label(f2, text=":", fg="gray").pack(side="left")
        self._fh_mi.pack(side="left", padx=(0, 8))

        tk.Button(f2, text="Filtrar", command=self._apply_filter, relief="groove").pack(side="left", padx=2)
        tk.Button(f2, text="Limpiar", command=self._clear_filter, relief="groove").pack(side="left", padx=2)
        self._lbl_filter = tk.Label(f2, text="", fg="gray")
        self._lbl_filter.pack(side="left", padx=8)

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=8)

        body = tk.PanedWindow(self, orient="horizontal", sashwidth=5, sashrelief="raised")
        body.pack(fill="both", expand=True, padx=8, pady=4)

        left = tk.Frame(body)
        body.add(left, minsize=640)

        cols = ("n", "inicio", "fin", "duracion", "tamanio", "archivo", "integ")
        self._tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="extended")
        headers = [
            ("n", "#", 42, "e"),
            ("inicio", "Inicio", 170, "w"),
            ("fin", "Fin", 90, "w"),
            ("duracion", "Duración", 80, "e"),
            ("tamanio", "Tamaño", 80, "e"),
            ("archivo", "Archivo", 120, "w"),
            ("integ", "✓", 36, "c"),
        ]
        for col, hdr, w, anc in headers:
            self._tree.heading(col, text=hdr, anchor=anc, command=lambda c=col: self._sort_by(c))
            self._tree.column(col, width=w, anchor=anc, stretch=(col == "inicio"), minwidth=24)

        vsb = ttk.Scrollbar(left, orient="vertical", command=self._tree.yview)
        hsb = ttk.Scrollbar(left, orient="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)

        self._tree.bind("<<TreeviewSelect>>", self._on_select)
        self._tree.bind("<Double-1>", self._on_double_click)

        right = tk.Frame(body, padx=10, pady=6)
        body.add(right, minsize=260)

        tk.Label(right, text="Clip seleccionado", font=("", 10, "bold"), anchor="w").pack(fill="x")
        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=4)

        self._info_labels = {}
        info_rows = [
            ("Inicio", "inicio"),
            ("Fin", "fin"),
            ("Duración", "duracion"),
            ("Archivo", "archivo"),
            ("Offset", "offset"),
            ("Tamaño", "tamanio"),
            ("Integridad", "integ"),
            ("Índice", "indice"),
        ]
        for label, key in info_rows:
            row = tk.Frame(right)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=f"{label}:", width=10, anchor="e", fg="gray").pack(side="left")
            lbl = tk.Label(row, text="—", anchor="w", font=("Consolas", 9))
            lbl.pack(side="left", fill="x", expand=True)
            self._info_labels[key] = lbl

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=8)

        tk.Label(right, text="Carpeta de salida:", anchor="w").pack(fill="x")
        out_row = tk.Frame(right)
        out_row.pack(fill="x", pady=(2, 2))
        self._var_out = tk.StringVar(value=str(Path.home()))
        tk.Entry(out_row, textvariable=self._var_out).pack(side="left", fill="x", expand=True)
        tk.Button(out_row, text="…", width=3, command=self._browse_out).pack(side="left", padx=(2, 0))

        self._var_convert = tk.BooleanVar(value=False)
        self._var_keep_ps_on_fail = tk.BooleanVar(value=True)
        self._chk_convert = tk.Checkbutton(right, text="Convertir a .mp4 (ffmpeg)", variable=self._var_convert)
        self._chk_convert.pack(anchor="w", pady=(2, 2))
        tk.Checkbutton(right, text="Conservar .ps si falla conversión", variable=self._var_keep_ps_on_fail).pack(anchor="w")

        if not ffmpeg_available():
            self._chk_convert.configure(state="disabled")
            tk.Label(right, text="  ffmpeg no encontrado", fg="gray", font=("", 8)).pack(anchor="w")

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=6)

        tk.Button(right, text="▶  Ver clip", command=self._ver_selected, relief="groove").pack(fill="x", pady=2)
        tk.Button(right, text="⬇  Extraer seleccionados", command=self._extract_selected, relief="groove", bg="#ddeeff").pack(fill="x", pady=2)
        tk.Button(right, text="⬇  Extraer visibles", command=self._extract_all, relief="groove").pack(fill="x", pady=2)

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=6)

        tk.Label(right, text="Herramientas", font=("", 9, "bold"), anchor="w").pack(fill="x")
        tk.Button(right, text="📋  Exportar CSV", command=self._export_csv, relief="groove").pack(fill="x", pady=2)
        tk.Button(right, text="⏱  Ver gaps de grabación", command=self._show_gaps, relief="groove").pack(fill="x", pady=2)
        tk.Button(right, text="🔎  Probar stream seleccionado", command=self._probe_selected, relief="groove").pack(fill="x", pady=2)
        tk.Button(right, text="🔐  Hashes / Integridad", command=self._open_hash_window, relief="groove").pack(fill="x", pady=2)

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=6)
        tk.Button(right, text="Cerrar", command=self.destroy, relief="groove", fg="gray").pack(fill="x")

        bot = tk.Frame(self, pady=3, padx=8)
        bot.pack(fill="x", side="bottom")
        self._var_status = tk.StringVar(value="Listo.")
        tk.Label(bot, textvariable=self._var_status, anchor="w", fg="gray", font=("", 9)).pack(side="left")
        self._var_prog = tk.DoubleVar(value=0)
        self._pbar = ttk.Progressbar(bot, variable=self._var_prog, maximum=100, length=180)
        self._pbar.pack(side="right")

    def _iid(self, clip: dict) -> str:
        return str(clip["rec_idx"])

    def _clip_from_iid(self, iid: str) -> Optional[dict]:
        return self._iid_map.get(iid)

    def _combo_date(self, dd_w, mm_w, yy_w, hh_w=None, mi_w=None):
        dd = dd_w.get()
        mm = mm_w.get()
        yy = yy_w.get()
        if dd == "--" or mm == "---" or yy == "----":
            return None

        meses = {"Ene": 1, "Feb": 2, "Mar": 3, "Abr": 4, "May": 5, "Jun": 6,
                 "Jul": 7, "Ago": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dic": 12}
        try:
            hh = int(hh_w.get()) if hh_w and hh_w.get() != "--" else 0
            mi = int(mi_w.get()) if mi_w and mi_w.get() != "--" else 0
            return datetime.datetime(int(yy), meses[mm], int(dd), hh, mi, 0, tzinfo=UTC)
        except (ValueError, KeyError):
            return None

    def _set_status(self, msg: str):
        self._var_status.set(msg)

    def _browse_sd(self):
        d = filedialog.askdirectory(title="Raíz de la SD")
        if d:
            self._var_sd.set(d)
            self._scan_indices()

    def _browse_out(self):
        d = filedialog.askdirectory(title="Carpeta de salida")
        if d:
            self._var_out.set(d)

    def _scan_indices(self):
        sd_s = self._var_sd.get().strip()
        if not sd_s:
            return
        sd_root = Path(sd_s)
        if not sd_root.exists():
            messagebox.showerror("Error", "La carpeta indicada no existe.")
            return

        self._indices = discover_indices(sd_root)
        names = [p.name for p in self._indices]
        self._cmb_index["values"] = names

        if names:
            pref = "index00.bin" if "index00.bin" in names else names[0]
            self._var_index.set(pref)
            self._lbl_info.configure(text=f"{len(names)} índice(s) detectado(s)", fg="darkgreen")
        else:
            self._var_index.set("")
            self._lbl_info.configure(text="No se encontraron índices", fg="firebrick")

    def _load(self):
        sd_s = self._var_sd.get().strip()
        if not sd_s:
            messagebox.showwarning("Sin carpeta", "Seleccione la carpeta raíz de la SD primero.")
            return

        sd_root = Path(sd_s)
        if not sd_root.exists():
            messagebox.showerror("Error", "La carpeta indicada no existe.")
            return

        if not self._indices:
            self._scan_indices()

        idx_name = self._var_index.get().strip()
        if not idx_name:
            messagebox.showerror("Índice no encontrado", f"No se encontró ningún index*.bin en:\n{sd_root}")
            return

        index_path = next((p for p in self._indices if p.name == idx_name), None)
        if index_path is None:
            messagebox.showerror("Índice inválido", "El índice seleccionado ya no está disponible.")
            return

        self._sd_root = sd_root
        self._set_status(f"Cargando {index_path.name}…")
        self._var_prog.set(0)
        self._pbar.configure(mode="indeterminate")
        self._pbar.start(10)
        threading.Thread(target=self._load_worker, args=(index_path,), daemon=True).start()

    def _load_worker(self, index_path: Path):
        try:
            clips = parse_index(index_path)
            self.after(0, lambda: self._on_loaded(clips, index_path))
        except Exception as e:
            self.after(0, lambda: self._on_load_error(str(e)))

    def _on_loaded(self, clips: list[dict], index_path: Path):
        self._pbar.stop()
        self._pbar.configure(mode="determinate")
        self._var_prog.set(0)

        self._clips = clips
        self._iid_map = {self._iid(c): c for c in clips}
        self._populate_tree(clips)
        self._populate_date_combos(clips)

        n_mp4 = len({c["file_no"] for c in clips})
        self._lbl_info.configure(
            text=f"{index_path.name} | {len(clips)} clips | {n_mp4} archivos fuente | calculando…",
            fg="darkgreen"
        )
        self._set_status(f"Cargado: {len(clips)} clips desde {index_path.name}. Calculando duraciones…")

        if self._sd_root:
            threading.Thread(target=self._batch_scr_worker, args=(clips,), daemon=True).start()

    def _on_load_error(self, msg: str):
        self._pbar.stop()
        self._pbar.configure(mode="determinate")
        messagebox.showerror("Error al cargar", msg)
        self._set_status(f"Error: {msg}")

    def _populate_date_combos(self, clips: list[dict]):
        if not clips:
            return
        meses_n = ["---", "Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]
        dt_first = min(c["dt_ini"] for c in clips)
        dt_last = max(c["dt_ini"] for c in clips)
        for dd, mm, yy, dt in [
            (self._fd_dd, self._fd_mm, self._fd_yy, dt_first),
            (self._fh_dd, self._fh_mm, self._fh_yy, dt_last),
        ]:
            dd.set(f"{dt.day:02d}")
            mm.set(meses_n[dt.month])
            yy.set(str(dt.year))

    def _batch_scr_worker(self, clips: list[dict]):
        total = len(clips)
        done = 0
        total_s = 0.0
        for clip in clips:
            if clip.get("scr_done"):
                done += 1
                continue
            src_file = find_mp4(self._sd_root, clip["file_no"]) if self._sd_root else None
            dur = scr_duration(src_file, clip["off_start"], clip["off_end"]) if src_file else None
            clip["scr_done"] = True

            if dur is not None and dur > 0:
                clip["duration"] = dur
                clip["dt_fin"] = clip["dt_ini"] + datetime.timedelta(seconds=dur)
                total_s += dur
                est_dur = clip["size"] / (3_440_000 / 8) if clip["size"] > 0 else 0
                rel = abs(dur - est_dur) / max(dur, 1)
                clip["integrity"] = "✓" if rel < 0.5 else "!"

                iid = self._iid(clip)
                fin_str = clip["dt_fin"].strftime("%H:%M:%S")
                dur_str = fmt_dur(dur)
                integ = clip["integrity"]

                def _update(iid=iid, fin_str=fin_str, dur_str=dur_str, integ=integ):
                    if self._tree.exists(iid):
                        self._tree.set(iid, "fin", fin_str)
                        self._tree.set(iid, "duracion", dur_str)
                        self._tree.set(iid, "integ", integ)
                self.after(0, _update)
            else:
                clip["integrity"] = "?"

            done += 1
            if done % 10 == 0 or done == total:
                pct = int(done * 100 / max(total, 1))
                msg = f"SCR: {done}/{total} clips | {total_s/60:.1f} min"
                def _prog(p=pct, m=msg):
                    self._var_prog.set(p)
                    self._set_status(m)
                self.after(0, _prog)

        nc = len(clips)
        n_mp4 = len({c["file_no"] for c in clips})
        ts = total_s
        def _final():
            self._lbl_info.configure(text=f"{nc} clips | {n_mp4} archivos fuente | {ts/60:.1f} min", fg="darkgreen")
            self._set_status(f"Listo. {nc} clips | {ts/60:.1f} min grabados.")
            self._var_prog.set(100)
        self.after(0, _final)

    def _populate_tree(self, clips: list[dict]):
        self._tree.delete(*self._tree.get_children())
        try:
            clips = sorted(clips, key=lambda c: c["dt_ini"], reverse=self._sort_rev)
        except Exception:
            pass
        for n, c in enumerate(clips, 1):
            dt_ini = c["dt_ini"].strftime("%d-%m-%Y  %H:%M:%S")
            dt_fin = c["dt_fin"].strftime("%H:%M:%S") if c["dt_fin"] else "—"
            self._tree.insert("", "end", iid=self._iid(c), values=(
                n, dt_ini, dt_fin, fmt_dur(c["duration"]), fmt_size(c["size"]), c["mp4_name"], c.get("integrity", "")
            ))

    def _sort_by(self, col: str):
        if self._sort_col == col:
            self._sort_rev = not self._sort_rev
        else:
            self._sort_col = col
            self._sort_rev = False

        shown_clips = []
        for iid in self._tree.get_children():
            clip = self._clip_from_iid(iid)
            if clip:
                shown_clips.append(clip)

        key_fn = {
            "inicio": lambda c: c["dt_ini"],
            "fin": lambda c: c["dt_fin"] or c["dt_ini"],
            "duracion": lambda c: c["duration"] or 0,
            "tamanio": lambda c: c["size"],
            "archivo": lambda c: c["mp4_name"],
            "n": lambda c: c["rec_idx"],
            "integ": lambda c: c.get("integrity", ""),
        }.get(col, lambda c: c["dt_ini"])

        shown_clips.sort(key=key_fn, reverse=self._sort_rev)
        self._tree.delete(*self._tree.get_children())
        for n, c in enumerate(shown_clips, 1):
            dt_ini = c["dt_ini"].strftime("%d-%m-%Y  %H:%M:%S")
            dt_fin = c["dt_fin"].strftime("%H:%M:%S") if c["dt_fin"] else "—"
            self._tree.insert("", "end", iid=self._iid(c), values=(
                n, dt_ini, dt_fin, fmt_dur(c["duration"]), fmt_size(c["size"]), c["mp4_name"], c.get("integrity", "")
            ))

        arrow = " ▲" if not self._sort_rev else " ▼"
        for col_id in ("n", "inicio", "fin", "duracion", "tamanio", "archivo", "integ"):
            txt = self._tree.heading(col_id, "text")
            txt = txt.replace(" ▲", "").replace(" ▼", "")
            if col_id == col:
                txt += arrow
            self._tree.heading(col_id, text=txt)

    def _on_select(self, _evt=None):
        sel = self._tree.selection()
        if not sel:
            return
        clip = self._clip_from_iid(sel[-1])
        if clip is None:
            return

        self._info_labels["inicio"].configure(text=clip["dt_ini"].strftime("%d-%m-%Y  %H:%M:%S"))
        self._info_labels["fin"].configure(text=fmt_dt(clip["dt_fin"]))
        self._info_labels["duracion"].configure(text=fmt_dur(clip["duration"]))
        self._info_labels["archivo"].configure(text=clip["mp4_name"])
        self._info_labels["offset"].configure(text=f"{clip['off_start']:,} → {clip['off_end']:,}")
        self._info_labels["tamanio"].configure(text=fmt_size(clip["size"]))
        self._info_labels["integ"].configure(text=clip.get("integrity", "pendiente"))
        self._info_labels["indice"].configure(text=clip.get("source_index", "—"))

        if self._sd_root and not clip.get("scr_done"):
            self._info_labels["duracion"].configure(text="calculando…", fg="gray")
            self._info_labels["fin"].configure(text="calculando…", fg="gray")
            threading.Thread(target=self._calc_scr_one, args=(clip,), daemon=True).start()

    def _calc_scr_one(self, clip: dict):
        src_file = find_mp4(self._sd_root, clip["file_no"]) if self._sd_root else None
        dur = scr_duration(src_file, clip["off_start"], clip["off_end"]) if src_file else None
        clip["scr_done"] = True
        iid = self._iid(clip)

        if dur is not None and dur > 0:
            clip["duration"] = dur
            clip["dt_fin"] = clip["dt_ini"] + datetime.timedelta(seconds=dur)
            est_dur = clip["size"] / (3_440_000 / 8) if clip["size"] > 0 else 0
            clip["integrity"] = "✓" if abs(dur - est_dur) / max(dur, 1) < 0.5 else "!"

            fin_full = clip["dt_fin"].strftime("%Y-%m-%d  %H:%M:%S")
            fin_short = clip["dt_fin"].strftime("%H:%M:%S")
            dur_str = fmt_dur(dur)
            integ = clip["integrity"]

            def _apply():
                self._info_labels["duracion"].configure(text=dur_str, fg="black")
                self._info_labels["fin"].configure(text=fin_full, fg="black")
                self._info_labels["integ"].configure(text=integ)
                if self._tree.exists(iid):
                    self._tree.set(iid, "fin", fin_short)
                    self._tree.set(iid, "duracion", dur_str)
                    self._tree.set(iid, "integ", integ)
            self.after(0, _apply)
        else:
            def _none():
                self._info_labels["duracion"].configure(text="—", fg="black")
                self._info_labels["fin"].configure(text="—", fg="black")
            self.after(0, _none)

    def _on_double_click(self, _evt=None):
        self._ver_selected()

    def _apply_filter(self):
        if not self._clips:
            messagebox.showinfo("Sin datos", "Cargue un índice primero.")
            return

        d_desde = self._combo_date(self._fd_dd, self._fd_mm, self._fd_yy, self._fd_hh, self._fd_mi)
        d_hasta = self._combo_date(self._fh_dd, self._fh_mm, self._fh_yy, self._fh_hh, self._fh_mi)
        if d_hasta and self._fh_hh.get() == "--":
            d_hasta = d_hasta.replace(hour=23, minute=59, second=59)

        result = self._clips
        if d_desde:
            result = [c for c in result if c["dt_ini"] >= d_desde]
        if d_hasta:
            result = [c for c in result if c["dt_ini"] <= d_hasta]

        self._populate_tree(result)
        self._lbl_filter.configure(text=f"{len(result)} de {len(self._clips)} clips")
        self._set_status(f"Filtro aplicado: {len(result)} clips.")

    def _clear_filter(self):
        for w in (self._fd_dd, self._fh_dd):
            w.set("--")
        for w in (self._fd_mm, self._fh_mm):
            w.set("---")
        for w in (self._fd_yy, self._fh_yy):
            w.set("----")
        for w in (self._fd_hh, self._fh_hh):
            w.set("--")
        for w in (self._fd_mi, self._fh_mi):
            w.set("--")

        self._populate_tree(self._clips)
        self._lbl_filter.configure(text="")
        self._set_status(f"Filtro limpiado. {len(self._clips)} clips.")

    def _get_selected_clips(self) -> list[dict]:
        sel = self._tree.selection()
        if not sel:
            messagebox.showinfo("Sin selección", "Seleccione uno o más clips en la tabla.")
            return []
        return [c for iid in sel if (c := self._clip_from_iid(iid)) is not None]

    def _ver_selected(self):
        clips = self._get_selected_clips()
        if not clips or self._sd_root is None:
            return
        clip = clips[0]
        tmp_dir = Path(tempfile.mkdtemp(prefix="sdhik_"))
        raw_path = tmp_dir / clip_filename(clip, "ps")
        self._set_status("Extrayendo para previsualizar…")
        try:
            extract_clip(clip, self._sd_root, raw_path)
            if self._var_convert.get() and ffmpeg_available():
                mp4_path = tmp_dir / clip_filename(clip, "mp4")
                res = convert_to_mp4(raw_path, mp4_path)
                if res.returncode == 0 and mp4_path.exists():
                    open_file(str(mp4_path))
                    self._set_status(f"Abriendo: {mp4_path.name}")
                    return
            open_file(str(raw_path))
            self._set_status(f"Abriendo: {raw_path.name}")
        except Exception as e:
            messagebox.showerror("Error al extraer", str(e))
            self._set_status(f"Error: {e}")

    def _extract_selected(self):
        clips = self._get_selected_clips()
        if clips:
            self._run_extraction(clips)

    def _extract_all(self):
        if not self._clips:
            messagebox.showinfo("Sin clips", "Cargue un índice primero.")
            return
        visible = [self._clip_from_iid(iid) for iid in self._tree.get_children()]
        visible = [c for c in visible if c]
        if len(visible) > 50:
            if not messagebox.askyesno("Extracción masiva", f"Se extraerán {len(visible)} clips.\n¿Continuar?"):
                return
        self._run_extraction(visible)

    def _run_extraction(self, clips: list[dict]):
        if self._sd_root is None:
            messagebox.showwarning("Sin SD", "Seleccione la carpeta de la SD.")
            return

        out_dir = Path(self._var_out.get())
        out_dir.mkdir(parents=True, exist_ok=True)
        convert = self._var_convert.get() and ffmpeg_available()
        keep_ps_on_fail = self._var_keep_ps_on_fail.get()

        self._var_prog.set(0)
        self._pbar.configure(mode="determinate", maximum=len(clips))
        self._set_status(f"Extrayendo {len(clips)} clips…")
        threading.Thread(target=self._extract_worker, args=(clips, self._sd_root, out_dir, convert, keep_ps_on_fail), daemon=True).start()

    def _extract_worker(self, clips, sd_root, out_dir, convert: bool, keep_ps_on_fail: bool):
        ok_count = 0
        mp4_ok = 0
        ps_only = 0
        errors = []

        for i, clip in enumerate(clips):
            raw_name = clip_filename(clip, "ps")
            raw_path = out_dir / raw_name

            def _prog(i=i, raw_name=raw_name):
                self._var_prog.set(i)
                self._set_status(f"[{i+1}/{len(clips)}] {raw_name}…")
            self.after(0, _prog)

            try:
                extract_clip(clip, sd_root, raw_path)

                if convert:
                    mp4_path = out_dir / clip_filename(clip, "mp4")
                    res = convert_to_mp4(raw_path, mp4_path)
                    if res.returncode == 0 and mp4_path.exists():
                        raw_path.unlink(missing_ok=True)
                        mp4_ok += 1
                        ok_count += 1
                    else:
                        tail = (res.stderr or res.stdout or "").strip()[-400:]
                        if not keep_ps_on_fail:
                            raw_path.unlink(missing_ok=True)
                        else:
                            ps_only += 1
                        errors.append((clip, f"ffmpeg falló: {tail}"))
                else:
                    ps_only += 1
                    ok_count += 1
            except Exception as e:
                errors.append((clip, str(e)))

        self.after(0, lambda: self._on_extract_done(ok_count, mp4_ok, ps_only, errors, len(clips)))

    def _on_extract_done(self, ok: int, mp4_ok: int, ps_only: int, errors: list, total: int):
        self._var_prog.set(total)
        summary = f"Total: {total} | MP4 OK: {mp4_ok} | PS OK: {ps_only} | Errores: {len(errors)}"

        if errors:
            detail = "\n".join(f"• {clip_filename(c)}: {e}" for c, e in errors[:8])
            if len(errors) > 8:
                detail += f"\n… y {len(errors)-8} más"
            messagebox.showwarning("Extracción con errores", f"{summary}\n\n{detail}")
        else:
            messagebox.showinfo("Extracción completada", f"{summary}\n\nSalida:\n{self._var_out.get()}")
        self._set_status(summary)

    def _export_csv(self):
        visible = [self._clip_from_iid(iid) for iid in self._tree.get_children()]
        visible = [c for c in visible if c]
        if not visible:
            messagebox.showinfo("Sin datos", "No hay clips en la tabla.")
            return

        out = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("Todos", "*.*")],
            initialfile="clips.csv",
            title="Guardar CSV"
        )
        if not out:
            return

        fields = ["n", "rec_idx", "file_no", "mp4_name", "source_index", "dt_ini", "dt_fin", "duration_s", "off_start", "off_end", "size_bytes", "integrity"]
        try:
            with open(out, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                for n, c in enumerate(visible, 1):
                    w.writerow({
                        "n": n,
                        "rec_idx": c["rec_idx"],
                        "file_no": c["file_no"],
                        "mp4_name": c["mp4_name"],
                        "source_index": c.get("source_index", ""),
                        "dt_ini": fmt_dt(c["dt_ini"]),
                        "dt_fin": fmt_dt(c["dt_fin"]),
                        "duration_s": f"{c['duration']:.1f}" if c["duration"] else "",
                        "off_start": c["off_start"],
                        "off_end": c["off_end"],
                        "size_bytes": c["size"],
                        "integrity": c.get("integrity", ""),
                    })
            messagebox.showinfo("CSV exportado", f"{len(visible)} clips → {out}")
            self._set_status(f"CSV exportado: {len(visible)} clips.")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _show_gaps(self):
        if not self._clips:
            messagebox.showinfo("Sin datos", "Cargue un índice primero.")
            return
        gaps = detect_gaps(self._clips, threshold_s=60.0)

        win = tk.Toplevel(self)
        win.title("Gaps de grabación")
        win.geometry("560x420")

        tk.Label(win, text=f"{len(gaps)} períodos sin grabación (>1 min)", font=("", 10, "bold"), pady=8).pack()
        cols = ("inicio", "fin", "duracion")
        tree = ttk.Treeview(win, columns=cols, show="headings")
        tree.heading("inicio", text="Fin grabación anterior")
        tree.heading("fin", text="Inicio grabación siguiente")
        tree.heading("duracion", text="Sin grabar")
        tree.column("inicio", width=190)
        tree.column("fin", width=190)
        tree.column("duracion", width=110, anchor="e")

        vsb = ttk.Scrollbar(win, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(fill="both", expand=True, padx=8, side="left")
        vsb.pack(fill="y", side="right", padx=(0, 8))

        for g in gaps:
            tree.insert("", "end", values=(fmt_dt(g["dt_start"]), fmt_dt(g["dt_end"]), fmt_dur(g["duration_s"])))
        if not gaps:
            tk.Label(win, text="No se detectaron gaps > 1 min.").pack()

    def _probe_selected(self):
        clips = self._get_selected_clips()
        if not clips or self._sd_root is None:
            return
        clip = clips[0]
        tmp_dir = Path(tempfile.mkdtemp(prefix="sdhik_probe_"))
        raw_path = tmp_dir / clip_filename(clip, "ps")
        try:
            extract_clip(clip, self._sd_root, raw_path)
            ok, detail = probe_streams(raw_path)
            if ok:
                messagebox.showinfo("Probe stream", f"ffprobe detectó streams en:\n{raw_path.name}\n\n{detail}")
            else:
                messagebox.showwarning("Probe stream", f"No se detectaron streams válidos.\n\n{detail}")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _open_hash_window(self):
        try:
            initial_dir = Path(self._var_out.get())
        except Exception:
            initial_dir = Path.home()
        HashWindow(self, initial_dir)


if __name__ == "__main__":
    app = App()
    app.mainloop()
