#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sd-hik-reader  v11.0
====================
Fusión de v6.2 (funcionalidad completa) + v10.4 (parser corregido).

Parser OFNI corregido:
  · start = data.find(b'OFNI')          offset variable, no fijo
  · ts_s  = u[9]   (+36)               timestamp inicio real
  · ts_e  = u[11]  (+44)               timestamp fin real
  · off_s = u[17]  (+68)               offset global inicio
  · off_e = u[18]  (+72)               offset global fin
  · file_no = off_s // BLOCK           número de HIV desde offset global
  · Prefiere index00.bin (main stream) sobre index00p.bin (sub-stream)

UI:
  · Ventana principal 800×600
  · Modal de carga centrado con barra de progreso y porcentaje
  · Selector de índice con dropdown
  · Panel de detalle a la derecha
  · Hash window, probe stream, gaps, CSV export
"""

import os, csv, shutil, struct, hashlib, tempfile
import threading, platform, subprocess, datetime, tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from datetime import timezone
from typing import Optional

UTC   = timezone.utc
BLOCK = 268_435_456        # 256 MB por HIV
INDEX_REC  = 80
TS_MIN     = 1_500_000_000
TS_MAX     = 2_100_000_000
DUR_MAX    = 7_200
PACK_START = b"\x00\x00\x01\xba"

# ─────────────────────────────────────────────────────────────────
# PARSER
# ─────────────────────────────────────────────────────────────────

def _valid_ts(v: int) -> bool:
    return TS_MIN < v < TS_MAX


def discover_indices(sd_root: Path) -> list[Path]:
    """Devuelve índices ordenados priorizando index00.bin."""
    found = []
    for base in (sd_root / "index", sd_root):
        if base.exists():
            found.extend(sorted(base.glob("index*.bin")))
    seen, uniq = set(), []
    for p in found:
        rp = str(p.resolve())
        if rp not in seen:
            uniq.append(p); seen.add(rp)
    # Preferencia: index00.bin > index00p.bin > resto
    def _prio(p):
        n = p.name
        if n == "index00.bin":  return 0
        if n == "index00p.bin": return 1
        if n == "index01.bin":  return 2
        return 9
    return sorted(uniq, key=_prio)


def parse_index(index_path: Path) -> list[dict]:
    """
    Soporta dos formatos de índice HIK:

    Formato A — índice maestro (index00.bin, main stream):
      Registros de 80 bytes sin magic.
      u[2] (+8)  = ts_ini   u[0] (+0)  = ts_fin
      u[6] (+24) = off_s    u[7] (+28) = off_e
      Localización: ofni_pos - 36 (el OFNI está embebido en u[9])

    Formato B — OFNI directo (index00p.bin, sub-stream):
      Registros de 80 bytes con magic b'OFNI'.
      u[9] (+36) = ts_ini   u[11] (+44) = ts_fin
      u[17] (+68) = off_s   u[18] (+72) = off_e
    """
    data    = Path(index_path).read_bytes()
    ofni_pos = data.find(b'OFNI')
    if ofni_pos == -1:
        raise ValueError(f"No se encontró firma OFNI en {index_path.name}")

    clips = []

    # ── Detectar formato ──────────────────────────────────────────
    master_start = ofni_pos - 36
    use_master   = (master_start >= 0 and
                    master_start + INDEX_REC <= len(data) and
                    _valid_ts(struct.unpack_from("<I", data,
                                                master_start + 8)[0]))

    if use_master:
        # ── Formato A: índice maestro ─────────────────────────────
        # Retroceder para encontrar el primer registro válido
        start = master_start
        while start - INDEX_REC >= 0:
            u2 = struct.unpack_from("<I", data, start - INDEX_REC + 8)[0]
            if _valid_ts(u2):
                start -= INDEX_REC
            else:
                break

        pos = start
        consec = 0
        while pos + INDEX_REC <= len(data):
            u     = struct.unpack_from("<20I", data, pos)
            ts_s  = u[2];  ts_e  = u[0]
            off_s = u[6];  off_e = u[7]
            pos  += INDEX_REC

            if not _valid_ts(ts_s):
                consec += 1
                if consec >= 5: break
                continue
            consec = 0

            if off_e <= off_s: continue

            size    = off_e - off_s
            file_no = off_s // BLOCK
            local_s = off_s %  BLOCK
            local_e = local_s + size
            dur_idx = None; dt_fin = None
            if _valid_ts(ts_e) and 0 < (ts_e - ts_s) <= DUR_MAX:
                dur_idx = ts_e - ts_s
                dt_fin  = datetime.datetime.fromtimestamp(ts_e, tz=UTC)

            dt_ini  = datetime.datetime.fromtimestamp(ts_s, tz=UTC)
            dur_est = round(size / 350_000) if size > 0 else None
            if dur_est and dur_est < 1: dur_est = None

            clips.append({
                "rec_idx":      len(clips),
                "file_no":      file_no,
                "ts_ini":       ts_s,
                "ts_fin":       ts_e if dur_idx else None,
                "off_start":    local_s,
                "off_end":      local_e,
                "dt_ini":       dt_ini,
                "dt_fin":       dt_fin,
                "duration":     dur_idx or dur_est,
                "duration_idx": dur_idx,
                "mp4_name":     f"hiv{file_no:05d}.mp4",
                "size":         size,
                "scr_done":     False,
                "integrity":    "",
                "source_index": index_path.name,
            })

    else:
        # ── Formato B: OFNI directo ───────────────────────────────
        pos = ofni_pos
        while pos + INDEX_REC <= len(data):
            raw = data[pos:pos + INDEX_REC]
            if raw[:4] != b'OFNI': break
            u     = struct.unpack_from("<20I", raw)
            ts_s  = u[9];   ts_e  = u[11]
            off_s = u[17];  off_e = u[18]
            pos  += INDEX_REC

            if not (_valid_ts(ts_s) and off_e > off_s): continue

            size    = off_e - off_s
            file_no = off_s // BLOCK
            local_s = off_s %  BLOCK
            local_e = local_s + size
            dur_idx = None; dt_fin = None
            if _valid_ts(ts_e) and 0 < (ts_e - ts_s) <= DUR_MAX:
                dur_idx = ts_e - ts_s
                dt_fin  = datetime.datetime.fromtimestamp(ts_e, tz=UTC)

            dt_ini  = datetime.datetime.fromtimestamp(ts_s, tz=UTC)
            dur_est = round(size / 12_000) if size > 0 else None
            if dur_est and dur_est < 1: dur_est = None

            clips.append({
                "rec_idx":      len(clips),
                "file_no":      file_no,
                "ts_ini":       ts_s,
                "ts_fin":       ts_e if dur_idx else None,
                "off_start":    local_s,
                "off_end":      local_e,
                "dt_ini":       dt_ini,
                "dt_fin":       dt_fin,
                "duration":     dur_idx or dur_est,
                "duration_idx": dur_idx,
                "mp4_name":     f"hiv{file_no:05d}.mp4",
                "size":         size,
                "scr_done":     False,
                "integrity":    "",
                "source_index": index_path.name,
            })

    return clips

# ─────────────────────────────────────────────────────────────────
# HIV / STREAM helpers
# ─────────────────────────────────────────────────────────────────

def find_mp4(sd_root: Path, file_no: int) -> Optional[Path]:
    name = f"hiv{file_no:05d}.mp4"
    for p in (sd_root / "hiv" / name,
              sd_root / "record" / name,
              sd_root / name):
        if p.exists():
            return p
    return None


def _find_scrs(data: bytes) -> list[float]:
    scrs, pos = [], 0
    while True:
        idx = data.find(PACK_START, pos)
        if idx < 0 or idx + 10 > len(data):
            break
        b   = data[idx+4:idx+10]
        raw = (b[0]<<40)|(b[1]<<32)|(b[2]<<24)|(b[3]<<16)|(b[4]<<8)|b[5]
        scr  = ((raw>>43)&0x7)<<30
        scr |= ((raw>>27)&0x7FFF)<<15
        scr |= ((raw>>11)&0x7FFF)
        val  = scr / 90000.0
        if 0 <= val < 86400*7:
            scrs.append(val)
        pos = idx + 4
    return scrs


def scr_duration(src: Path, off_s: int, off_e: int) -> Optional[float]:
    size = off_e - off_s
    if size <= 0 or src is None:
        return None
    scan = min(262144, size)
    try:
        with open(src, "rb") as f:
            f.seek(off_s); head = f.read(scan)
            tail = head
            if size > scan * 2:
                f.seek(off_e - scan); tail = f.read(scan)
    except OSError:
        return None
    sh, st = _find_scrs(head), _find_scrs(tail)
    if not sh or not st:
        return None
    dur = max(st) - min(sh)
    return dur if 0 < dur <= DUR_MAX else None


def extract_clip(clip: dict, sd_root: Path, out_path: Path) -> int:
    src = find_mp4(sd_root, clip["file_no"])
    if src is None:
        raise FileNotFoundError(f"No se encontró {clip['mp4_name']}")
    size = clip["size"]
    if size <= 0:
        raise ValueError("Clip sin datos")
    CHUNK = 4 * 1024 * 1024
    written = 0
    with open(src, "rb") as s, open(out_path, "wb") as d:
        s.seek(clip["off_start"])
        rem = size
        while rem > 0:
            chunk = s.read(min(CHUNK, rem))
            if not chunk: break
            d.write(chunk); written += len(chunk); rem -= len(chunk)
    return written


def ffmpeg_ok()  -> bool: return bool(shutil.which("ffmpeg"))
def ffprobe_ok() -> bool: return bool(shutil.which("ffprobe"))


def convert_mp4(src: Path, dst: Path) -> subprocess.CompletedProcess:
    r = subprocess.run(
        ["ffmpeg","-y","-fflags","+genpts","-i",str(src),
         "-map","0:v:0?","-map","0:a:0?","-c","copy",
         "-movflags","+faststart",str(dst)],
        capture_output=True, text=True)
    if r.returncode == 0: return r
    return subprocess.run(
        ["ffmpeg","-y","-fflags","+genpts","-err_detect","ignore_err",
         "-i",str(src),"-map","0:v:0?","-map","0:a:0?",
         "-c:v","libx264","-preset","veryfast","-crf","23",
         "-c:a","aac","-b:a","128k","-movflags","+faststart",str(dst)],
        capture_output=True, text=True)


def probe_clip(src: Path) -> tuple[bool, str]:
    if not ffprobe_ok(): return False, "ffprobe no disponible"
    try:
        r = subprocess.run(
            ["ffprobe","-v","error","-show_entries",
             "stream=index,codec_type,codec_name",
             "-of","compact=p=0:nk=1",str(src)],
            capture_output=True, text=True)
        return r.returncode == 0, (r.stdout or r.stderr or "").strip()
    except Exception as e:
        return False, str(e)


def detect_gaps(clips: list[dict], thr: float = 60.0) -> list[dict]:
    wf = sorted([c for c in clips if c["dt_fin"]], key=lambda c: c["dt_ini"])
    gaps = []
    for i in range(1, len(wf)):
        g = (wf[i]["dt_ini"] - wf[i-1]["dt_fin"]).total_seconds()
        if g >= thr:
            gaps.append({"dt_start": wf[i-1]["dt_fin"],
                         "dt_end":   wf[i]["dt_ini"],
                         "duration_s": g})
    return gaps


def file_hashes(path: Path, algos=("sha256",)) -> dict:
    hs = {a: hashlib.new(a) for a in algos}
    with open(path,"rb") as f:
        while chunk := f.read(1024*1024):
            for h in hs.values(): h.update(chunk)
    return {n: h.hexdigest() for n,h in hs.items()}

# ─────────────────────────────────────────────────────────────────
# FORMATO
# ─────────────────────────────────────────────────────────────────

def fmt_dt(dt):
    return dt.strftime("%d-%m-%Y  %H:%M:%S") if dt else "—"

def fmt_dt_short(dt):
    return dt.strftime("%H:%M:%S") if dt else "—"

def fmt_size(n):
    if n >= 1_048_576: return f"{n/1_048_576:.1f} MB"
    if n >= 1024:      return f"{n/1024:.0f} KB"
    return f"{n} B"

def fmt_dur(s):
    if s is None or s == 0: return "—"
    s = int(round(s))
    if s >= 3600: return f"{s//3600}h {(s%3600)//60}m {s%60}s"
    if s >= 60:   return f"{(s%3600)//60}m {s%60}s"
    return f"{s}s"

def clip_name(clip: dict, ext="ps") -> str:
    dur = int(round(clip["duration"])) if clip["duration"] else 0
    return f"clip_{clip['dt_ini'].strftime('%Y%m%d_%H%M%S')}_{dur}s.{ext}"

def open_file(path: str):
    try:
        if   platform.system() == "Windows": os.startfile(path)
        elif platform.system() == "Darwin":  subprocess.Popen(["open", path])
        else:                                subprocess.Popen(["xdg-open", path])
    except Exception as e:
        messagebox.showerror("Error al abrir", str(e))

# ─────────────────────────────────────────────────────────────────
# MODAL DE CARGA
# ─────────────────────────────────────────────────────────────────

class LoadingModal(tk.Toplevel):
    """Ventana modal centrada con barra de progreso y porcentaje."""
    def __init__(self, master, title="Cargando…"):
        super().__init__(master)
        self.title("")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", lambda: None)  # no cerrable

        # Estilo
        self.configure(bg="#1e2128")
        pad = 28

        outer = tk.Frame(self, bg="#1e2128", padx=pad, pady=pad)
        outer.pack()

        tk.Label(outer, text=title, bg="#1e2128", fg="#e0e6f0",
                 font=("Consolas", 11, "bold")).pack(pady=(0, 14))

        self._var_msg = tk.StringVar(value="Iniciando…")
        tk.Label(outer, textvariable=self._var_msg,
                 bg="#1e2128", fg="#7a8a9a",
                 font=("Consolas", 9)).pack(pady=(0, 10))

        # Barra
        bar_frame = tk.Frame(outer, bg="#2a2f3a", bd=0)
        bar_frame.pack(fill="x", pady=(0, 8))

        self._canvas = tk.Canvas(bar_frame, height=12, width=320,
                                 bg="#2a2f3a", highlightthickness=0)
        self._canvas.pack()
        self._bar = self._canvas.create_rectangle(
            0, 0, 0, 12, fill="#00c8ff", outline="")

        # Porcentaje
        self._var_pct = tk.StringVar(value="0%")
        tk.Label(outer, textvariable=self._var_pct,
                 bg="#1e2128", fg="#00c8ff",
                 font=("Consolas", 13, "bold")).pack()

        self._center(master)

    def _center(self, master):
        self.update_idletasks()
        mw = master.winfo_width()  or 800
        mh = master.winfo_height() or 600
        mx = master.winfo_rootx()
        my = master.winfo_rooty()
        w  = self.winfo_reqwidth()
        h  = self.winfo_reqheight()
        self.geometry(f"+{mx + (mw-w)//2}+{my + (mh-h)//2}")

    def set(self, pct: float, msg: str = ""):
        pct = max(0.0, min(100.0, pct))
        fill = int(320 * pct / 100)
        self._canvas.coords(self._bar, 0, 0, fill, 12)
        self._var_pct.set(f"{int(pct)}%")
        if msg:
            self._var_msg.set(msg)
        self.update_idletasks()

    def close(self):
        self.grab_release()
        self.destroy()

# ─────────────────────────────────────────────────────────────────
# HASH WINDOW
# ─────────────────────────────────────────────────────────────────

class HashWindow(tk.Toplevel):
    def __init__(self, master, initial_dir: Path):
        super().__init__(master)
        self.title("Hashes / Integridad")
        self.geometry("920x500")
        self.minsize(760, 380)
        self.transient(master)

        self._results: list[dict] = []
        self._var_dir    = tk.StringVar(value=str(initial_dir))
        self._var_sha256 = tk.BooleanVar(value=True)
        self._var_md5    = tk.BooleanVar(value=False)
        self._var_status = tk.StringVar(value="Listo.")
        self._var_prog   = tk.DoubleVar(value=0)
        self._build()

    def _build(self):
        top = tk.Frame(self, padx=8, pady=6)
        top.pack(fill="x")
        tk.Label(top, text="Carpeta:").pack(side="left")
        tk.Entry(top, textvariable=self._var_dir, width=55).pack(
            side="left", padx=4, fill="x", expand=True)
        tk.Button(top, text="…",      command=self._browse).pack(side="left", padx=2)
        tk.Button(top, text="Calcular", command=self._run,
                  relief="groove", bg="#e0ffe0").pack(side="left", padx=4)
        tk.Button(top, text="CSV",    command=self._csv,
                  relief="groove").pack(side="left", padx=2)

        opts = tk.Frame(self, padx=8, pady=2)
        opts.pack(fill="x")
        tk.Checkbutton(opts, text="SHA-256", variable=self._var_sha256).pack(side="left")
        tk.Checkbutton(opts, text="MD5",     variable=self._var_md5).pack(side="left", padx=8)

        mid = tk.Frame(self, padx=8, pady=4)
        mid.pack(fill="both", expand=True)
        cols = ("archivo","ext","tamano","sha256","md5")
        self._tree = ttk.Treeview(mid, columns=cols, show="headings")
        for col,hdr,w,anc in [("archivo","Archivo",240,"w"),("ext","Ext",50,"c"),
                               ("tamano","Tamaño",90,"e"),("sha256","SHA-256",320,"w"),
                               ("md5","MD5",220,"w")]:
            self._tree.heading(col, text=hdr, anchor=anc)
            self._tree.column(col, width=w, anchor=anc,
                              stretch=(col in {"archivo","sha256","md5"}))
        vsb = ttk.Scrollbar(mid, orient="vertical",   command=self._tree.yview)
        hsb = ttk.Scrollbar(mid, orient="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._tree.grid(row=0,column=0,sticky="nsew")
        vsb.grid(row=0,column=1,sticky="ns")
        hsb.grid(row=1,column=0,sticky="ew")
        mid.rowconfigure(0,weight=1); mid.columnconfigure(0,weight=1)

        bot = tk.Frame(self, padx=8, pady=4)
        bot.pack(fill="x", side="bottom")
        tk.Label(bot, textvariable=self._var_status, fg="gray",
                 anchor="w").pack(side="left")
        ttk.Progressbar(bot, variable=self._var_prog,
                        maximum=100, length=160).pack(side="right")

    def _browse(self):
        d = filedialog.askdirectory(parent=self)
        if d: self._var_dir.set(d)

    def _run(self):
        algos = [a for a,v in [("sha256",self._var_sha256),
                                ("md5",self._var_md5)] if v.get()]
        if not algos:
            messagebox.showwarning("Sin algoritmo",
                "Seleccione al menos uno.", parent=self); return
        root = Path(self._var_dir.get().strip())
        if not root.is_dir():
            messagebox.showerror("Error","Carpeta no válida.",parent=self); return
        files = sorted(p for p in root.iterdir()
                       if p.is_file() and p.suffix.lower() in {".mp4",".ps"})
        if not files:
            messagebox.showinfo("Sin archivos",
                "No se encontraron .mp4 ni .ps.", parent=self); return
        self._tree.delete(*self._tree.get_children())
        self._results = []
        self._var_prog.set(0)
        threading.Thread(target=self._worker,
                         args=(files, tuple(algos)), daemon=True).start()

    def _worker(self, files, algos):
        for i, path in enumerate(files, 1):
            hs  = file_hashes(path, algos)
            row = {"archivo": path.name, "ext": path.suffix.lower(),
                   "size_bytes": path.stat().st_size,
                   "sha256": hs.get("sha256",""), "md5": hs.get("md5",""),
                   "full_path": str(path)}
            def _ui(row=row, i=i):
                self._results.append(row)
                self._tree.insert("","end", values=(
                    row["archivo"], row["ext"],
                    fmt_size(row["size_bytes"]),
                    row["sha256"], row["md5"]))
                self._var_prog.set(i * 100 / len(files))
                self._var_status.set(f"[{i}/{len(files)}] {row['archivo']}")
            self.after(0, _ui)
        self.after(0, lambda: self._var_status.set(
            f"Listo. {len(files)} archivos."))

    def _csv(self):
        if not self._results:
            messagebox.showinfo("Sin datos","Calcule los hashes primero.",
                                parent=self); return
        out = filedialog.asksaveasfilename(parent=self,
            defaultextension=".csv",
            filetypes=[("CSV","*.csv")], initialfile="hashes.csv")
        if not out: return
        fields = ["archivo","ext","size_bytes","sha256","md5","full_path"]
        with open(out,"w",newline="",encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader(); w.writerows(self._results)
        messagebox.showinfo("CSV",f"Exportado → {out}", parent=self)

# ─────────────────────────────────────────────────────────────────
# APP PRINCIPAL
# ─────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("sd-hik-reader  v11.0")
        self.geometry("1024x600")
        self.minsize(700, 400)

        self._clips:    list[dict]      = []
        self._iid_map:  dict[str,dict]  = {}
        self._sd_root:  Optional[Path]  = None
        self._indices:  list[Path]      = []
        self._sort_col  = "inicio"
        self._sort_rev  = False
        self._load_id   = 0

        self._build_ui()

    # ══════════════════════════════════════════════════════════════
    # UI
    # ══════════════════════════════════════════════════════════════

    def _build_ui(self):
        # ── Fila 1: SD + índice ───────────────────────────────────
        f1 = tk.Frame(self, pady=5, padx=8)
        f1.pack(fill="x")

        tk.Label(f1, text="SD:").pack(side="left")
        self._var_sd = tk.StringVar()
        tk.Entry(f1, textvariable=self._var_sd, width=36).pack(
            side="left", padx=(3,2))
        tk.Button(f1, text="Abrir…", command=self._browse_sd,
                  relief="groove").pack(side="left", padx=2)

        tk.Label(f1, text="Índice:").pack(side="left", padx=(10,3))
        self._var_idx = tk.StringVar()
        self._cmb_idx = ttk.Combobox(f1, textvariable=self._var_idx,
                                     state="readonly", width=16)
        self._cmb_idx.pack(side="left")
        tk.Button(f1, text="⟳", width=2,
                  command=self._scan_indices,
                  relief="groove").pack(side="left", padx=2)
        tk.Button(f1, text="Cargar", command=self._load,
                  relief="groove", bg="#e0ffe0").pack(side="left", padx=6)

        self._lbl_info = tk.Label(f1, text="", fg="gray", font=("",8))
        self._lbl_info.pack(side="left", padx=4)

        # ── Fila 2: filtros ───────────────────────────────────────
        f2 = tk.Frame(self, pady=3, padx=8)
        f2.pack(fill="x")

        MESES = ["---","Ene","Feb","Mar","Abr","May","Jun",
                 "Jul","Ago","Sep","Oct","Nov","Dic"]
        DIAS  = ["--"] + [f"{d:02d}" for d in range(1,32)]
        ANIOS = ["----"] + [str(a) for a in range(2000,2051)]
        HORAS = ["--"] + [f"{h:02d}" for h in range(24)]
        MINS  = ["--"] + [f"{m:02d}" for m in range(60)]

        def dc(p):
            dd = ttk.Combobox(p, values=DIAS,  width=3, state="readonly")
            mm = ttk.Combobox(p, values=MESES, width=4, state="readonly")
            yy = ttk.Combobox(p, values=ANIOS, width=6, state="readonly")
            dd.set("--"); mm.set("---"); yy.set("----")
            return dd, mm, yy

        def tc(p):
            hh = ttk.Combobox(p, values=HORAS, width=3, state="readonly")
            mi = ttk.Combobox(p, values=MINS,  width=3, state="readonly")
            hh.set("--"); mi.set("--")
            return hh, mi

        tk.Label(f2, text="Inicio:").pack(side="left")
        self._fd_dd,self._fd_mm,self._fd_yy = dc(f2)
        self._fd_dd.pack(side="left",padx=(3,1))
        self._fd_mm.pack(side="left",padx=1)
        self._fd_yy.pack(side="left",padx=(1,3))
        self._fd_hh,self._fd_mi = tc(f2)
        self._fd_hh.pack(side="left",padx=1)
        tk.Label(f2,text=":",fg="gray").pack(side="left")
        self._fd_mi.pack(side="left",padx=(0,8))

        tk.Label(f2, text="Fin:").pack(side="left")
        self._fh_dd,self._fh_mm,self._fh_yy = dc(f2)
        self._fh_dd.pack(side="left",padx=(3,1))
        self._fh_mm.pack(side="left",padx=1)
        self._fh_yy.pack(side="left",padx=(1,3))
        self._fh_hh,self._fh_mi = tc(f2)
        self._fh_hh.pack(side="left",padx=1)
        tk.Label(f2,text=":",fg="gray").pack(side="left")
        self._fh_mi.pack(side="left",padx=(0,6))

        tk.Button(f2, text="Filtrar", command=self._apply_filter,
                  relief="groove").pack(side="left",padx=2)
        tk.Button(f2, text="Limpiar", command=self._clear_filter,
                  relief="groove").pack(side="left",padx=2)
        self._lbl_filter = tk.Label(f2, text="", fg="gray", font=("",8))
        self._lbl_filter.pack(side="left", padx=6)

        ttk.Separator(self, orient="horizontal").pack(fill="x", padx=8)

        # ── Status bar ────────────────────────────────────────────
        sbar = tk.Frame(self, pady=2, padx=8, relief="sunken", bd=1)
        sbar.pack(fill="x", side="bottom")
        self._var_status = tk.StringVar(value="Listo.")
        tk.Label(sbar, textvariable=self._var_status, anchor="w",
                 fg="gray", font=("",8)).pack(side="left")
        self._var_sel = tk.StringVar(value="")
        tk.Label(sbar, textvariable=self._var_sel, anchor="e",
                 fg="gray", font=("",8)).pack(side="right", padx=6)

        # ── Cuerpo ────────────────────────────────────────────────
        body = tk.PanedWindow(self, orient="horizontal",
                              sashwidth=4, sashrelief="raised")
        body.pack(fill="both", expand=True, padx=8, pady=4)

        # ── Tabla ─────────────────────────────────────────────────
        left = tk.Frame(body)
        body.add(left, minsize=490)

        cols = ("n","inicio","fin","duracion","tamano","archivo","integ")
        self._tree = ttk.Treeview(left, columns=cols,
                                  show="headings", selectmode="extended")
        for col,hdr,w,anc in [
            ("n",       "#",       40, "e"),
            ("inicio",  "Inicio", 150, "w"),
            ("fin",     "Fin",     72, "w"),
            ("duracion","Duración",68, "e"),
            ("tamano",  "Tamaño",  68, "e"),
            ("archivo", "Archivo", 96, "w"),
            ("integ",   "✓",       26, "c"),
        ]:
            self._tree.heading(col, text=hdr, anchor=anc,
                               command=lambda c=col: self._sort_by(c))
            self._tree.column(col, width=w, anchor=anc,
                              stretch=(col=="inicio"), minwidth=22)

        self._tree.tag_configure("no_hiv", background="#fff0f0")
        self._tree.tag_configure("warn",   foreground="#888888")

        vsb = ttk.Scrollbar(left, orient="vertical",   command=self._tree.yview)
        hsb = ttk.Scrollbar(left, orient="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._tree.grid(row=0,column=0,sticky="nsew")
        vsb.grid(row=0,column=1,sticky="ns")
        hsb.grid(row=1,column=0,sticky="ew")
        left.rowconfigure(0,weight=1); left.columnconfigure(0,weight=1)

        self._tree.bind("<<TreeviewSelect>>", self._on_select)
        self._tree.bind("<Double-1>",          self._on_dbl)
        self._tree.bind("<Control-a>",         lambda e: (
            self._tree.selection_set(self._tree.get_children()), "break"))

        # ── Panel derecho con scroll ──────────────────────────────
        right_outer = tk.Frame(body)
        body.add(right_outer, minsize=200)

        right_canvas = tk.Canvas(right_outer, bd=0, highlightthickness=0)
        right_vsb    = ttk.Scrollbar(right_outer, orient="vertical",
                                     command=right_canvas.yview)
        right_canvas.configure(yscrollcommand=right_vsb.set)
        right_vsb.pack(side="right", fill="y")
        right_canvas.pack(side="left", fill="both", expand=True)

        right = tk.Frame(right_canvas, padx=8, pady=4)
        _rw   = right_canvas.create_window((0, 0), window=right, anchor="nw")

        def _cfg_scroll(e):
            right_canvas.configure(scrollregion=right_canvas.bbox("all"))
        def _cfg_width(e):
            right_canvas.itemconfig(_rw, width=e.width)
        right.bind("<Configure>",        _cfg_scroll)
        right_canvas.bind("<Configure>", _cfg_width)

        def _on_mousewheel(e):
            right_canvas.yview_scroll(int(-1*(e.delta/120)), "units")
        def _on_mousewheel_lx(e):
            right_canvas.yview_scroll(-1 if e.num==4 else 1, "units")
        for _w in (right_canvas, right):
            _w.bind("<MouseWheel>", _on_mousewheel)
            _w.bind("<Button-4>",   _on_mousewheel_lx)
            _w.bind("<Button-5>",   _on_mousewheel_lx)

        tk.Label(right, text="Clip seleccionado",
                 font=("",9,"bold"), anchor="w").pack(fill="x")
        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=4)

        self._info = {}
        for lbl, key in [("Inicio","ini"),("Fin","fin"),("Duración","dur"),
                         ("Archivo","arch"),("Offset","off"),
                         ("Tamaño","tam"),("Integridad","integ"),
                         ("Índice","idx")]:
            row = tk.Frame(right)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=f"{lbl}:", width=10, anchor="e",
                     fg="gray").pack(side="left")
            lv = tk.Label(row, text="—", anchor="w",
                          font=("Consolas",8), wraplength=148,
                          justify="left")
            lv.pack(side="left", fill="x", expand=True)
            self._info[key] = lv

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=6)

        tk.Label(right, text="Salida:", anchor="w",
                 font=("",8)).pack(fill="x")
        out_row = tk.Frame(right)
        out_row.pack(fill="x", pady=(2,4))
        self._var_out = tk.StringVar(value=str(Path.home() / "Videos"))
        try: (Path.home()/"Videos").mkdir(parents=True, exist_ok=True)
        except: pass
        tk.Entry(out_row, textvariable=self._var_out,
                 font=("",8)).pack(side="left", fill="x", expand=True)
        tk.Button(out_row, text="…", width=3,
                  command=self._browse_out).pack(side="left", padx=(2,0))

        self._var_conv  = tk.BooleanVar(value=False)
        self._var_keepps = tk.BooleanVar(value=True)
        chk = tk.Checkbutton(right, text="Convertir a .mp4 (ffmpeg)",
                             variable=self._var_conv, font=("",8))
        chk.pack(anchor="w")
        if not ffmpeg_ok():
            chk.configure(state="disabled")
            tk.Label(right, text="  ffmpeg no encontrado",
                     fg="gray", font=("",7)).pack(anchor="w")
        tk.Checkbutton(right, text="Conservar .ps si falla ffmpeg",
                       variable=self._var_keepps,
                       font=("",8)).pack(anchor="w", pady=(0,4))

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=4)

        btns = [
            ("▶  Ver clip",              self._ver,         None),
            ("⬇  Extraer seleccionados", self._ext_sel,     "#ddeeff"),
            ("⬇  Extraer visibles",      self._ext_all,     None),
        ]
        for txt,cmd,bg in btns:
            kw = {"bg":bg} if bg else {}
            tk.Button(right, text=txt, command=cmd,
                      relief="groove", font=("",8), **kw).pack(fill="x", pady=1)

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=4)
        tk.Label(right, text="Herramientas", font=("",8,"bold"),
                 anchor="w").pack(fill="x")

        tools = [
            ("📋  Exportar CSV",            self._csv),
            ("⏱  Gaps de grabación",        self._gaps),
            ("🔎  Probar stream",            self._probe),
            ("🔐  Hashes / Integridad",      self._hashes),
        ]
        for txt,cmd in tools:
            tk.Button(right, text=txt, command=cmd,
                      relief="groove", font=("",8)).pack(fill="x", pady=1)

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=4)
        tk.Button(right, text="Cerrar", command=self.destroy,
                  relief="groove", fg="gray",
                  font=("",8)).pack(fill="x")

    # ══════════════════════════════════════════════════════════════
    # HELPERS
    # ══════════════════════════════════════════════════════════════

    def _iid(self, c): return str(c["rec_idx"])
    def _by_iid(self, i): return self._iid_map.get(i)
    def _status(self, m): self._var_status.set(m)

    def _combo_dt(self, dd_w, mm_w, yy_w, hh_w=None, mi_w=None):
        dd,mm,yy = dd_w.get(),mm_w.get(),yy_w.get()
        if dd=="--" or mm=="---" or yy=="----": return None
        M={"Ene":1,"Feb":2,"Mar":3,"Abr":4,"May":5,"Jun":6,
           "Jul":7,"Ago":8,"Sep":9,"Oct":10,"Nov":11,"Dic":12}
        try:
            hh = int(hh_w.get()) if hh_w and hh_w.get()!="--" else 0
            mi = int(mi_w.get()) if mi_w and mi_w.get()!="--" else 0
            return datetime.datetime(int(yy),M[mm],int(dd),hh,mi,tzinfo=UTC)
        except (ValueError,KeyError):
            return None

    def _browse_sd(self):
        d = filedialog.askdirectory(title="Raíz de la SD")
        if d:
            self._var_sd.set(d); self._scan_indices()

    def _browse_out(self):
        d = filedialog.askdirectory(title="Carpeta de salida")
        if d: self._var_out.set(d)

    # ══════════════════════════════════════════════════════════════
    # ESCANEAR + CARGAR
    # ══════════════════════════════════════════════════════════════

    def _scan_indices(self):
        sd_s = self._var_sd.get().strip()
        if not sd_s: return
        sd = Path(sd_s)
        if not sd.exists():
            messagebox.showerror("Error","La carpeta no existe."); return
        self._indices = discover_indices(sd)
        names = [p.name for p in self._indices]
        self._cmb_idx["values"] = names
        if names:
            self._var_idx.set(names[0])
            self._lbl_info.configure(
                text=f"{len(names)} índice(s)", fg="darkgreen")
        else:
            self._var_idx.set("")
            self._lbl_info.configure(text="Sin índices", fg="firebrick")

    def _load(self):
        sd_s = self._var_sd.get().strip()
        if not sd_s:
            messagebox.showwarning("Sin carpeta",
                "Seleccione la carpeta de la SD."); return
        if not self._indices:
            self._scan_indices()
        idx_name = self._var_idx.get().strip()
        if not idx_name:
            messagebox.showerror("Sin índice",
                "No se encontraron índices en la carpeta."); return
        idx_path = next((p for p in self._indices if p.name==idx_name), None)
        if idx_path is None:
            messagebox.showerror("Error","Índice no disponible."); return

        self._sd_root = Path(sd_s)
        self._load_id += 1
        lid = self._load_id

        # Modal de carga
        self._modal = LoadingModal(self, "Cargando índice…")
        self._modal.set(0, f"Leyendo {idx_path.name}…")

        threading.Thread(target=self._load_worker,
                         args=(idx_path, lid), daemon=True).start()

    def _load_worker(self, idx_path: Path, lid: int):
        try:
            clips = parse_index(idx_path)
            self.after(0, lambda: self._on_loaded(clips, idx_path, lid))
        except Exception as e:
            self.after(0, lambda: self._on_load_err(str(e)))

    def _on_loaded(self, clips, idx_path, lid):
        if lid != self._load_id: return
        if hasattr(self,"_modal"):
            self._modal.set(100,"Procesando…")

        self._clips   = clips
        self._iid_map = {self._iid(c): c for c in clips}
        self._populate_tree(clips)
        self._fill_combos(clips)

        n_mp4 = len({c["file_no"] for c in clips})
        self._lbl_info.configure(
            text=f"{idx_path.name} | {len(clips)} clips | {n_mp4} HIV",
            fg="darkgreen")
        self._status(f"Listo: {len(clips)} clips. Calculando duraciones SCR…")

        if hasattr(self,"_modal"):
            self._modal.close()

        if self._sd_root:
            threading.Thread(target=self._scr_worker,
                             args=(clips, lid), daemon=True).start()

    def _on_load_err(self, msg):
        if hasattr(self,"_modal"): self._modal.close()
        messagebox.showerror("Error al cargar", msg)
        self._status(f"Error: {msg}")

    def _fill_combos(self, clips):
        if not clips: return
        N=["---","Ene","Feb","Mar","Abr","May","Jun",
           "Jul","Ago","Sep","Oct","Nov","Dic"]
        for dd,mm,yy,dt in [
            (self._fd_dd,self._fd_mm,self._fd_yy, min(c["dt_ini"] for c in clips)),
            (self._fh_dd,self._fh_mm,self._fh_yy, max(c["dt_ini"] for c in clips)),
        ]:
            dd.set(f"{dt.day:02d}"); mm.set(N[dt.month]); yy.set(str(dt.year))

    # ══════════════════════════════════════════════════════════════
    # SCR worker (duraciones en background)
    # ══════════════════════════════════════════════════════════════

    def _scr_worker(self, clips, lid):
        total   = len(clips)
        done    = 0
        total_s = 0.0
        n_no    = 0

        for c in clips:
            if lid != self._load_id: return
            mp4 = find_mp4(self._sd_root, c["file_no"]) if self._sd_root else None
            if mp4 is None:
                c["scr_done"] = True; c["integrity"] = "?"
                n_no += 1; done += 1; continue

            dur = scr_duration(mp4, c["off_start"], c["off_end"])
            c["scr_done"] = True

            if dur and dur > 0:
                c["duration"] = dur
                c["dt_fin"]   = c["dt_ini"] + datetime.timedelta(seconds=dur)
                total_s      += dur
                est = c["size"] / (3_440_000/8) if c["size"] > 0 else 0
                c["integrity"] = "✓" if abs(dur-est)/max(dur,1) < 0.5 else "!"
            else:
                c["integrity"] = "?"

            iid   = self._iid(c)
            fin_s = fmt_dt_short(c["dt_fin"])
            dur_s = fmt_dur(c["duration"])
            integ = c["integrity"]

            def _upd(iid=iid, fin_s=fin_s, dur_s=dur_s, integ=integ):
                if self._tree.exists(iid):
                    self._tree.set(iid,"fin",     fin_s)
                    self._tree.set(iid,"duracion", dur_s)
                    self._tree.set(iid,"integ",    integ)
            self.after(0, _upd)

            done += 1
            if done % 15 == 0 or done == total:
                pct = int(done*100/total)
                msg = f"SCR: {done}/{total} | {total_s/60:.1f} min"
                self.after(0, lambda p=pct, m=msg: self._status(m))

        def _fin():
            n = len(clips)
            n_mp4 = len({c["file_no"] for c in clips})
            extra = f" | ⚠ {n_no} sin HIV" if n_no else ""
            self._lbl_info.configure(
                text=f"{self._var_idx.get()} | {n} clips | "
                     f"{n_mp4} HIV | {total_s/60:.1f} min{extra}",
                fg="darkgreen")
            self._status(f"Listo. {n} clips | {total_s/60:.1f} min grabados.")
        self.after(0, _fin)

    # ══════════════════════════════════════════════════════════════
    # TABLA
    # ══════════════════════════════════════════════════════════════

    def _populate_tree(self, clips):
        self._tree.delete(*self._tree.get_children())
        try:
            clips = sorted(clips, key=lambda c: c["dt_ini"],
                           reverse=self._sort_rev)
        except Exception: pass

        for n, c in enumerate(clips, 1):
            mp4 = find_mp4(self._sd_root, c["file_no"]) \
                  if self._sd_root else None
            tag = "no_hiv" if mp4 is None else ""
            arch = c["mp4_name"]
            if mp4 is None: arch = "⚠ " + arch
            self._tree.insert("","end", iid=self._iid(c),
                              tags=(tag,) if tag else (), values=(
                n,
                fmt_dt(c["dt_ini"]),
                fmt_dt_short(c["dt_fin"]),
                fmt_dur(c["duration"]),
                fmt_size(c["size"]),
                arch,
                c.get("integrity",""),
            ))

    def _sort_by(self, col):
        if self._sort_col == col: self._sort_rev = not self._sort_rev
        else: self._sort_col = col; self._sort_rev = False

        shown = [self._by_iid(i)
                 for i in self._tree.get_children()]
        shown = [c for c in shown if c]
        kf = {"inicio":   lambda c: c["dt_ini"],
              "fin":      lambda c: c["dt_fin"] or c["dt_ini"],
              "duracion": lambda c: c["duration"] or 0,
              "tamano":   lambda c: c["size"],
              "archivo":  lambda c: c["mp4_name"],
              "n":        lambda c: c["rec_idx"],
              "integ":    lambda c: c.get("integrity",""),
              }.get(col, lambda c: c["dt_ini"])
        shown.sort(key=kf, reverse=self._sort_rev)

        self._tree.delete(*self._tree.get_children())
        for n, c in enumerate(shown, 1):
            mp4 = find_mp4(self._sd_root, c["file_no"]) \
                  if self._sd_root else None
            tag  = "no_hiv" if mp4 is None else ""
            arch = c["mp4_name"]
            if mp4 is None: arch = "⚠ " + arch
            self._tree.insert("","end", iid=self._iid(c),
                              tags=(tag,) if tag else (), values=(
                n, fmt_dt(c["dt_ini"]), fmt_dt_short(c["dt_fin"]),
                fmt_dur(c["duration"]), fmt_size(c["size"]),
                arch, c.get("integrity",""),
            ))
        arrow = " ▲" if not self._sort_rev else " ▼"
        for cid in ("n","inicio","fin","duracion","tamano","archivo","integ"):
            txt = self._tree.heading(cid,"text").replace(" ▲","").replace(" ▼","")
            if cid == col: txt += arrow
            self._tree.heading(cid, text=txt)

    # ══════════════════════════════════════════════════════════════
    # SELECCIÓN
    # ══════════════════════════════════════════════════════════════

    def _on_select(self, _=None):
        sel = self._tree.selection()
        n   = len(sel)
        self._var_sel.set(f"{n} sel." if n else "")
        if not sel: return
        c = self._by_iid(sel[-1])
        if not c: return
        self._info["ini"].configure(text=fmt_dt(c["dt_ini"]))
        self._info["fin"].configure(text=fmt_dt(c["dt_fin"]))
        self._info["dur"].configure(text=fmt_dur(c["duration"]))
        self._info["arch"].configure(text=c["mp4_name"])
        self._info["off"].configure(
            text=f"{c['off_start']:,} → {c['off_end']:,}")
        self._info["tam"].configure(text=fmt_size(c["size"]))
        self._info["integ"].configure(text=c.get("integrity","—"))
        self._info["idx"].configure(text=c.get("source_index","—"))

        if self._sd_root and not c.get("scr_done"):
            threading.Thread(target=self._scr_one,
                             args=(c,), daemon=True).start()

    def _scr_one(self, c):
        mp4 = find_mp4(self._sd_root, c["file_no"]) if self._sd_root else None
        dur = scr_duration(mp4, c["off_start"], c["off_end"]) if mp4 else None
        c["scr_done"] = True
        iid = self._iid(c)
        if dur and dur > 0:
            c["duration"] = dur
            c["dt_fin"]   = c["dt_ini"] + datetime.timedelta(seconds=dur)
            est = c["size"] / (3_440_000/8) if c["size"] > 0 else 0
            c["integrity"] = "✓" if abs(dur-est)/max(dur,1) < 0.5 else "!"
            def _a():
                self._info["fin"].configure(text=fmt_dt(c["dt_fin"]))
                self._info["dur"].configure(text=fmt_dur(dur))
                self._info["integ"].configure(text=c["integrity"])
                if self._tree.exists(iid):
                    self._tree.set(iid,"fin",     fmt_dt_short(c["dt_fin"]))
                    self._tree.set(iid,"duracion", fmt_dur(dur))
                    self._tree.set(iid,"integ",    c["integrity"])
            self.after(0, _a)

    def _on_dbl(self, _=None): self._ver()

    # ══════════════════════════════════════════════════════════════
    # FILTROS
    # ══════════════════════════════════════════════════════════════

    def _apply_filter(self):
        if not self._clips:
            messagebox.showinfo("Sin datos","Cargue un índice primero."); return
        d1 = self._combo_dt(self._fd_dd,self._fd_mm,self._fd_yy,
                            self._fd_hh,self._fd_mi)
        d2 = self._combo_dt(self._fh_dd,self._fh_mm,self._fh_yy,
                            self._fh_hh,self._fh_mi)
        if d2 and self._fh_hh.get()=="--":
            d2 = d2.replace(hour=23,minute=59,second=59)
        r = self._clips
        if d1: r = [c for c in r if c["dt_ini"] >= d1]
        if d2: r = [c for c in r if c["dt_ini"] <= d2]
        self._populate_tree(r)
        self._lbl_filter.configure(
            text=f"{len(r)}/{len(self._clips)}")
        self._status(f"Filtro: {len(r)} clips.")

    def _clear_filter(self):
        for w in (self._fd_dd,self._fh_dd): w.set("--")
        for w in (self._fd_mm,self._fh_mm): w.set("---")
        for w in (self._fd_yy,self._fh_yy): w.set("----")
        for w in (self._fd_hh,self._fh_hh): w.set("--")
        for w in (self._fd_mi,self._fh_mi): w.set("--")
        self._populate_tree(self._clips)
        self._lbl_filter.configure(text="")
        self._status(f"{len(self._clips)} clips.")

    # ══════════════════════════════════════════════════════════════
    # VER / EXTRAER
    # ══════════════════════════════════════════════════════════════

    def _sel_clips(self) -> list[dict]:
        sel = self._tree.selection()
        if not sel:
            messagebox.showinfo("Sin selección",
                "Seleccione uno o más clips."); return []
        return [c for i in sel if (c:=self._by_iid(i)) is not None]

    def _ver(self):
        clips = self._sel_clips()
        if not clips or not self._sd_root: return
        c = clips[0]
        tmp = Path(tempfile.mkdtemp(prefix="sdhik_"))
        ps  = tmp / clip_name(c,"ps")
        self._status("Extrayendo…")
        try:
            extract_clip(c, self._sd_root, ps)
            if self._var_conv.get() and ffmpeg_ok():
                mp4 = tmp / clip_name(c,"mp4")
                res = convert_mp4(ps, mp4)
                if res.returncode == 0:
                    ps.unlink(missing_ok=True)
                    open_file(str(mp4))
                    self._status(f"Abriendo {mp4.name}")
                    return
            open_file(str(ps))
            self._status(f"Abriendo {ps.name}")
        except Exception as e:
            messagebox.showerror("Error",str(e))
            self._status(f"Error: {e}")

    def _ext_sel(self):
        clips = self._sel_clips()
        if clips: self._run_extract(clips)

    def _ext_all(self):
        if not self._clips:
            messagebox.showinfo("Sin clips","Cargue un índice primero."); return
        vis = [self._by_iid(i) for i in self._tree.get_children()]
        vis = [c for c in vis if c]
        if len(vis) > 50:
            if not messagebox.askyesno("Extracción masiva",
                f"Se extraerán {len(vis)} clips.\n¿Continuar?"): return
        self._run_extract(vis)

    def _run_extract(self, clips):
        if not self._sd_root:
            messagebox.showwarning("Sin SD","Seleccione la SD."); return
        out = Path(self._var_out.get())
        out.mkdir(parents=True, exist_ok=True)
        conv    = self._var_conv.get() and ffmpeg_ok()
        keepps  = self._var_keepps.get()
        self._status(f"Extrayendo {len(clips)} clips…")
        threading.Thread(target=self._ext_worker,
                         args=(clips, out, conv, keepps), daemon=True).start()

    def _ext_worker(self, clips, out_dir, conv, keepps):
        ok = mp4_ok = ps_ok = 0
        errors = []
        for i, c in enumerate(clips):
            ps = out_dir / clip_name(c,"ps")
            self.after(0, lambda i=i, n=ps.name:
                self._status(f"[{i+1}/{len(clips)}] {n}"))
            try:
                extract_clip(c, self._sd_root, ps)
                if conv:
                    mp4 = out_dir / clip_name(c,"mp4")
                    res = convert_mp4(ps, mp4)
                    if res.returncode == 0:
                        ps.unlink(missing_ok=True); mp4_ok += 1; ok += 1
                    else:
                        if not keepps: ps.unlink(missing_ok=True)
                        else: ps_ok += 1
                        errors.append((c, f"ffmpeg: {(res.stderr or '')[-200:]}"))
                else:
                    ps_ok += 1; ok += 1
            except Exception as e:
                errors.append((c, str(e)))

        def _done():
            s = f"MP4:{mp4_ok} PS:{ps_ok} Err:{len(errors)}"
            if errors:
                det = "\n".join(f"• {clip_name(c)}: {e}"
                                for c,e in errors[:6])
                messagebox.showwarning("Completado con errores",
                    f"{s}\n\n{det}")
            else:
                messagebox.showinfo("Completado",
                    f"{s}\n\n{out_dir}")
            self._status(s)
        self.after(0, _done)

    # ══════════════════════════════════════════════════════════════
    # HERRAMIENTAS
    # ══════════════════════════════════════════════════════════════

    def _csv(self):
        vis = [self._by_iid(i) for i in self._tree.get_children()]
        vis = [c for c in vis if c]
        if not vis:
            messagebox.showinfo("Sin datos","No hay clips."); return
        out = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV","*.csv")], initialfile="clips.csv")
        if not out: return
        fields=["n","rec_idx","file_no","mp4_name","source_index",
                "dt_ini","dt_fin","duration_s","off_start","off_end",
                "size_bytes","integrity"]
        with open(out,"w",newline="",encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for n,c in enumerate(vis,1):
                w.writerow({"n":n,"rec_idx":c["rec_idx"],
                    "file_no":c["file_no"],"mp4_name":c["mp4_name"],
                    "source_index":c.get("source_index",""),
                    "dt_ini":fmt_dt(c["dt_ini"]),
                    "dt_fin":fmt_dt(c["dt_fin"]),
                    "duration_s":f"{c['duration']:.1f}" if c["duration"] else "",
                    "off_start":c["off_start"],"off_end":c["off_end"],
                    "size_bytes":c["size"],"integrity":c.get("integrity","")})
        messagebox.showinfo("CSV",f"{len(vis)} clips → {out}")
        self._status(f"CSV: {len(vis)} clips.")

    def _gaps(self):
        if not self._clips:
            messagebox.showinfo("Sin datos","Cargue un índice."); return
        gaps = detect_gaps(self._clips)
        win  = tk.Toplevel(self)
        win.title("Gaps de grabación")
        win.geometry("540x380")
        tk.Label(win, text=f"{len(gaps)} períodos sin grabación (>1 min)",
                 font=("",9,"bold"), pady=6).pack()
        cols=("ini","fin","dur")
        t=ttk.Treeview(win,columns=cols,show="headings")
        t.heading("ini",text="Fin anterior"); t.column("ini",width=185)
        t.heading("fin",text="Inicio siguiente"); t.column("fin",width=185)
        t.heading("dur",text="Sin grabar"); t.column("dur",width=110,anchor="e")
        vsb=ttk.Scrollbar(win,orient="vertical",command=t.yview)
        t.configure(yscrollcommand=vsb.set)
        t.pack(fill="both",expand=True,padx=8,side="left")
        vsb.pack(fill="y",side="right",padx=(0,8))
        for g in gaps:
            t.insert("","end",values=(
                fmt_dt(g["dt_start"]),fmt_dt(g["dt_end"]),
                fmt_dur(g["duration_s"])))

    def _probe(self):
        clips = self._sel_clips()
        if not clips or not self._sd_root: return
        c   = clips[0]
        tmp = Path(tempfile.mkdtemp(prefix="sdhik_probe_"))
        ps  = tmp / clip_name(c,"ps")
        try:
            extract_clip(c, self._sd_root, ps)
            ok, detail = probe_clip(ps)
            fn = (messagebox.showinfo if ok else messagebox.showwarning)
            fn("Probe stream",
               f"{'Streams detectados' if ok else 'Sin streams válidos'}"
               f"\n\n{detail}")
        except Exception as e:
            messagebox.showerror("Error",str(e))

    def _hashes(self):
        try:    d = Path(self._var_out.get())
        except: d = Path.home()
        HashWindow(self, d)


if __name__ == "__main__":
    App().mainloop()