#!/usr/bin/env python3
"""
sd-hik-reader  ·  Visor e inspector de tarjetas SD Hikvision
Modelo compatible: CS-EB3-R200 y variantes con firmware Hikvision V5.x

UI: Tkinter — dark industrial theme
Plataforma: Windows (prioridad) / multiplataforma
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import json
import os
import string

from src import scan_folder, parse_index, parse_log, read_hex_region
from src import extract_clip, find_ffmpeg, ffmpeg_version
from src.parser import fmt_dt, fmt_sz, fmt_dur, analyze_hiv_header

# ── Paleta — dark industrial ──────────────────────────────────────────────────

C = {
    "bg":         "#0f1117",
    "bg2":        "#161b25",
    "panel":      "#1c2333",
    "panel2":     "#212840",
    "border":     "#2a3349",
    "border2":    "#3a4566",
    "accent":     "#3b82f6",      # azul principal
    "accent_d":   "#2563eb",
    "accent2":    "#10b981",      # verde éxito
    "accent2_d":  "#059669",
    "warn":       "#f59e0b",      # amarillo advertencia
    "error":      "#ef4444",      # rojo error
    "text":       "#e2e8f0",
    "text2":      "#94a3b8",
    "text3":      "#475569",
    "sel_bg":     "#1e3a5f",
    "sel_fg":     "#93c5fd",
    "vid_bg":     "#0f2137",
    "vid_fg":     "#60a5fa",
    "pic_bg":     "#0d2218",
    "pic_fg":     "#34d399",
    "cross_bg":   "#2a1a0a",
    "cross_fg":   "#fb923c",
    "log_bg":     "#1c1a0d",
    "log_fg":     "#fbbf24",
    "hex_bg":     "#0a0f1a",
    "hex_fg":     "#22d3ee",
    "hex_off":    "#64748b",
    "hex_asc":    "#a78bfa",
}

FONT_MONO  = ("Consolas",    9)
FONT_MONO2 = ("Consolas",   10)
FONT_UI    = ("Segoe UI",    9)
FONT_UI_B  = ("Segoe UI",    9, "bold")
FONT_TITLE = ("Segoe UI",   13, "bold")
FONT_H2    = ("Segoe UI",   10, "bold")
FONT_SMALL = ("Segoe UI",    8)
FONT_CARD  = ("Segoe UI",   22, "bold")

# ── Helpers UI ────────────────────────────────────────────────────────────────

def sep(parent, orient="h", **kw):
    if orient == "h":
        return tk.Frame(parent, bg=C["border"], height=1, **kw)
    return tk.Frame(parent, bg=C["border"], width=1, **kw)


def label(parent, text, style="normal", **kw):
    colors = {
        "normal":  (C["text"],   FONT_UI),
        "muted":   (C["text2"],  FONT_UI),
        "small":   (C["text2"],  FONT_SMALL),
        "title":   (C["text"],   FONT_TITLE),
        "h2":      (C["text"],   FONT_H2),
        "mono":    (C["hex_fg"], FONT_MONO),
        "warn":    (C["warn"],   FONT_UI_B),
        "error":   (C["error"],  FONT_UI_B),
        "accent":  (C["accent"], FONT_UI_B),
        "success": (C["accent2"],FONT_UI_B),
    }
    fg, font = colors.get(style, (C["text"], FONT_UI))
    return tk.Label(parent, text=text, bg=kw.pop("bg", C["bg2"]),
                    fg=fg, font=font, **kw)


def btn(parent, text, cmd, style="normal", width=None):
    styles = {
        "primary": (C["accent"],   C["accent_d"],  "#ffffff"),
        "success": (C["accent2"],  C["accent2_d"], "#ffffff"),
        "warn":    (C["warn"],     "#d97706",       "#000000"),
        "normal":  (C["panel2"],   C["border2"],    C["text"]),
        "danger":  (C["error"],    "#dc2626",       "#ffffff"),
    }
    bg, abg, fg = styles.get(style, styles["normal"])
    kw = dict(text=text, command=cmd, bg=bg, fg=fg,
               activebackground=abg, activeforeground=fg,
               font=FONT_UI, relief="flat", padx=12, pady=6,
               cursor="hand2", bd=0)
    if width:
        kw["width"] = width
    return tk.Button(parent, **kw)


def scrolled_text(parent, **kw):
    frame = tk.Frame(parent, bg=C["bg2"])
    txt = tk.Text(frame, bg=C["hex_bg"], fg=C["hex_fg"],
                  font=FONT_MONO, relief="flat", insertbackground=C["text"],
                  selectbackground=C["sel_bg"], selectforeground=C["sel_fg"],
                  wrap="none", borderwidth=0, **kw)
    vsb = ttk.Scrollbar(frame, orient="vertical",   command=txt.yview)
    hsb = ttk.Scrollbar(frame, orient="horizontal", command=txt.xview)
    txt.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
    hsb.pack(side="bottom", fill="x")
    vsb.pack(side="right",  fill="y")
    txt.pack(fill="both", expand=True)
    return frame, txt


def _configure_treeview_style():
    s = ttk.Style()
    s.theme_use("clam")
    s.configure("Dark.Treeview",
                 background=C["panel"], foreground=C["text"],
                 fieldbackground=C["panel"], rowheight=26,
                 font=FONT_UI, borderwidth=0, relief="flat")
    s.configure("Dark.Treeview.Heading",
                 background=C["bg2"], foreground=C["text2"],
                 font=("Segoe UI", 8, "bold"), relief="flat", padding=(6, 5))
    s.map("Dark.Treeview",
          background=[("selected", C["sel_bg"])],
          foreground=[("selected", C["sel_fg"])])
    s.configure("Dark.TNotebook",
                 background=C["bg"], borderwidth=0, tabmargins=0)
    s.configure("Dark.TNotebook.Tab",
                 background=C["bg2"], foreground=C["text2"],
                 font=FONT_UI, padding=(16, 8), borderwidth=0)
    s.map("Dark.TNotebook.Tab",
          background=[("selected", C["panel"])],
          foreground=[("selected", C["accent"])])
    s.configure("TScrollbar",
                 background=C["border"], troughcolor=C["bg"],
                 relief="flat", borderwidth=0, arrowsize=11)
    s.map("TScrollbar", background=[("active", C["text3"])])
    s.configure("TCombobox",
                 fieldbackground=C["panel2"], background=C["panel2"],
                 foreground=C["text"], selectbackground=C["sel_bg"],
                 arrowcolor=C["text2"], borderwidth=0)


# ── App principal ─────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("sd-hik-reader  ·  Hikvision SD Reader")
        self.geometry("1440x900")
        self.minsize(1100, 700)
        self.configure(bg=C["bg"])

        _configure_treeview_style()

        self._report   = None
        self._entries  = []
        self._hex_path = None
        self._hex_off  = 0
        self._sort_col = None
        self._sort_rev = False

        self._build_topbar()
        self._build_toolbar()
        self._build_statusbar()
        self._build_notebook()

    # ── Top bar ───────────────────────────────────────────────────────────────

    def _build_topbar(self):
        bar = tk.Frame(self, bg=C["panel"], height=54)
        bar.pack(fill="x"); bar.pack_propagate(False)

        # Barra lateral de color
        tk.Frame(bar, bg=C["accent"], width=4).pack(side="left", fill="y")

        # Logo / título
        left = tk.Frame(bar, bg=C["panel"])
        left.pack(side="left", padx=(16, 0))
        tk.Label(left, text="●", bg=C["panel"], fg=C["accent"],
                 font=("Segoe UI", 18)).pack(side="left")
        tk.Label(left, text=" sd-hik", bg=C["panel"], fg=C["accent"],
                 font=("Segoe UI", 14, "bold")).pack(side="left")
        tk.Label(left, text="-reader", bg=C["panel"], fg=C["text"],
                 font=("Segoe UI", 14)).pack(side="left")

        tk.Label(bar,
                 text="Visor e inspector de tarjetas SD Hikvision  ·  CS-EB3 · HIK filesystem",
                 bg=C["panel"], fg=C["text2"], font=FONT_SMALL).pack(side="left", padx=24)

        # Badge ffmpeg
        ffmpeg = find_ffmpeg()
        ffmpeg_txt = f"ffmpeg: {ffmpeg_version(ffmpeg)}" if ffmpeg else "ffmpeg: NO encontrado"
        ffmpeg_fg  = C["accent2"] if ffmpeg else C["error"]
        tk.Label(bar, text=ffmpeg_txt, bg=C["panel"], fg=ffmpeg_fg,
                 font=FONT_SMALL).pack(side="right", padx=16)

    # ── Toolbar ───────────────────────────────────────────────────────────────

    def _build_toolbar(self):
        tb = tk.Frame(self, bg=C["bg2"],
                      highlightbackground=C["border"], highlightthickness=1)
        tb.pack(fill="x")
        row = tk.Frame(tb, bg=C["bg2"])
        row.pack(side="left", padx=10, pady=6)

        btn(row, "📂  Carpeta SD",    self._pick_folder,  "primary").pack(side="left", padx=(0,4))
        btn(row, "💽  Unidad",        self._pick_drive,   "primary").pack(side="left", padx=(0,12))
        sep(row, "v").pack(side="left", fill="y", pady=3, padx=6)
        btn(row, "📄  Abrir índice",  self._open_index,   "normal").pack(side="left", padx=4)
        btn(row, "🔍  Abrir en HEX",  self._open_hex,     "normal").pack(side="left", padx=4)
        sep(row, "v").pack(side="left", fill="y", pady=3, padx=6)
        btn(row, "📋  Exportar JSON", self._export_json,  "normal").pack(side="left", padx=4)
        btn(row, "🔄  Re-escanear",   self._rescan,       "normal").pack(side="left", padx=4)

    # ── Status bar ────────────────────────────────────────────────────────────

    def _build_statusbar(self):
        sb = tk.Frame(self, bg=C["bg2"],
                      highlightbackground=C["border"], highlightthickness=1, height=24)
        sb.pack(side="bottom", fill="x"); sb.pack_propagate(False)
        self._sv = tk.StringVar(value="Seleccioná una carpeta SD o unidad para comenzar.")
        tk.Label(sb, textvariable=self._sv, bg=C["bg2"], fg=C["text2"],
                 font=FONT_SMALL, anchor="w").pack(side="left", padx=10, fill="y")

    # ── Notebook ──────────────────────────────────────────────────────────────

    def _build_notebook(self):
        self.nb = ttk.Notebook(self, style="Dark.TNotebook")
        self.nb.pack(fill="both", expand=True, padx=0, pady=0)
        self._tabs = {}
        tabs = [
            ("dashboard",  "  📊  Dashboard  "),
            ("timeline",   "  📅  Línea de tiempo  "),
            ("clips",      "  🎬  Clips  "),
            ("logs",       "  📝  Logs RATS  "),
            ("indexes",    "  🗂  Índices OFNI  "),
            ("hexviewer",  "  🔬  Visor HEX  "),
            ("json",       "  { }  JSON  "),
        ]
        for key, lbl in tabs:
            f = tk.Frame(self.nb, bg=C["bg2"])
            self._tabs[key] = f
            self.nb.add(f, text=lbl)
            self._placeholder(f)

    def _placeholder(self, frame):
        for w in frame.winfo_children(): w.destroy()
        tk.Label(frame,
                 text="Sin datos.\nSeleccioná una carpeta SD o unidad.",
                 bg=C["bg2"], fg=C["text3"],
                 font=("Segoe UI", 11), justify="center").pack(expand=True)

    # ── Acciones ──────────────────────────────────────────────────────────────

    def _pick_folder(self):
        d = filedialog.askdirectory(title="Seleccionar carpeta SD")
        if d: self._scan(d)

    def _pick_drive(self):
        drives = [f"{l}:\\" for l in string.ascii_uppercase if os.path.exists(f"{l}:\\")]
        if not drives:
            self._pick_folder(); return

        win = tk.Toplevel(self); win.grab_set()
        win.title("Seleccionar unidad")
        win.geometry("400x400"); win.configure(bg=C["bg"])
        win.resizable(False, False)

        tk.Label(win, text="Unidades disponibles", bg=C["bg"], fg=C["text"],
                 font=FONT_H2).pack(pady=(16,8), padx=16, anchor="w")

        lb = tk.Listbox(win, bg=C["panel"], fg=C["text"],
                        selectbackground=C["sel_bg"], selectforeground=C["sel_fg"],
                        font=FONT_UI, relief="flat",
                        highlightbackground=C["border"], highlightthickness=1, height=10)
        lb.pack(fill="both", expand=True, padx=16)
        for d in drives:
            try:
                import shutil as _sh
                tot, _, free = _sh.disk_usage(d)
                lb.insert("end", f"  {d}   {fmt_sz(tot)} total · {fmt_sz(free)} libre")
            except:
                lb.insert("end", f"  {d}")

        def ok():
            sel = lb.curselection()
            if not sel: return
            drive = drives[sel[0]]; win.destroy()
            for sub in ["", "HIK", "Record"]:
                cand = os.path.join(drive, sub) if sub else drive
                if os.path.isdir(cand) and any(
                    f.lower().startswith("index") and f.lower().endswith(".bin")
                    for f in os.listdir(cand)):
                    self._scan(cand); return
            self._scan(drive)

        btn(win, "Abrir unidad", ok, "primary").pack(pady=12)

    def _open_index(self):
        path = filedialog.askopenfilename(
            title="Abrir archivo de índice",
            filetypes=[("Índice SD", "index*.bin"), ("Todos", "*.*")])
        if not path: return
        self._sv.set(f"Parseando {os.path.basename(path)} …"); self.update()
        d = parse_index(path)
        folder = os.path.dirname(path)
        report = {
            "folder": folder, "scanned_at": "",
            "indexes": [d], "logs": [], "hiv_files": [],
            "other": [], "best": d,
            "summary": {
                "best_index": d["filename"], "total": len(d["entries"]),
                "videos": sum(1 for e in d["entries"] if e["tipo"]=="Video"),
                "fotos":  sum(1 for e in d["entries"] if e["tipo"]=="Foto"),
                "cross_block": sum(1 for e in d["entries"] if e["cross_block"]),
                "hiv_count": 0, "hiv_size": "—",
                "date_from": fmt_dt(d["ts_first"]) if d["ts_first"] else "—",
                "date_to":   fmt_dt(d["ts_last"])  if d["ts_last"]  else "—",
                "log_events": 0,
                "tz_note": "Hora local cámara (sin offset UTC)",
            }
        }
        self._populate(report)

    def _open_hex(self):
        path = filedialog.askopenfilename(
            title="Abrir archivo en visor HEX",
            filetypes=[("Binarios SD", "*.bin *.mp4"), ("Todos", "*.*")])
        if not path: return
        self._hex_path = path
        self._hex_off  = 0
        self.nb.select(self._tabs["hexviewer"])
        self._render_hex()

    def _rescan(self):
        if self._report: self._scan(self._report["folder"])
        else: self._sv.set("No hay carpeta seleccionada.")

    def _scan(self, folder):
        self._sv.set(f"Escaneando {folder} …"); self.update()
        def worker():
            r = scan_folder(folder)
            self.after(0, lambda: self._populate(r))
        threading.Thread(target=worker, daemon=True).start()

    def _populate(self, report):
        self._report  = report
        self._entries = report["best"]["entries"] if report.get("best") else []
        s = report["summary"]
        self._sv.set(
            f"✔  {report['folder']}  │  {s['total']} clips  "
            f"({s['videos']} videos, {s['fotos']} fotos)  │  "
            f"{s['date_from']} → {s['date_to']}  │  {s['tz_note']}"
        )
        self._build_dashboard(report)
        self._build_timeline(report)
        self._build_clips(report)
        self._build_logs(report)
        self._build_indexes(report)
        self._build_json(report)

    # ── TAB: Dashboard ────────────────────────────────────────────────────────

    def _build_dashboard(self, report):
        f = self._tabs["dashboard"]
        for w in f.winfo_children(): w.destroy()
        s = report["summary"]

        # Scroll container
        canvas = tk.Canvas(f, bg=C["bg2"], highlightthickness=0)
        vsb = ttk.Scrollbar(f, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y"); canvas.pack(fill="both", expand=True)
        inner = tk.Frame(canvas, bg=C["bg2"])
        cw = canvas.create_window((0,0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: (
            canvas.configure(scrollregion=canvas.bbox("all")),
            canvas.itemconfig(cw, width=canvas.winfo_width())
        ))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(cw, width=e.width))

        pad = tk.Frame(inner, bg=C["bg2"]); pad.pack(fill="both", padx=24, pady=20)

        # Título
        tk.Label(pad, text="Dashboard", bg=C["bg2"], fg=C["text"],
                 font=FONT_TITLE).pack(anchor="w")
        tk.Label(pad, text=report["folder"], bg=C["bg2"], fg=C["text2"],
                 font=FONT_SMALL).pack(anchor="w", pady=(2,16))

        # Cards métricas
        cf = tk.Frame(pad, bg=C["bg2"]); cf.pack(fill="x", pady=(0,20))
        cards = [
            ("🎬", "Videos",        str(s["videos"]),      C["accent"]),
            ("📷", "Fotos",         str(s["fotos"]),        C["accent2"]),
            ("⚠️", "Cross-block",   str(s["cross_block"]), C["warn"]),
            ("💾", "Archivos HIV",  str(s["hiv_count"]),   C["text2"]),
            ("📝", "Eventos log",   str(s["log_events"]),  C["log_fg"]),
        ]
        for icon, lbl, val, color in cards:
            card = tk.Frame(cf, bg=C["panel"],
                            highlightbackground=C["border"], highlightthickness=1)
            card.pack(side="left", padx=(0,10), ipadx=20, ipady=12)
            tk.Label(card, text=icon, bg=C["panel"],
                     font=("Segoe UI",20)).pack()
            tk.Label(card, text=val, bg=C["panel"], fg=color,
                     font=FONT_CARD).pack()
            tk.Label(card, text=lbl, bg=C["panel"], fg=C["text2"],
                     font=FONT_SMALL).pack()

        sep(pad).pack(fill="x", pady=16)

        # Detalles
        def row_kv(parent, k, v, vc=None):
            r = tk.Frame(parent, bg=C["bg2"]); r.pack(fill="x", pady=3)
            tk.Label(r, text=k, bg=C["bg2"], fg=C["text2"],
                     font=FONT_UI, width=24, anchor="w").pack(side="left")
            tk.Label(r, text=v, bg=C["bg2"], fg=vc or C["text"],
                     font=FONT_UI_B, anchor="w").pack(side="left")

        tk.Label(pad, text="Índices", bg=C["bg2"], fg=C["text"],
                 font=FONT_H2).pack(anchor="w", pady=(0,6))
        row_kv(pad, "Índice principal",   s["best_index"])
        row_kv(pad, "Total clips",        str(s["total"]))
        row_kv(pad, "Período grabado",    f"{s['date_from']}  →  {s['date_to']}")
        row_kv(pad, "Espacio HIV total",  s["hiv_size"])
        row_kv(pad, "Zona horaria",       s["tz_note"], C["warn"])

        sep(pad).pack(fill="x", pady=16)

        # Tabla de archivos
        tk.Label(pad, text="Archivos en la SD", bg=C["bg2"], fg=C["text"],
                 font=FONT_H2).pack(anchor="w", pady=(0,6))

        tf = tk.Frame(pad, bg=C["bg2"]); tf.pack(fill="x")
        cols  = ("nombre","tipo","tam","ent","estado")
        hdrs  = ("Archivo","Tipo","Tamaño","Entradas","Estado / Rango")
        widths= (210, 170, 90, 80, 440)
        tree  = ttk.Treeview(tf, columns=cols, show="headings",
                              height=12, style="Dark.Treeview")
        for c, h, w in zip(cols, hdrs, widths):
            tree.heading(c, text=h); tree.column(c, width=w, minwidth=30, anchor="w")
        tree.tag_configure("ip",  background=C["vid_bg"],  foreground=C["vid_fg"])
        tree.tag_configure("ib",  background=C["panel"],   foreground=C["text2"])
        tree.tag_configure("lg",  background=C["log_bg"],  foreground=C["log_fg"])
        tree.tag_configure("hv",  background=C["panel"],   foreground=C["text2"])
        tree.tag_configure("err", background=C["cross_bg"],foreground=C["error"])

        for d in report["indexes"]:
            is_p = d["priority"] == "principal"
            n    = len(d["entries"])
            rng  = f"{fmt_dt(d['ts_first'])}  →  {fmt_dt(d['ts_last'])}" if n else "vacío"
            if d["error"]: rng = f"⚠ {d['error']}"
            tag  = "err" if d["error"] else ("ip" if is_p else "ib")
            tree.insert("","end",tags=(tag,), values=(
                d["filename"],
                "● Principal" if is_p else "○ Backup",
                d["size_fmt"], n if n else "—", rng))
        for d in report["logs"]:
            n  = len(d["entries"])
            st = f"{n} eventos RATS"
            if d.get("header_dt"): st += f"  ·  último: {d['header_dt']}"
            if d["error"]: st = f"⚠ {d['error']}"
            tree.insert("","end",tags=("lg",), values=(
                d["filename"],"Log RATS",d["size_fmt"],n,st))
        for h in report["hiv_files"][:10]:
            tree.insert("","end",tags=("hv",), values=(
                h["name"],"HIV (MPEG-PS raw)",h["size_fmt"],"—",""))
        rest = len(report["hiv_files"]) - 10
        if rest > 0:
            tree.insert("","end",tags=("hv",), values=(
                f"… y {rest} más","HIV","","",""))

        vsb2 = ttk.Scrollbar(tf, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb2.set)
        vsb2.pack(side="right", fill="y"); tree.pack(fill="x")

        # Nota técnica
        sep(pad).pack(fill="x", pady=16)
        note = (
            "ℹ️  Formato verificado: CS-EB3-R200  ·  Firmware V5.4.0 build 250520\n"
            "     Los archivos HIV*.mp4 son streams MPEG-PS raw (no son MP4 reales).\n"
            "     Los timestamps se graban en hora local sin offset UTC (bug de firmware)."
        )
        tk.Label(pad, text=note, bg=C["bg2"], fg=C["text2"],
                 font=FONT_SMALL, justify="left", anchor="w").pack(anchor="w")

    # ── TAB: Línea de tiempo ──────────────────────────────────────────────────

    def _build_timeline(self, report):
        f = self._tabs["timeline"]
        for w in f.winfo_children(): w.destroy()

        entries = report["best"]["entries"] if report.get("best") else []
        if not entries:
            self._placeholder(f); return

        # Cabecera
        hdr = tk.Frame(f, bg=C["bg2"]); hdr.pack(fill="x", padx=16, pady=(12,4))
        tk.Label(hdr, text="Línea de tiempo", bg=C["bg2"], fg=C["text"],
                 font=FONT_TITLE).pack(side="left")
        tk.Label(hdr, text=f"  {len(entries)} eventos · cada bloque = 1 slot (~0.667s)",
                 bg=C["bg2"], fg=C["text2"], font=FONT_SMALL).pack(side="left")

        sep(f).pack(fill="x", padx=0)

        # Canvas con timeline visual
        canvas_frame = tk.Frame(f, bg=C["bg2"])
        canvas_frame.pack(fill="both", expand=True, padx=16, pady=12)

        tl = tk.Canvas(canvas_frame, bg=C["bg"], highlightthickness=0)
        hsb = ttk.Scrollbar(canvas_frame, orient="horizontal", command=tl.xview)
        vsb = ttk.Scrollbar(canvas_frame, orient="vertical",   command=tl.yview)
        tl.configure(xscrollcommand=hsb.set, yscrollcommand=vsb.set)
        hsb.pack(side="bottom", fill="x")
        vsb.pack(side="right",  fill="y")
        tl.pack(fill="both", expand=True)

        # Agrupar por fecha
        from datetime import datetime, timezone
        from collections import defaultdict
        days = defaultdict(list)
        for e in entries:
            dt = datetime.fromtimestamp(e["ts_s"], tz=timezone.utc)
            key = dt.strftime("%d-%m-%Y")
            days[key].append(e)

        # Renderizar días
        BLOCK_W = 18; BLOCK_H = 18; GAP = 3
        DAY_H   = 46; LABEL_W = 110; TOP_PAD = 10

        y = TOP_PAD
        max_x = LABEL_W
        for day_str in sorted(days.keys(), key=lambda d: d.split("-")[::-1]):
            evs = sorted(days[day_str], key=lambda e: e["ts_s"])
            # Etiqueta del día
            tl.create_text(8, y + DAY_H//2, text=day_str,
                           fill=C["text2"], font=("Segoe UI",8), anchor="w")
            # Separador
            tl.create_line(LABEL_W-4, y, LABEL_W-4, y+DAY_H,
                           fill=C["border"], width=1)

            # Bloques por hora (0-23)
            from datetime import datetime, timezone
            for e in evs:
                dt  = datetime.fromtimestamp(e["ts_s"], tz=timezone.utc)
                # Posición X proporcional a la hora del día
                sec_of_day = dt.hour*3600 + dt.minute*60 + dt.second
                x = LABEL_W + int(sec_of_day / 86400 * 1200)

                color = C["vid_fg"] if e["tipo"]=="Video" else C["pic_fg"]
                if e["cross_block"]: color = C["cross_fg"]

                bx1, by1 = x, y + 4
                bx2, by2 = x + BLOCK_W, y + BLOCK_H + 4

                item = tl.create_rectangle(bx1, by1, bx2, by2,
                                           fill=color, outline="", tags=("slot",))
                tl.tag_bind(item, "<Enter>", lambda ev, e=e, bx=bx1, by=by1:
                            self._tl_tooltip(tl, e, bx, by))
                tl.tag_bind(item, "<Leave>", lambda ev:
                            self._tl_hide_tooltip(tl))

                max_x = max(max_x, bx2 + 10)

            # Marcas de hora
            for h in range(0, 24, 2):
                hx = LABEL_W + int(h * 3600 / 86400 * 1200)
                tl.create_text(hx, y + DAY_H - 6, text=f"{h:02d}h",
                               fill=C["text3"], font=("Segoe UI",7), anchor="w")
            y += DAY_H + 4

        tl.configure(scrollregion=(0, 0, max_x + 20, y + 10))

        # Leyenda
        leg = tk.Frame(f, bg=C["bg2"]); leg.pack(fill="x", padx=16, pady=(0,8))
        for color, lbl in [(C["vid_fg"],"Video"),(C["pic_fg"],"Foto"),(C["cross_fg"],"Cross-block")]:
            tk.Canvas(leg, width=14, height=14, bg=C["bg2"],
                      highlightthickness=0).pack(side="left", padx=(0,2))
            c2 = leg.winfo_children()[-1]
            c2.create_rectangle(2,2,12,12, fill=color, outline="")
            tk.Label(leg, text=lbl, bg=C["bg2"], fg=C["text2"],
                     font=FONT_SMALL).pack(side="left", padx=(0,12))

    def _tl_tooltip(self, canvas, entry, x, y):
        canvas.delete("tooltip")
        txt = (f"Slot {entry['slot']}  ·  {entry['tipo']}\n"
               f"{entry['dt_s']}\n"
               f"HIV: {entry['hiv_file']}  offset: {entry['off_fmt']}\n"
               f"Tamaño: {entry['size_fmt']}  "
               f"{'⚠ Cross-block' if entry['cross_block'] else ''}")
        bx, by = min(x+24, 900), y
        box = canvas.create_rectangle(bx-2, by-2, bx+220, by+60,
                                      fill=C["panel"], outline=C["border"],
                                      tags=("tooltip",))
        canvas.create_text(bx+4, by+4, text=txt, fill=C["text"],
                           font=FONT_SMALL, anchor="nw", tags=("tooltip",))

    def _tl_hide_tooltip(self, canvas):
        canvas.delete("tooltip")

    # ── TAB: Clips ────────────────────────────────────────────────────────────

    def _build_clips(self, report):
        f = self._tabs["clips"]
        for w in f.winfo_children(): w.destroy()
        self._entries = report["best"]["entries"] if report.get("best") else []

        # Barra de filtros
        bar = tk.Frame(f, bg=C["bg2"],
                       highlightbackground=C["border"], highlightthickness=1)
        bar.pack(fill="x", padx=0, pady=0)
        row = tk.Frame(bar, bg=C["bg2"]); row.pack(side="left", padx=10, pady=7)

        tk.Label(row, text="Tipo:", bg=C["bg2"], fg=C["text2"], font=FONT_UI).pack(side="left")
        self._fv = tk.StringVar(value="Todos")
        cb = ttk.Combobox(row, textvariable=self._fv, width=9,
                          values=["Todos","Video","Foto"], state="readonly",
                          style="TCombobox")
        cb.pack(side="left", padx=(4,12))
        cb.bind("<<ComboboxSelected>>", lambda *a: self._filter_clips())

        tk.Label(row, text="Buscar:", bg=C["bg2"], fg=C["text2"], font=FONT_UI).pack(side="left")
        self._sv_q = tk.StringVar()
        self._sv_q.trace_add("write", lambda *a: self.after(80, self._filter_clips))
        tk.Entry(row, textvariable=self._sv_q,
                 bg=C["panel2"], fg=C["text"], insertbackground=C["text"],
                 relief="flat", font=FONT_UI, width=22,
                 highlightbackground=C["border"], highlightthickness=1).pack(side="left")

        sep(row, "v").pack(side="left", fill="y", pady=2, padx=10)
        btn(row, "⬇  Descargar clip", self._download_clip, "success").pack(side="left", padx=4)

        self._lbl_cnt = tk.Label(row, text="", bg=C["bg2"], fg=C["text2"], font=FONT_SMALL)
        self._lbl_cnt.pack(side="left", padx=12)

        # Treeview
        tf = tk.Frame(f, bg=C["bg2"]); tf.pack(fill="both", expand=True)
        COLS   = ("slot","tipo","dt_s","dur","hiv","off","tam","scr","cruz")
        HDRS   = ("#","Tipo","Inicio","Dur. SCR","Archivo HIV","Offset HIV","Tamaño","SCR ~","Cross")
        WIDTHS = (48, 62, 162, 82, 128, 110, 90, 70, 68)

        self._tc = ttk.Treeview(tf, columns=COLS, show="headings", style="Dark.Treeview")
        for col, hdr, w in zip(COLS, HDRS, WIDTHS):
            self._tc.heading(col, text=hdr, command=lambda c=col: self._sort_clips(c))
            self._tc.column(col, width=w, minwidth=30, anchor="w")
        self._tc.tag_configure("vid",   background=C["vid_bg"],   foreground=C["vid_fg"])
        self._tc.tag_configure("pic",   background=C["pic_bg"],   foreground=C["pic_fg"])
        self._tc.tag_configure("cross", background=C["cross_bg"], foreground=C["cross_fg"])

        vsb = ttk.Scrollbar(tf, orient="vertical",   command=self._tc.yview)
        hsb = ttk.Scrollbar(tf, orient="horizontal", command=self._tc.xview)
        self._tc.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        hsb.pack(side="bottom", fill="x")
        vsb.pack(side="right",  fill="y")
        self._tc.pack(fill="both", expand=True)

        self._filter_clips()

    def _filter_clips(self):
        if not hasattr(self, "_tc"): return
        tipo  = getattr(self, "_fv",   tk.StringVar()).get()
        query = getattr(self, "_sv_q", tk.StringVar()).get().strip().lower()
        self._tc.delete(*self._tc.get_children())
        shown = 0
        for e in self._entries:
            if tipo != "Todos" and e["tipo"] != tipo: continue
            if query:
                hay = f"{e['dt_s']} {e['hiv_file']} {e['slot']} {e['tipo']}".lower()
                if query not in hay: continue
            tag = "cross" if e["cross_block"] else ("vid" if e["tipo"]=="Video" else "pic")
            self._tc.insert("","end",tags=(tag,), values=(
                e["slot"], e["tipo"], e["dt_s"],
                fmt_dur(e["dur_scr"]), e["hiv_file"],
                e["off_fmt"], e["size_fmt"],
                f"~{e['dur_scr']:.3f}s",
                "⚠ Sí" if e["cross_block"] else "No",
            ))
            shown += 1
        if hasattr(self, "_lbl_cnt"):
            self._lbl_cnt.config(text=f"{shown} de {len(self._entries)} clips")

    def _sort_clips(self, col):
        if self._sort_col == col: self._sort_rev = not self._sort_rev
        else: self._sort_col, self._sort_rev = col, False
        items = [(self._tc.set(k, col), k) for k in self._tc.get_children("")]
        try:
            items.sort(key=lambda x: float(
                x[0].replace(" KB","").replace(" MB","").replace("—","0").replace("~","").replace("s","")),
                reverse=self._sort_rev)
        except ValueError:
            items.sort(key=lambda x: x[0], reverse=self._sort_rev)
        for i, (_, k) in enumerate(items): self._tc.move(k, "", i)

    def _download_clip(self):
        sel = self._tc.selection()
        if not sel:
            messagebox.showinfo("Sin selección",
                                "Seleccioná un clip de la lista primero."); return
        if not find_ffmpeg():
            messagebox.showerror("ffmpeg no encontrado",
                                 "Instalá ffmpeg y agregalo al PATH.\n"
                                 "https://ffmpeg.org/download.html"); return

        vals   = self._tc.item(sel[0], "values")
        slot_n = int(vals[0])
        entry  = next((e for e in self._entries if e["slot"] == slot_n), None)
        if not entry:
            messagebox.showerror("Error","No se encontró la entrada."); return

        folder   = self._report["folder"]
        hiv_path = os.path.join(folder, entry["hiv_file"])
        if not os.path.isfile(hiv_path):
            messagebox.showerror("HIV no encontrado",
                                 f"No se encontró:\n{hiv_path}"); return

        # Diálogo opciones
        win = tk.Toplevel(self); win.grab_set()
        win.title("Opciones de extracción")
        win.geometry("380x280"); win.configure(bg=C["bg"])
        win.resizable(False, False)

        tk.Label(win, text="Opciones de extracción", bg=C["bg"],
                 fg=C["text"], font=FONT_H2).pack(pady=(16,4), padx=16, anchor="w")
        tk.Label(win, text=f"Slot {entry['slot']}  ·  {entry['dt_s']}  ·  {entry['size_fmt']}",
                 bg=C["bg"], fg=C["text2"], font=FONT_SMALL).pack(padx=16, anchor="w")
        sep(win).pack(fill="x", pady=10)

        tk.Label(win, text="Resolución de salida:", bg=C["bg"],
                 fg=C["text"], font=FONT_UI).pack(padx=16, anchor="w")
        rv = tk.StringVar(value="original")
        for lbl, val in [
            ("Original (copia directa, más rápido)", "original"),
            ("1920×1080  Full HD",                   "1920x1080"),
            ("1280×720   HD",                        "1280x720"),
            ("854×480    SD",                        "854x480"),
        ]:
            tk.Radiobutton(win, text=lbl, variable=rv, value=val,
                           bg=C["bg"], fg=C["text"], selectcolor=C["panel"],
                           activebackground=C["bg"], font=FONT_UI).pack(anchor="w", padx=28)

        chosen = {"ok": False, "res": None}

        def ok():
            chosen["ok"]  = True
            chosen["res"] = None if rv.get()=="original" else rv.get()
            win.destroy()

        btn(win, "Continuar", ok, "success").pack(pady=12)
        self.wait_window(win)
        if not chosen["ok"]: return

        ts_str   = entry["dt_s"].replace(":","−").replace(" ","_")
        def_name = f"HIK_{ts_str}.mp4"
        out_path = filedialog.asksaveasfilename(
            defaultextension=".mp4",
            filetypes=[("Video MP4","*.mp4"),("Todos","*.*")],
            initialfile=def_name)
        if not out_path: return

        self._sv.set(f"⏳ Extrayendo slot {entry['slot']}  gl_s={entry['gl_s']:,} …")
        self.update()

        def worker():
            result = extract_clip(
                hiv_path, entry["gl_s"], entry["gl_e"],
                out_path, chosen["res"],
                progress_cb=lambda msg: self.after(0, lambda m=msg: self._sv.set(m[:120]))
            )
            self.after(0, lambda: self._dl_done(result))

        threading.Thread(target=worker, daemon=True).start()

    def _dl_done(self, result):
        if result["success"]:
            self._sv.set(f"✔ Guardado: {result['out_path']}  ({fmt_sz(result['out_size'])})")
            messagebox.showinfo("Extracción completa",
                                f"Clip guardado:\n{result['out_path']}\n\n"
                                f"Tamaño: {fmt_sz(result['out_size'])}\n"
                                f"Duración: {fmt_dur(result['duration'])}")
        else:
            self._sv.set(f"✘ Error de extracción")
            messagebox.showerror("Error de extracción",
                                 result["error"] or "Error desconocido.")

    # ── TAB: Logs RATS ────────────────────────────────────────────────────────

    def _build_logs(self, report):
        f = self._tabs["logs"]
        for w in f.winfo_children(): w.destroy()
        if not report["logs"]:
            self._placeholder(f); return

        nb2 = ttk.Notebook(f, style="Dark.TNotebook")
        nb2.pack(fill="both", expand=True)

        for d in report["logs"]:
            tab = tk.Frame(nb2, bg=C["bg2"])
            nb2.add(tab, text=f"  {d['filename']}  ")

            info = tk.Frame(tab, bg=C["bg2"]); info.pack(fill="x", padx=10, pady=6)
            tk.Label(info,
                     text=(f"  {len(d['entries'])} eventos RATS  ·  "
                           f"último acceso: {d.get('header_dt','—')}  ·  "
                           f"{d['size_fmt']}"),
                     bg=C["bg2"], fg=C["text2"], font=FONT_SMALL).pack(side="left")

            if d["error"]:
                tk.Label(tab, text=f"ERROR: {d['error']}",
                         bg=C["bg2"], fg=C["error"], font=FONT_UI).pack(padx=10)
                continue

            tf = tk.Frame(tab, bg=C["bg2"]); tf.pack(fill="both", expand=True, padx=10, pady=(0,8))
            COLS = ("idx","dt","tipo","cod","off")
            HDRS = ("#","Fecha / Hora","Tipo de evento","Código","Offset")
            WS   = (52, 175, 240, 120, 110)
            tree = ttk.Treeview(tf, columns=COLS, show="headings", style="Dark.Treeview")
            tree.tag_configure("ev",  background=C["log_bg"],  foreground=C["log_fg"])
            tree.tag_configure("sys", background=C["panel"],   foreground=C["text2"])
            for c, h, w in zip(COLS, HDRS, WS):
                tree.heading(c, text=h); tree.column(c, width=w, minwidth=30, anchor="w")
            for e in d["entries"]:
                tag = "sys" if "Sistema" in e["tipo"] else "ev"
                tree.insert("","end",tags=(tag,), values=(
                    e["idx"], e["dt"], e["tipo"], e["codigo"], f"0x{e['offset']:x}"))
            vsb = ttk.Scrollbar(tf, orient="vertical", command=tree.yview)
            tree.configure(yscrollcommand=vsb.set)
            vsb.pack(side="right", fill="y"); tree.pack(fill="both", expand=True)

    # ── TAB: Índices OFNI ─────────────────────────────────────────────────────

    def _build_indexes(self, report):
        f = self._tabs["indexes"]
        for w in f.winfo_children(): w.destroy()

        canvas = tk.Canvas(f, bg=C["bg2"], highlightthickness=0)
        vsb    = ttk.Scrollbar(f, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y"); canvas.pack(fill="both", expand=True)
        inner = tk.Frame(canvas, bg=C["bg2"])
        cw    = canvas.create_window((0,0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: (
            canvas.configure(scrollregion=canvas.bbox("all")),
            canvas.itemconfig(cw, width=canvas.winfo_width())
        ))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(cw, width=e.width))

        for d in report["indexes"]:
            is_p = d["priority"] == "principal"
            n    = len(d["entries"])

            card = tk.Frame(inner, bg=C["panel"],
                            highlightbackground=C["accent"] if is_p else C["border"],
                            highlightthickness=1)
            card.pack(fill="x", padx=12, pady=(10,0))

            # Header de la card
            hr = tk.Frame(card, bg=C["panel"]); hr.pack(fill="x", padx=14, pady=8)
            badge_txt  = "● PRINCIPAL" if is_p else "○ BACKUP"
            badge_col  = C["accent"] if is_p else C["text3"]
            tk.Label(hr, text=badge_txt, bg=C["panel"], fg=badge_col,
                     font=("Segoe UI",8,"bold")).pack(side="left")
            tk.Label(hr, text=f"  {d['filename']}", bg=C["panel"], fg=C["text"],
                     font=FONT_H2).pack(side="left")
            tk.Label(hr,
                     text=(f"  ·  {d['size_fmt']}  ·  "
                           f"header en 0x{d.get('header_offset',0):x}  ·  "
                           f"{d['slots_total']} slots  ·  {n} válidas"),
                     bg=C["panel"], fg=C["text2"], font=FONT_SMALL).pack(side="left")

            if d["error"]:
                tk.Label(card, text=f"ERROR: {d['error']}",
                         bg=C["panel"], fg=C["error"], font=FONT_UI).pack(anchor="w", padx=14, pady=(0,8))
                continue

            # Timestamps del header
            if d["header_ts"]:
                tr = tk.Frame(card, bg=C["panel"]); tr.pack(fill="x", padx=14, pady=(0,4))
                tk.Label(tr, text="Header timestamps: ",
                         bg=C["panel"], fg=C["text2"], font=FONT_SMALL).pack(side="left")
                for ht in d["header_ts"][:4]:
                    tk.Label(tr, text=f"  +0x{ht['offset']:02x}: {ht['dt']}",
                             bg=C["panel"], fg=C["warn"], font=FONT_SMALL).pack(side="left")

            # Hex del header
            if d["header_hex"]:
                hf = tk.Frame(card, bg=C["hex_bg"]); hf.pack(fill="x", padx=14, pady=(0,4))
                tk.Text(hf, bg=C["hex_bg"], fg=C["hex_fg"], font=FONT_MONO,
                        height=5, relief="flat", state="normal",
                        wrap="none").pack(fill="x", padx=4, pady=4)
                hf.winfo_children()[-1].insert("1.0", d["header_hex"])
                hf.winfo_children()[-1].config(state="disabled")

            if n == 0:
                continue

            # Rango
            rr = tk.Frame(card, bg=C["panel"]); rr.pack(fill="x", padx=14, pady=(0,4))
            tk.Label(rr, text=f"Rango: {fmt_dt(d['ts_first'])}  →  {fmt_dt(d['ts_last'])}",
                     bg=C["panel"], fg=C["text2"], font=FONT_SMALL).pack(side="left")

            # Primeras 5 entradas como muestra
            tk.Label(card, text="Primeras entradas:",
                     bg=C["panel"], fg=C["text2"], font=FONT_SMALL).pack(anchor="w", padx=14)
            sample_frame = tk.Frame(card, bg=C["panel"])
            sample_frame.pack(fill="x", padx=14, pady=(2,10))
            COLS2  = ("slot","tipo","dt_s","hiv","gl_s","gl_e","tam","cross","stream_b")
            HDRS2  = ("#","Tipo","Inicio","HIV","gl_s","gl_e","Tamaño","Cross","stream_bytes")
            WIDTHS2= (48,62,158,110,110,110,86,60,100)
            tree2 = ttk.Treeview(sample_frame, columns=COLS2,
                                  show="headings", height=min(n,5), style="Dark.Treeview")
            for c,h,w in zip(COLS2,HDRS2,WIDTHS2):
                tree2.heading(c, text=h); tree2.column(c, width=w, minwidth=30, anchor="w")
            tree2.tag_configure("v", background=C["vid_bg"],  foreground=C["vid_fg"])
            tree2.tag_configure("p", background=C["pic_bg"],  foreground=C["pic_fg"])
            tree2.tag_configure("x", background=C["cross_bg"],foreground=C["cross_fg"])
            for e in d["entries"][:5]:
                tag = "x" if e["cross_block"] else ("v" if e["tipo"]=="Video" else "p")
                tree2.insert("","end",tags=(tag,), values=(
                    e["slot"], e["tipo"], e["dt_s"], e["hiv_file"],
                    f"{e['gl_s']:,}", f"{e['gl_e']:,}", e["size_fmt"],
                    "Sí" if e["cross_block"] else "—",
                    fmt_sz(e.get("stream_bytes",0))))
            tree2.pack(fill="x")

    # ── TAB: Visor HEX ────────────────────────────────────────────────────────

    def _render_hex(self):
        f = self._tabs["hexviewer"]
        for w in f.winfo_children(): w.destroy()

        # Toolbar HEX
        tb = tk.Frame(f, bg=C["bg2"]); tb.pack(fill="x", padx=8, pady=6)

        tk.Label(tb, text="Archivo:", bg=C["bg2"], fg=C["text2"], font=FONT_UI).pack(side="left")
        self._hex_path_var = tk.StringVar(value=self._hex_path or "—")
        tk.Label(tb, textvariable=self._hex_path_var, bg=C["bg2"],
                 fg=C["accent"], font=FONT_SMALL, width=55, anchor="w").pack(side="left", padx=4)

        btn(tb, "📂 Abrir", self._open_hex, "normal").pack(side="left", padx=4)
        sep(tb, "v").pack(side="left", fill="y", pady=2, padx=8)

        tk.Label(tb, text="Offset (hex):", bg=C["bg2"], fg=C["text2"], font=FONT_UI).pack(side="left")
        self._hex_off_var = tk.StringVar(value="0")
        oe = tk.Entry(tb, textvariable=self._hex_off_var,
                      bg=C["panel2"], fg=C["hex_fg"], font=FONT_MONO,
                      relief="flat", width=12,
                      highlightbackground=C["border"], highlightthickness=1)
        oe.pack(side="left", padx=4)
        oe.bind("<Return>", lambda e: self._hex_goto())

        btn(tb, "Ir", self._hex_goto, "normal").pack(side="left", padx=2)
        btn(tb, "◀ Prev", lambda: self._hex_nav(-1), "normal").pack(side="left", padx=2)
        btn(tb, "Next ▶", lambda: self._hex_nav(+1), "normal").pack(side="left", padx=2)
        sep(tb, "v").pack(side="left", fill="y", pady=2, padx=8)

        # Accesos rápidos para firmas conocidas
        tk.Label(tb, text="Ir a:", bg=C["bg2"], fg=C["text2"], font=FONT_UI).pack(side="left")
        for lbl, sig in [("OFNI", b"OFNI"), ("RATS", b"RATS"),
                          ("MPEG-PS", b"\x00\x00\x01\xba"), ("ftyp", b"ftyp")]:
            btn(tb, lbl, lambda s=sig: self._hex_find(s), "normal").pack(side="left", padx=2)

        sep(f).pack(fill="x")

        # Info del archivo
        self._hex_info = tk.StringVar(value="")
        tk.Label(f, textvariable=self._hex_info, bg=C["bg2"],
                 fg=C["text2"], font=FONT_SMALL, anchor="w").pack(fill="x", padx=10, pady=2)

        # Área HEX
        frame, self._hex_txt = scrolled_text(f)
        frame.pack(fill="both", expand=True, padx=8, pady=(0,8))

        # Colorear texto del hex
        self._hex_txt.tag_configure("offset", foreground=C["hex_off"])
        self._hex_txt.tag_configure("hex",    foreground=C["hex_fg"])
        self._hex_txt.tag_configure("ascii",  foreground=C["hex_asc"])
        self._hex_txt.tag_configure("sig",    foreground=C["warn"], font=("Consolas",9,"bold"))

        if self._hex_path:
            self._hex_reload()

    def _hex_goto(self):
        try:
            v = self._hex_off_var.get().strip()
            self._hex_off = int(v, 16 if v.startswith("0x") or any(c in v for c in "abcdef") else 10)
            self._hex_reload()
        except ValueError:
            self._sv.set("Offset inválido.")

    def _hex_nav(self, direction):
        self._hex_off = max(0, self._hex_off + direction * 512)
        self._hex_off_var.set(f"0x{self._hex_off:x}")
        self._hex_reload()

    def _hex_find(self, signature: bytes):
        if not self._hex_path: return
        try:
            with open(self._hex_path, "rb") as fh:
                fh.seek(self._hex_off + 1)
                data = fh.read(min(2 * 1024 * 1024, 4 * 1024 * 1024))
            pos = data.find(signature)
            if pos >= 0:
                self._hex_off = self._hex_off + 1 + pos
                self._hex_off_var.set(f"0x{self._hex_off:x}")
                self._hex_reload()
            else:
                self._sv.set(f"Firma {signature.hex()} no encontrada desde offset actual.")
        except Exception as ex:
            self._sv.set(f"Error: {ex}")

    def _hex_reload(self):
        if not hasattr(self, "_hex_txt") or not self._hex_path:
            return
        result = read_hex_region(self._hex_path, self._hex_off, 512)
        if result["error"]:
            self._sv.set(f"Error HEX: {result['error']}"); return

        size = os.path.getsize(self._hex_path)
        self._hex_info.set(
            f"  {os.path.basename(self._hex_path)}  ·  "
            f"Tamaño: {fmt_sz(size)}  ·  "
            f"Offset: 0x{result['offset']:x}  ·  "
            f"Mostrando {result['length']} bytes"
        )
        self._hex_txt.config(state="normal")
        self._hex_txt.delete("1.0","end")

        # Insertar hex con colores por columnas
        SIGS = [b"OFNI", b"RATS", b"\x00\x00\x01\xba", b"\x00\x00\x01\xbb",
                b"\x00\x00\x01\xe0", b"\x00\x00\x01\xc0", b"ftyp", b"moov"]
        raw  = result["raw"]

        for i in range(0, len(raw), 16):
            line_off   = result["offset"] + i
            chunk      = raw[i:i+16]
            hex_bytes  = " ".join(f"{b:02x}" for b in chunk)
            hex_bytes += "   " * (16 - len(chunk))
            h1, h2     = hex_bytes[:23], hex_bytes[24:47]
            ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)

            self._hex_txt.insert("end", f"{line_off:08x}  ", "offset")
            self._hex_txt.insert("end", f"{h1}  {h2}  ", "hex")
            self._hex_txt.insert("end", f"|{ascii_part}|\n", "ascii")

        self._hex_txt.config(state="disabled")

    # ── TAB: JSON ─────────────────────────────────────────────────────────────

    def _build_json(self, report):
        f = self._tabs["json"]
        for w in f.winfo_children(): w.destroy()

        tb = tk.Frame(f, bg=C["bg2"]); tb.pack(fill="x", padx=8, pady=6)
        tk.Label(tb, text="JSON del escaneo completo", bg=C["bg2"],
                 fg=C["text2"], font=FONT_SMALL).pack(side="left")
        btn(tb, "📋 Copiar", self._copy_json, "normal").pack(side="right", padx=4)

        frame, self._jtxt = scrolled_text(f)
        self._jtxt.config(fg=C["accent2"])
        frame.pack(fill="both", expand=True, padx=8, pady=(0,8))

        def clean(o):
            if isinstance(o, dict):  return {k:clean(v) for k,v in o.items() if k!="best"}
            if isinstance(o, list):  return [clean(x) for x in o]
            return o
        j = json.dumps(clean(report), indent=2, ensure_ascii=False, default=str)
        self._jtxt.config(state="normal")
        self._jtxt.delete("1.0","end")
        self._jtxt.insert("1.0", j)
        self._jtxt.config(state="disabled")

    def _copy_json(self):
        if hasattr(self, "_jtxt"):
            self.clipboard_clear()
            self.clipboard_append(self._jtxt.get("1.0","end"))
            self._sv.set("JSON copiado al portapapeles.")

    def _export_json(self):
        if not self._report:
            messagebox.showinfo("Sin datos","Escaneá una carpeta primero."); return
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON","*.json"),("Todos","*.*")],
            initialfile="sd_report.json")
        if not path: return

        def clean(o):
            if isinstance(o, dict):  return {k:clean(v) for k,v in o.items() if k!="best"}
            if isinstance(o, list):  return [clean(x) for x in o]
            return o
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(clean(self._report), fh, indent=2, ensure_ascii=False, default=str)
        self._sv.set(f"JSON exportado: {path}")
        messagebox.showinfo("Exportado", f"Guardado en:\n{path}")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()
