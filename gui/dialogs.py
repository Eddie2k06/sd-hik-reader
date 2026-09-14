#!/usr/bin/env python3
"""
gui/dialogs.py

Ventanas secundarias:
- FormatDialog: formateo de bajo nivel de la tarjeta SD (con confirmación
  explícita y ventana de progreso por cluster).
- HashDialog: cálculo de hash (SHA-256/SHA-1/MD5/SHA-512/CRC32) de una
  carpeta o archivo, en 4 pasos (elegir carpeta, confirmar, opciones,
  progreso), con generación de informe .txt y/o .pdf con cadena de
  custodia.
- show_about: ventana "Acerca de".
"""

import logging
import os
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import ttk, filedialog, messagebox
from typing import List, Optional

from core import config
from core import deps_check
from core import drives as drives_mod
from core import ffmpeg_setup

log = logging.getLogger("ezviz_reader.dialogs")
from core.formatter import (
    DEFAULT_CLUSTER_SIZE, FormatCancelled, FormatError, FormatProgress,
    format_eta_text, format_speed_text, low_level_format,
)
from core.hashing import (
    ALL_ALGORITHMS, DEFAULT_EXCLUDE_SUFFIXES, HashCancelled, HashProgress,
    ReportlabInstallError, build_hash_entries, collect_files, count_subfolders,
    install_reportlab, write_hash_report, write_hash_report_pdf,
)


def center_window(win, parent=None):
    """Centra una ventana Toplevel sobre `parent` (o sobre la pantalla
    si no hay parent). Debe llamarse después de armar todo el
    contenido de la ventana, para que winfo_width/height ya sean
    los reales (usamos update_idletasks para forzar el cálculo)."""
    win.update_idletasks()
    w = win.winfo_width()
    h = win.winfo_height()
    x = y = None
    if parent is not None:
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            x = px + (pw - w) // 2
            y = py + (ph - h) // 2
        except tk.TclError:
            pass
    if x is None:
        x = (win.winfo_screenwidth() - w) // 2
        y = (win.winfo_screenheight() - h) // 2
    win.geometry(f"+{max(x, 0)}+{max(y, 0)}")

APP_NAME = "Lector de Tarjetas SD (EZVIZ / Hikvision)"
APP_VERSION = "3.1"

PATTERNS = {
    "Ceros (0x00)": b'\x00',
    "Unos (0xFF)": b'\xFF',
}

# -- Paleta compartida (misma que el mockup HTML) --------------------------
C_PAGE = "#e8e8e8"
C_PANEL = "#ffffff"
C_BORDER = "#c9c9c9"
C_BORDER_STRONG = "#adadad"
C_TEXT = "#1a1a1a"
C_MUTED = "#5c5c5c"
C_ACCENT = "#0078d4"
C_ACCENT_DARK = "#005a9e"
C_WARN_BG = "#fff4ce"
C_WARN_BORDER = "#ffb900"
C_DANGER = "#c42b1c"
C_DANGER_BG = "#fde7e9"
C_OK = "#2e8b3d"
C_OK_BG = "#e6f4ea"
C_DONE = "#3a4a5c"
C_CARD_BG = "#fafafa"
C_FREE_BAR = "#dcdcdc"

FONT_BASE = ('Segoe UI', 9)
FONT_BOLD = ('Segoe UI', 9, 'bold')
FONT_SMALL = ('Segoe UI', 8)
FONT_TITLE = ('Segoe UI', 11, 'bold')
FONT_SECTION = ('Segoe UI', 8, 'bold')


def _badge(parent, text, kind='ok'):
    colors = {
        'ok': (C_OK_BG, C_OK),
        'warn': (C_WARN_BG, "#8a6d00"),
        'off': ("#eeeeee", C_MUTED),
    }
    bg, fg = colors.get(kind, colors['ok'])
    lbl = tk.Label(parent, text=text, bg=bg, fg=fg, font=FONT_SMALL,
                    padx=8, pady=2)
    return lbl


def _card(parent):
    """Tarjeta con fondo gris claro y borde fino, igual que .card del mockup."""
    outer = tk.Frame(parent, bg=C_BORDER, padx=1, pady=1)
    inner = tk.Frame(outer, bg=C_CARD_BG, padx=12, pady=10)
    inner.pack(fill='x')
    return outer, inner


def _kv_row(card, label, value_widget_or_text):
    """Arma una fila 'etiqueta ... valor' como .kv del mockup.
    Devuelve el widget de valor (Label) para poder actualizarlo después
    con .config(text=...), salvo que se haya pasado un widget propio."""
    row = tk.Frame(card, bg=C_CARD_BG)
    row.pack(fill='x', pady=2)
    tk.Label(row, text=label, bg=C_CARD_BG, fg=C_MUTED, font=FONT_BASE).pack(side='left')
    if isinstance(value_widget_or_text, str):
        value_lbl = tk.Label(row, text=value_widget_or_text, bg=C_CARD_BG, fg=C_TEXT,
                              font=FONT_BOLD)
        value_lbl.pack(side='right')
        return value_lbl
    value_widget_or_text.pack(side='right')
    return value_widget_or_text


def _section_title(parent, text):
    tk.Label(parent, text=text.upper(), bg=parent['bg'] if 'bg' in parent.keys() else C_PAGE,
              fg=C_MUTED, font=FONT_SECTION).pack(anchor='w', pady=(0, 4))


_styles_ready = False


def _ensure_styles():
    """Configura los estilos ttk usados en toda la app (ventana
    principal + diálogos): tema 'clam' (permite recolorear widgets,
    a diferencia del tema nativo de Windows que ignora 'background')
    y el mismo gris C_PAGE para todos los contenedores (TFrame,
    TLabelframe, TNotebook, etc.), para que no se note un gris
    distinto entre la ventana principal y estas ventanas secundarias.
    Se llama una sola vez por proceso, lo antes posible (desde
    main.py, antes de armar la ventana principal)."""
    global _styles_ready
    if _styles_ready:
        return
    style = ttk.Style()
    try:
        style.theme_use('clam')
    except tk.TclError:
        pass

    # Contenedores: mismo gris de fondo que usan los tk.Frame/tk.Label
    # "planos" de estos diálogos (bg=C_PAGE a mano), para que la
    # ventana principal (que usa ttk.Frame/ttk.LabelFrame/ttk.Notebook)
    # quede pareja con ellos.
    for widget_style in (
        'TFrame', 'TLabelframe', 'TLabelframe.Label', 'TNotebook',
        'TNotebook.Tab', 'TPanedwindow', 'TLabel', 'TCheckbutton',
        'TRadiobutton',
    ):
        style.configure(widget_style, background=C_PAGE)
    style.map('TNotebook.Tab', background=[('selected', C_PAGE)])

    style.configure('Accent.TButton', background=C_ACCENT, foreground='#ffffff',
                     font=FONT_BOLD, padding=6)
    style.map('Accent.TButton',
              background=[('active', C_ACCENT_DARK), ('disabled', '#9fc8e8')])

    style.configure('Danger.TButton', background=C_DANGER, foreground='#ffffff',
                     font=FONT_BOLD, padding=6)
    style.map('Danger.TButton',
              background=[('active', '#a52218'), ('disabled', '#e6a9a2')])

    _styles_ready = True


# ==========================================================================
# Ver: información de la tarjeta SD
# ==========================================================================
class DriveInfoDialog(tk.Toplevel):
    """Ventana "Ver": info del volumen (tipo, sistema de archivos, espacio
    usado/libre) + info del índice EZVIZ si ya se cargó la tarjeta."""

    def __init__(self, parent, app):
        super().__init__(parent)
        _ensure_styles()
        self.app = app
        self.title("Información de la tarjeta SD")
        self.configure(bg=C_PAGE)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.container = tk.Frame(self, bg=C_PAGE, padx=18, pady=14)
        self.container.pack(fill='both', expand=True)

        self._build()
        center_window(self, parent)

    def _build(self):
        for w in self.container.winfo_children():
            w.destroy()

        path = self.app.path_var.get().strip()
        info = drives_mod.describe_path(path)

        # -- Sección Volumen --
        _section_title(self.container, "Volumen")
        outer, card = _card(self.container)
        outer.pack(fill='x', pady=(0, 14))

        _kv_row(card, "Destino", path or "(sin seleccionar)")

        if not info.exists:
            _kv_row(card, "Estado", _badge(card, "⚠ Ruta no encontrada", 'warn'))
        else:
            _kv_row(card, "Tipo de unidad", info.type_label)
            _kv_row(card, "Sistema de archivos", info.filesystem)
            _kv_row(card, "Tamaño total",
                    f"{info.total_bytes / (1024**3):.1f} GB ({info.total_bytes:,} bytes)")

            # barra usado/libre
            bar_wrap = tk.Frame(card, bg=C_CARD_BG)
            bar_wrap.pack(fill='x', pady=(8, 2))
            bar_track = tk.Frame(bar_wrap, bg=C_BORDER_STRONG, height=18)
            bar_track.pack(fill='x')
            bar_track.pack_propagate(False)
            if info.total_bytes:
                used_w = max(1, round(info.used_pct))
                used_frame = tk.Frame(bar_track, bg=C_ACCENT, width=used_w)
                used_frame.place(relx=0, rely=0, relwidth=info.used_pct / 100, relheight=1)
                free_frame = tk.Frame(bar_track, bg=C_FREE_BAR)
                free_frame.place(relx=info.used_pct / 100, rely=0,
                                  relwidth=1 - info.used_pct / 100, relheight=1)

            legend = tk.Frame(card, bg=C_CARD_BG)
            legend.pack(fill='x', pady=(4, 0))
            tk.Label(legend, text=f"■ Usado: {info.used_bytes / (1024**3):.1f} GB ({info.used_pct:.0f}%)",
                      bg=C_CARD_BG, fg=C_ACCENT, font=FONT_SMALL).pack(side='left')
            tk.Label(legend, text=f"■ Libre: {info.free_bytes / (1024**3):.1f} GB ({100 - info.used_pct:.0f}%)",
                      bg=C_CARD_BG, fg=C_MUTED, font=FONT_SMALL).pack(side='right')

        # -- Sección Índice EZVIZ --
        _section_title(self.container, "Índice EZVIZ / Hikvision")
        outer2, card2 = _card(self.container)
        outer2.pack(fill='x', pady=(0, 14))

        if self.app.parser is not None:
            serial = self.app.info_labels['Serial'].cget('text')
            mac = self.app.info_labels['MAC'].cget('text')
            data_dirs = self.app.info_labels['DataDirs'].cget('text')
            archivos = self.app.info_labels['Archivos'].cget('text')
            segmentos = self.app.info_labels['Segmentos'].cget('text')

            _kv_row(card2, "Estado", _badge(card2, "✓ Índice cargado", 'ok'))
            _kv_row(card2, "Serial", serial)
            _kv_row(card2, "MAC", mac)
            _kv_row(card2, "DataDirs", data_dirs)
            _kv_row(card2, "Archivos", archivos)
            _kv_row(card2, "Segmentos", segmentos)
        elif info.exists and info.has_ezviz_index:
            _kv_row(card2, "Estado", _badge(card2, "✓ Índice detectado (sin cargar)", 'ok'))
            note = tk.Label(
                card2, bg=C_CARD_BG, fg=C_MUTED, font=FONT_SMALL, justify='left',
                text="Se encontró info.bin / index00.bin en esta carpeta.\n"
                     "Tocá \"🔍 Cargar\" para ver Serial, MAC y segmentos.")
            note.pack(anchor='w', pady=(4, 0))
        else:
            _kv_row(card2, "Estado", _badge(card2, "✕ Sin índice detectado", 'off'))

        # -- Footer --
        footer = tk.Frame(self.container, bg=C_PAGE)
        footer.pack(fill='x', pady=(6, 0))
        ttk.Separator(footer).pack(fill='x', pady=(0, 10))
        btns = tk.Frame(footer, bg=C_PAGE)
        btns.pack(anchor='e')
        ttk.Button(btns, text="🔄 Actualizar", command=self._build).pack(side='left', padx=(0, 8))
        ttk.Button(btns, text="Cerrar", command=self.destroy).pack(side='left')


# ==========================================================================
# Ver: mapa de segmentos (mockup v3 — grilla moderna agrupada)
# ==========================================================================
# -- Paleta de estados de slot (misma que el mockup HTML: mapa-segmentos v3)
C_SLOT_FREE = "#f4f5f6"
C_SLOT_VIDEO = "#0078d4"
C_SLOT_ALARM = "#f2994a"
C_SLOT_CORRUPT = "#b7bcc2"
C_GROUP_BG = "#eceff1"
C_MAP_BG = "#d3d6d8"

SLOT_STATE_LABELS = {
    'free': 'libre',
    'video': 'video grabado',
    'alarm': 'evento / alarma',
    'corrupt': 'registro corrupto',
}
SLOT_STATE_COLORS = {
    'free': C_SLOT_FREE,
    'video': C_SLOT_VIDEO,
    'alarm': C_SLOT_ALARM,
    'corrupt': C_SLOT_CORRUPT,
}


class SegmentMapDialog(tk.Toplevel):
    """Ventana "Ver > Mapa de segmentos": réplica funcional del mockup v3
    (grilla moderna agrupada en bloques de 32 slots + resumen de todos
    los avFiles con barras de ocupación). Usa EZVIZIndexParser.get_segment_map().
    """

    GROUP_SIZE = 32
    CELL = 13
    GAP = 3
    GROUP_COLS = 8       # slots por fila dentro de un bloque
    GROUPS_PER_ROW = 4   # bloques por fila del mapa

    OVERVIEW_ROW_H = 20
    OVERVIEW_W = 560
    OVERVIEW_TRACK_X0 = 96
    OVERVIEW_TRACK_X1 = 500

    def __init__(self, parent, app):
        super().__init__(parent)
        _ensure_styles()
        self.app = app
        self.title("Información de la tarjeta SD — Mapa de índice")
        self.configure(bg=C_PAGE)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self._tooltip = None
        self._current_index = 0
        self._flat_files = []
        self._map_data = {}
        self._step = 'overview'  # 'overview' | 'single'

        self.container = tk.Frame(self, bg=C_PAGE, padx=18, pady=14)
        self.container.pack(fill='both', expand=True)

        self._load_map()
        self._build()
        center_window(self, parent)

    # -- datos -----------------------------------------------------------
    def _load_map(self):
        self._flat_files = []
        self._map_data = {}
        if self.app.parser is None:
            return
        try:
            self._map_data = self.app.parser.get_segment_map()
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo construir el mapa de segmentos:\n{e}",
                                  parent=self)
            self._map_data = {}
        for index_dir_num, files in sorted(self._map_data.items()):
            for finfo in files:
                self._flat_files.append(finfo)
        if self._flat_files:
            self._current_index = min(self._current_index, len(self._flat_files) - 1)

    # -- layout general ----------------------------------------------------
    def _build(self):
        for w in self.container.winfo_children():
            w.destroy()
        self._hide_tip()

        if self.app.parser is None:
            tk.Label(self.container, bg=C_PAGE, fg=C_MUTED, font=FONT_BASE, justify='left',
                      wraplength=520,
                      text="Todavía no se cargó ningún índice EZVIZ/Hikvision.\n"
                           "Elegí la carpeta de la tarjeta SD y tocá \"🔍 Cargar\" antes de "
                           "abrir el mapa de segmentos.").pack(pady=30)
            footer = tk.Frame(self.container, bg=C_PAGE)
            footer.pack(fill='x', pady=(6, 0))
            ttk.Button(footer, text="Cerrar", command=self.destroy).pack(anchor='e')
            return

        if not self._flat_files:
            tk.Label(self.container, bg=C_PAGE, fg=C_MUTED, font=FONT_BASE,
                      text="El índice cargado no tiene avFiles.").pack(pady=30)
            ttk.Button(self.container, text="Cerrar", command=self.destroy).pack(anchor='e')
            return

        # -- switcher --
        switcher = tk.Frame(self.container, bg=C_PAGE)
        switcher.pack(fill='x', pady=(0, 14))
        self.btn_single = self._pill_button(
            switcher, "1 · Un archivo (agrupado)", self._step == 'single',
            lambda: self._switch_step('single'))
        self.btn_single.pack(side='left', padx=(0, 8))
        self.btn_overview = self._pill_button(
            switcher, "2 · Resumen de todos los archivos", self._step == 'overview',
            lambda: self._switch_step('overview'))
        self.btn_overview.pack(side='left')

        if self._step == 'single':
            self._build_step_single()
        else:
            self._build_step_overview()

        # -- footer --
        footer = tk.Frame(self.container, bg=C_PAGE)
        footer.pack(fill='x', pady=(10, 0))
        ttk.Separator(footer).pack(fill='x', pady=(0, 10))
        btns = tk.Frame(footer, bg=C_PAGE)
        btns.pack(anchor='e')
        ttk.Button(btns, text="🔄 Actualizar", command=self._reload).pack(side='left', padx=(0, 8))
        ttk.Button(btns, text="Cerrar", command=self.destroy).pack(side='left')

    def _reload(self):
        self._load_map()
        self._build()

    def _pill_button(self, parent, text, active, command):
        bg = C_ACCENT if active else C_PANEL
        fg = "#ffffff" if active else C_TEXT
        return tk.Button(
            parent, text=text, bg=bg, fg=fg, activebackground=C_ACCENT_DARK,
            activeforeground="#ffffff", font=FONT_SMALL, relief='flat', bd=0,
            padx=12, pady=6, cursor='hand2', command=command)

    def _switch_step(self, step):
        self._step = step
        self._build()

    # ======================================================================
    # PASO 1: mapa agrupado de un avFile
    # ======================================================================
    def _build_step_single(self):
        finfo = self._flat_files[self._current_index]

        # -- selector de archivo --
        picker = tk.Frame(self.container, bg=C_PAGE)
        picker.pack(fill='x', pady=(0, 10))
        ttk.Button(picker, text="‹", width=3, command=self._prev_file).pack(side='left')
        tk.Label(picker, text="Archivo:", bg=C_PAGE, fg=C_MUTED, font=FONT_BASE).pack(
            side='left', padx=(8, 6))

        self.file_var = tk.StringVar(value=self._file_label(finfo))
        combo = ttk.Combobox(
            picker, textvariable=self.file_var, state='readonly',
            values=[self._file_label(f) for f in self._flat_files], width=48)
        combo.pack(side='left', fill='x', expand=True)
        combo.bind('<<ComboboxSelected>>', lambda e: self._select_file_by_label(combo.get()))

        ttk.Button(picker, text="›", width=3, command=self._next_file).pack(side='left', padx=(6, 0))

        # -- título de sección --
        title_row = tk.Frame(self.container, bg=C_PAGE)
        title_row.pack(fill='x', pady=(4, 9))
        tk.Label(title_row, text="MAPA DE SEGMENTOS", bg=C_PAGE, fg=C_MUTED,
                  font=FONT_SECTION).pack(side='left')
        tk.Label(title_row, text=f"{finfo['used']} / {self.app.parser.MAX_SEGMENTS} ocupados",
                  bg=C_PAGE, fg=C_TEXT, font=FONT_SECTION).pack(side='right')

        # -- grilla agrupada --
        map_outer = tk.Frame(self.container, bg=C_BORDER_STRONG, padx=1, pady=1)
        map_outer.pack(pady=(0, 10))
        map_inner = tk.Frame(map_outer, bg=C_MAP_BG, padx=10, pady=10)
        map_inner.pack()

        total_slots = self.app.parser.MAX_SEGMENTS
        num_groups = total_slots // self.GROUP_SIZE
        group_rows = num_groups // self.GROUPS_PER_ROW
        group_w = self.GROUP_COLS * (self.CELL + self.GAP) - self.GAP + 12
        group_h = (self.GROUP_SIZE // self.GROUP_COLS) * (self.CELL + self.GAP) - self.GAP + 26
        group_gap = 8

        canvas_w = self.GROUPS_PER_ROW * (group_w + group_gap) - group_gap
        canvas_h = group_rows * (group_h + group_gap) - group_gap

        self.map_canvas = tk.Canvas(map_inner, width=canvas_w, height=canvas_h,
                                     bg=C_MAP_BG, highlightthickness=0)
        self.map_canvas.pack()

        slots = finfo['slots']
        rows_per_group = self.GROUP_SIZE // self.GROUP_COLS
        for g in range(num_groups):
            gc, gr = g % self.GROUPS_PER_ROW, g // self.GROUPS_PER_ROW
            gx = gc * (group_w + group_gap)
            gy = gr * (group_h + group_gap)
            self.map_canvas.create_rectangle(
                gx, gy, gx + group_w, gy + group_h, fill=C_GROUP_BG, outline='')
            start_slot, end_slot = g * self.GROUP_SIZE, g * self.GROUP_SIZE + self.GROUP_SIZE - 1
            self.map_canvas.create_text(
                gx + 6, gy + 8, anchor='w', text=f"Bloque {g}", fill=C_MUTED,
                font=('Segoe UI', 7, 'bold'))
            self.map_canvas.create_text(
                gx + group_w - 6, gy + 8, anchor='e', text=f"#{start_slot}–{end_slot}",
                fill=C_MUTED, font=('Segoe UI', 7, 'bold'))

            for j in range(self.GROUP_SIZE):
                i = g * self.GROUP_SIZE + j
                r, c = divmod(j, self.GROUP_COLS)
                x0 = gx + 6 + c * (self.CELL + self.GAP)
                y0 = gy + 18 + r * (self.CELL + self.GAP)
                x1, y1 = x0 + self.CELL, y0 + self.CELL
                state = slots[i]['state']
                color = SLOT_STATE_COLORS[state]
                rect = self.map_canvas.create_rectangle(x0, y0, x1, y1, fill=color, outline='')
                if i == finfo['first_free']:
                    self.map_canvas.create_rectangle(
                        x0 - 1, y0 - 1, x1 + 1, y1 + 1, outline='#ffffff', width=2)
                seg = slots[i]['segment']
                tip = f"Slot #{i} — {SLOT_STATE_LABELS[state]}"
                if seg is not None:
                    tip += (f"\n{seg.startTimeDt:%Y-%m-%d %H:%M:%S} UTC "
                            f"({seg.duration:.0f}s)")
                self.map_canvas.tag_bind(
                    rect, '<Enter>', lambda e, t=tip: self._show_tip(t, e.x_root, e.y_root))
                self.map_canvas.tag_bind(rect, '<Leave>', lambda e: self._hide_tip())

        # -- leyenda --
        legend = tk.Frame(self.container, bg=C_PAGE)
        legend.pack(fill='x', pady=(0, 14))
        for state in ('free', 'video', 'alarm', 'corrupt'):
            item = tk.Frame(legend, bg=C_PAGE)
            item.pack(side='left', padx=(0, 16))
            tk.Canvas(item, width=10, height=10, bg=SLOT_STATE_COLORS[state],
                       highlightthickness=1, highlightbackground=C_BORDER_STRONG
                       ).pack(side='left', padx=(0, 5))
            labels = {'free': 'Libre', 'video': 'Video grabado',
                      'alarm': 'Evento / alarma', 'corrupt': 'Corrupto / ilegible'}
            tk.Label(item, text=labels[state], bg=C_PAGE, fg=C_MUTED,
                      font=FONT_SMALL).pack(side='left')

        # -- anillo de ocupación + mini stats --
        stat_row = tk.Frame(self.container, bg=C_PAGE)
        stat_row.pack(fill='x')

        pct = finfo['used'] / total_slots if total_slots else 0
        ring = tk.Canvas(stat_row, width=64, height=64, bg=C_PAGE, highlightthickness=0)
        ring.pack(side='left', padx=(0, 16))
        ring.create_oval(6, 6, 58, 58, outline="#e1e3e5", width=8)
        if pct > 0:
            ring.create_arc(6, 6, 58, 58, start=90, extent=-360 * pct,
                             style='arc', outline=C_ACCENT, width=8)
        ring.create_text(32, 32, text=f"{pct*100:.0f}%", font=FONT_BOLD, fill=C_TEXT)

        mini = tk.Frame(stat_row, bg=C_PAGE)
        mini.pack(side='left', anchor='w')
        first_free_txt = f"#{finfo['first_free']}" if finfo['first_free'] is not None else "— (lleno)"
        for txt in (
            f"Slots usados: {finfo['used']} / {total_slots}",
            f"Primer libre: {first_free_txt}",
            f"Tamaño de slot: {self.app.parser.SEGMENT_LEN} bytes — grupo: {self.GROUP_SIZE} slots",
        ):
            tk.Label(mini, text=txt, bg=C_PAGE, fg=C_MUTED, font=FONT_SMALL,
                      justify='left', anchor='w').pack(anchor='w', pady=1)

    def _file_label(self, finfo):
        multi = len(self._map_data) > 1
        prefix = f"datadir{finfo['index_dir_num']}: " if multi else ""
        return (f"{prefix}avFile #{finfo['file_num']:04d} — "
                f"{finfo['used']}/{self.app.parser.MAX_SEGMENTS} ocupados "
                f"— de {len(self._flat_files)} archivos totales")

    def _select_file_by_label(self, label):
        for i, f in enumerate(self._flat_files):
            if self._file_label(f) == label:
                self._current_index = i
                break
        self._build()

    def _prev_file(self):
        if self._current_index > 0:
            self._current_index -= 1
            self._build()

    def _next_file(self):
        if self._current_index < len(self._flat_files) - 1:
            self._current_index += 1
            self._build()

    # -- tooltip flotante --------------------------------------------------
    def _show_tip(self, text, x_root, y_root):
        self._hide_tip()
        tw = tk.Toplevel(self)
        tw.wm_overrideredirect(True)
        tw.attributes('-topmost', True)
        tw.wm_geometry(f"+{x_root + 14}+{y_root + 14}")
        tk.Label(tw, text=text, bg="#1a1a1a", fg="#ffffff", font=FONT_SMALL,
                  padx=7, pady=4, justify='left').pack()
        self._tooltip = tw

    def _hide_tip(self):
        if self._tooltip is not None:
            try:
                self._tooltip.destroy()
            except tk.TclError:
                pass
            self._tooltip = None

    # ======================================================================
    # PASO 2: resumen de todos los avFiles
    # ======================================================================
    def _build_step_overview(self):
        MAX_SEGMENTS = self.app.parser.MAX_SEGMENTS
        total_avfiles = len(self._flat_files)
        total_capacity = total_avfiles * MAX_SEGMENTS
        total_segments = sum(f['used'] for f in self._flat_files)
        fully_free = sum(1 for f in self._flat_files if f['used'] == 0)
        fullest = max(self._flat_files, key=lambda f: f['used'])

        # -- card índice --
        tk.Label(self.container, text="ÍNDICE EZVIZ / HIKVISION", bg=C_PAGE, fg=C_MUTED,
                  font=FONT_SECTION).pack(anchor='w', pady=(0, 6))
        outer, card = _card(self.container)
        outer.pack(fill='x', pady=(0, 14))
        _kv_row(card, "avFiles", str(total_avfiles))
        _kv_row(card, "Slots por archivo (MAX_SEGMENTS)", str(MAX_SEGMENTS))
        _kv_row(card, "Capacidad total del índice", f"{total_capacity:,} slots")
        _kv_row(card, "Segmentos válidos leídos", f"{total_segments:,}")

        # -- título ocupación --
        title_row = tk.Frame(self.container, bg=C_PAGE)
        title_row.pack(fill='x', pady=(0, 6))
        tk.Label(title_row, text="OCUPACIÓN POR ARCHIVO", bg=C_PAGE, fg=C_MUTED,
                  font=FONT_SECTION).pack(side='left')
        tk.Label(title_row, text="tocá una fila para abrir su mapa", bg=C_PAGE, fg=C_TEXT,
                  font=FONT_SECTION).pack(side='right')

        # -- lista scrollable dibujada en canvas --
        list_outer = tk.Frame(self.container, bg=C_BORDER, padx=1, pady=1)
        list_outer.pack(fill='x', pady=(0, 4))
        list_inner = tk.Frame(list_outer, bg=C_CARD_BG)
        list_inner.pack(fill='both', expand=True)

        view_h = min(len(self._flat_files) * self.OVERVIEW_ROW_H, 170)
        self.overview_canvas = tk.Canvas(list_inner, width=self.OVERVIEW_W, height=view_h,
                                          bg=C_CARD_BG, highlightthickness=0)
        vsb = ttk.Scrollbar(list_inner, orient='vertical', command=self.overview_canvas.yview)
        self.overview_canvas.configure(yscrollcommand=vsb.set)
        self.overview_canvas.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')

        self._draw_overview_rows()

        def _wheel(event):
            delta = -1 * int(event.delta / 120) if getattr(event, 'delta', 0) else \
                (-1 if event.num == 4 else 1)
            self.overview_canvas.yview_scroll(delta, 'units')
        self.overview_canvas.bind('<MouseWheel>', _wheel)
        self.overview_canvas.bind('<Button-4>', _wheel)
        self.overview_canvas.bind('<Button-5>', _wheel)

        # -- mini stats --
        stat_row = tk.Frame(self.container, bg=C_PAGE)
        stat_row.pack(fill='x', pady=(10, 0))
        mini = tk.Frame(stat_row, bg=C_PAGE)
        mini.pack(side='left', anchor='w')
        pct_fullest = fullest['used'] / MAX_SEGMENTS * 100 if MAX_SEGMENTS else 0
        for txt in (
            f"Archivo más lleno: #{fullest['file_num']:04d} ({pct_fullest:.0f}%)",
            f"Archivos totalmente libres: {fully_free} / {total_avfiles}",
        ):
            tk.Label(mini, text=txt, bg=C_PAGE, fg=C_MUTED, font=FONT_SMALL,
                      justify='left', anchor='w').pack(anchor='w', pady=1)

    def _draw_overview_rows(self):
        c = self.overview_canvas
        c.delete('all')
        row_h = self.OVERVIEW_ROW_H
        x0, x1 = self.OVERVIEW_TRACK_X0, self.OVERVIEW_TRACK_X1
        MAX_SEGMENTS = self.app.parser.MAX_SEGMENTS

        for i, finfo in enumerate(self._flat_files):
            y = i * row_h
            tag = f"row{i}"
            selected = (i == self._current_index and self._step == 'single')
            row_bg = "#e9f3fc" if selected else C_CARD_BG
            bg_tag = f"bg{i}"
            c.create_rectangle(0, y, self.OVERVIEW_W, y + row_h, fill=row_bg, outline='',
                                tags=(tag, bg_tag))

            multi = len(self._map_data) > 1
            fname = (f"d{finfo['index_dir_num']}·#{finfo['file_num']:04d}" if multi
                     else f"#{finfo['file_num']:04d}")
            c.create_text(6, y + row_h / 2, anchor='w', text=fname, fill=C_MUTED,
                           font=FONT_SMALL, tags=(tag,))

            c.create_rectangle(x0, y + 5, x1, y + row_h - 5, fill="#e6e6e6", outline='',
                                tags=(tag,))
            used = finfo['used']
            if used:
                counts = {'video': 0, 'alarm': 0, 'corrupt': 0}
                for s in finfo['slots']:
                    if s['state'] in counts:
                        counts[s['state']] += 1
                cursor_x = x0
                for state in ('video', 'alarm', 'corrupt'):
                    n = counts[state]
                    if not n:
                        continue
                    seg_w = (x1 - x0) * (n / MAX_SEGMENTS)
                    seg_x1 = cursor_x + seg_w
                    c.create_rectangle(cursor_x, y + 5, seg_x1, y + row_h - 5,
                                        fill=SLOT_STATE_COLORS[state], outline='', tags=(tag,))
                    cursor_x = seg_x1

            pct = used / MAX_SEGMENTS * 100 if MAX_SEGMENTS else 0
            c.create_text(x1 + 8, y + row_h / 2, anchor='w', text=f"{pct:.0f}%",
                           fill=C_MUTED, font=FONT_SMALL, tags=(tag,))

            if not selected:
                c.tag_bind(tag, '<Enter>', lambda e, bt=bg_tag: c.itemconfig(bt, fill="#f0f0f0"))
                c.tag_bind(tag, '<Leave>', lambda e, bt=bg_tag: c.itemconfig(bt, fill=C_CARD_BG))
            c.tag_bind(tag, '<Button-1>', lambda e, idx=i: self._select_file_from_overview(idx))

        c.configure(scrollregion=(0, 0, self.OVERVIEW_W, len(self._flat_files) * row_h))

    def _select_file_from_overview(self, idx):
        self._current_index = idx
        self._step = 'single'
        self._build()


# ==========================================================================
# Formateo de bajo nivel
# ==========================================================================
class FormatDialog(tk.Toplevel):
    """Paso 1: elegir destino y parámetros. Paso 2: confirmación
    explícita. Paso 3: progreso."""

    def __init__(self, parent, initial_path: str = ""):
        super().__init__(parent)
        _ensure_styles()
        self.title("Formatear tarjeta SD — Bajo nivel")
        self.configure(bg=C_PAGE)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.cancel_event = threading.Event()
        self._build_step1(initial_path)
        center_window(self, parent)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _fresh_container(self):
        for w in self.winfo_children():
            w.destroy()
        container = tk.Frame(self, bg=C_PAGE, padx=18, pady=14)
        container.pack(fill='both', expand=True)
        return container

    # -- Paso 1: parámetros -------------------------------------------------
    def _build_step1(self, initial_path: str = ""):
        frame = self._fresh_container()

        # Banner de advertencia (igual a .banner del mockup)
        banner_outer = tk.Frame(frame, bg=C_WARN_BORDER, padx=1, pady=1)
        banner_outer.pack(fill='x', pady=(0, 14))
        banner = tk.Frame(banner_outer, bg=C_WARN_BG, padx=10, pady=8)
        banner.pack(fill='x')
        tk.Label(banner, text="⚠️", bg=C_WARN_BG, font=('Segoe UI', 13)).pack(side='left', padx=(0, 8))
        tk.Label(
            banner, bg=C_WARN_BG, fg=C_TEXT, justify='left', font=FONT_SMALL,
            wraplength=430,
            text="Esta operación sobrescribe por completo el contenido del destino "
                 "elegido. Es irreversible. Por seguridad, sólo se puede elegir entre "
                 "las unidades REMOVIBLES detectadas en este equipo: nunca un disco "
                 "fijo, ni una ruta escrita a mano."
        ).pack(side='left', fill='x')

        # Destino: únicamente unidades removibles detectadas (sin campo de
        # texto libre ni "Examinar…", para no correr el riesgo de apuntar
        # por error a un disco fijo del equipo).
        dest_header = tk.Frame(frame, bg=C_PAGE)
        dest_header.pack(fill='x')
        tk.Label(dest_header, text="Unidad removible", bg=C_PAGE, fg=C_TEXT,
                  font=FONT_BASE).pack(side='left')
        ttk.Button(dest_header, text="🔄 Actualizar",
                   command=lambda: self._refresh_drives()).pack(side='right')

        self.drive_var = tk.StringVar()
        self.drive_combo = ttk.Combobox(frame, textvariable=self.drive_var, state='readonly')
        self.drive_combo.pack(fill='x', pady=(4, 2))
        self.drive_combo.bind('<<ComboboxSelected>>', lambda e: self._on_drive_selected())

        self.no_drives_label = tk.Label(
            frame, bg=C_PAGE, fg=C_DANGER, justify='left', font=FONT_SMALL,
            wraplength=470, text="")
        self.no_drives_label.pack(anchor='w')

        tk.Label(
            frame, bg=C_PAGE, fg=C_MUTED, justify='left', font=FONT_SMALL,
            wraplength=470,
            text="Se listan sólo las unidades que el sistema operativo reporta como "
                 "removibles (denominación y tipo). Si no ves la tarjeta, desconectala, "
                 "volvé a conectarla y tocá \"🔄 Actualizar\"."
        ).pack(anchor='w', pady=(2, 12))

        # Cluster + patrón, en fila (igual a .row-inline)
        opts_row = tk.Frame(frame, bg=C_PAGE)
        opts_row.pack(fill='x', pady=(0, 12))

        cluster_col = tk.Frame(opts_row, bg=C_PAGE)
        cluster_col.pack(side='left', fill='x', expand=True, padx=(0, 8))
        tk.Label(cluster_col, text="Tamaño de cluster", bg=C_PAGE, fg=C_TEXT, font=FONT_BASE).pack(anchor='w')
        cluster_inner = tk.Frame(cluster_col, bg=C_PAGE)
        cluster_inner.pack(fill='x', pady=(4, 0))
        self.cluster_var = tk.StringVar(value=str(DEFAULT_CLUSTER_SIZE // 1024))
        ttk.Entry(cluster_inner, textvariable=self.cluster_var, width=8).pack(side='left')
        tk.Label(cluster_inner, text="KiB", bg=C_PAGE, fg=C_MUTED, font=FONT_SMALL).pack(side='left', padx=4)

        pattern_col = tk.Frame(opts_row, bg=C_PAGE)
        pattern_col.pack(side='left', fill='x', expand=True)
        tk.Label(pattern_col, text="Patrón de escritura", bg=C_PAGE, fg=C_TEXT, font=FONT_BASE).pack(anchor='w')
        self.pattern_var = tk.StringVar(value=list(PATTERNS.keys())[0])
        ttk.Combobox(pattern_col, textvariable=self.pattern_var, values=list(PATTERNS.keys()),
                     state='readonly').pack(fill='x', pady=(4, 0))

        # Card de tamaño / tipo de unidad (igual a .card del mockup)
        outer, card = _card(frame)
        outer.pack(fill='x', pady=(0, 4))
        self.size_row = _kv_row(card, "Tamaño detectado", "—")
        self.type_row = _kv_row(card, "Tipo de unidad", "—")
        self._probed_size = 0
        self.target_path = ""

        # Footer
        footer = tk.Frame(frame, bg=C_PAGE)
        footer.pack(fill='x', pady=(16, 0))
        ttk.Separator(footer).pack(fill='x', pady=(0, 10))
        btns = tk.Frame(footer, bg=C_PAGE)
        btns.pack(anchor='e')
        ttk.Button(btns, text="Cancelar", command=self.destroy).pack(side='left', padx=(0, 8))
        self.continue_btn = ttk.Button(
            btns, text="Continuar ›", command=self._go_step2,
            style='Accent.TButton', state='disabled')
        self.continue_btn.pack(side='left')

        self._drives = []
        self._refresh_drives(preselect_path=initial_path)

    def _refresh_drives(self, preselect_path: str = ""):
        """Vuelve a listar las unidades REMOVIBLES conectadas (misma
        detección que usa el menú SD). Nunca incluye discos fijos ni
        permite tipear una ruta a mano."""
        try:
            all_drives = drives_mod.list_available_drives()
        except Exception:
            all_drives = []
        self._drives = [d for d in all_drives if d.is_removable]

        if not self._drives:
            self.drive_combo.configure(values=[])
            self.drive_var.set("")
            self.no_drives_label.config(
                text="⚠ No se detectó ninguna unidad removible conectada al equipo.")
            self.size_row.config(text="—")
            self.type_row.config(text="—")
            self.target_path = ""
            self._probed_size = 0
            self.continue_btn.config(state='disabled')
            return

        self.no_drives_label.config(text="")
        self.drive_combo.configure(values=[d.display_text() for d in self._drives])

        target_idx = 0
        if preselect_path:
            for i, d in enumerate(self._drives):
                if os.path.normpath(d.path) == os.path.normpath(preselect_path):
                    target_idx = i
                    break
        self.drive_combo.current(target_idx)
        self._on_drive_selected()

    def _on_drive_selected(self):
        idx = self.drive_combo.current()
        if idx < 0 or idx >= len(self._drives):
            return
        d = self._drives[idx]
        self.target_path = d.path
        self._probed_size = d.total_bytes
        self.size_row.config(
            text=f"{d.total_bytes / (1024**3):.1f} GB ({d.total_bytes:,} bytes)"
            if d.total_bytes else "No se pudo determinar")
        self.type_row.config(text=f"{d.type_label}  ·  {d.label}")
        self.continue_btn.config(state='normal' if d.total_bytes else 'disabled')

    def _go_step2(self):
        if not self.target_path or not self._drives:
            messagebox.showwarning("Atención", "Elegí una unidad removible primero", parent=self)
            return
        try:
            cluster_kib = int(self.cluster_var.get())
            if cluster_kib <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Error", "Tamaño de cluster inválido", parent=self)
            return

        if not self._probed_size:
            messagebox.showerror(
                "Error", "No se pudo determinar el tamaño de la unidad seleccionada.",
                parent=self)
            return

        self.cluster_size = cluster_kib * 1024
        self.pattern = PATTERNS[self.pattern_var.get()]
        self.total_bytes = self._probed_size

        for w in self.winfo_children():
            w.destroy()
        self._build_step2_confirm()

    # -- Paso 2: confirmación explícita -------------------------------------
    def _build_step2_confirm(self):
        frame = self._fresh_container()

        center = tk.Frame(frame, bg=C_PAGE)
        center.pack(fill='x', pady=(0, 4))
        tk.Label(center, text="⚠️", bg=C_PAGE, fg=C_DANGER, font=('Segoe UI', 26)).pack()
        tk.Label(center, text="CONFIRMACIÓN FINAL", bg=C_PAGE, fg=C_DANGER,
                  font=('Segoe UI', 12, 'bold')).pack(pady=(2, 14))

        # Resumen en tarjeta roja (igual a .confirm-summary)
        summary_outer = tk.Frame(frame, bg="#f3c6c6", padx=1, pady=1)
        summary_outer.pack(fill='x', pady=(0, 14))
        summary = tk.Frame(summary_outer, bg=C_DANGER_BG, padx=12, pady=10)
        summary.pack(fill='x')
        tk.Label(summary, text="Vas a borrar por completo:", bg=C_DANGER_BG, fg=C_TEXT,
                  font=FONT_BOLD).pack(anchor='w', pady=(0, 6))
        for label, value in (
            ("Destino", self.target_path),
            ("Tamaño estimado", f"~{self.total_bytes / (1024**3):.2f} GB"),
            ("Cluster", f"{self.cluster_size // 1024} KiB"),
            ("Patrón", self.pattern_var.get()),
        ):
            row = tk.Frame(summary, bg=C_DANGER_BG)
            row.pack(fill='x', pady=1)
            tk.Label(row, text=label, bg=C_DANGER_BG, fg=C_MUTED, font=FONT_SMALL).pack(side='left')
            tk.Label(row, text=value, bg=C_DANGER_BG, fg=C_TEXT, font=FONT_BOLD).pack(side='right')

        tk.Label(
            frame, bg=C_PAGE, fg=C_MUTED, font=FONT_SMALL,
            text="Escribí FORMATEAR (en mayúsculas) para habilitar el botón"
        ).pack(pady=(0, 6))

        self.confirm_var = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=self.confirm_var, width=20, justify='center',
                           font=FONT_BOLD)
        entry.pack(pady=(0, 16))
        self.confirm_var.trace_add('write', lambda *_: self._check_confirm_text())

        footer = tk.Frame(frame, bg=C_PAGE)
        footer.pack(fill='x')
        ttk.Separator(footer).pack(fill='x', pady=(0, 10))
        btns = tk.Frame(footer, bg=C_PAGE)
        btns.pack(anchor='e')
        ttk.Button(btns, text="‹ Volver", command=self._restart).pack(side='left', padx=(0, 8))
        self.confirm_btn = ttk.Button(
            btns, text="Formatear ahora", state='disabled', command=self._start_format,
            style='Danger.TButton')
        self.confirm_btn.pack(side='left')

    def _check_confirm_text(self):
        self.confirm_btn.config(
            state='normal' if self.confirm_var.get() == "FORMATEAR" else 'disabled')

    def _restart(self):
        self._build_step1(self.target_path)

    # -- Paso 3: progreso (grilla estilo defrag) -----------------------------
    GRID_COLS = 34
    GRID_ROWS = 8
    GRID_CELL = 12
    GRID_GAP = 2
    GRID_ACTIVE_WINDOW = 5

    def _start_format(self):
        frame = self._fresh_container()
        self.title("Formateando... no cierres esta ventana")

        tk.Label(frame, bg=C_PAGE, fg=C_TEXT, font=FONT_BOLD, anchor='w',
                  text=f"Formateando  —  {self.target_path}  (no cierres esta ventana)"
                  ).pack(anchor='w', pady=(0, 10))

        # -- grilla defrag --
        cols, rows, cell, gap = self.GRID_COLS, self.GRID_ROWS, self.GRID_CELL, self.GRID_GAP
        canvas_w = cols * (cell + gap) - gap
        canvas_h = rows * (cell + gap) - gap

        grid_outer = tk.Frame(frame, bg=C_BORDER_STRONG, padx=1, pady=1)
        grid_outer.pack(pady=(0, 10))
        grid_frame = tk.Frame(grid_outer, bg="#d8d8d8", padx=6, pady=6)
        grid_frame.pack()
        self.grid_canvas = tk.Canvas(grid_frame, width=canvas_w, height=canvas_h,
                                       bg="#d8d8d8", highlightthickness=0)
        self.grid_canvas.pack()

        self.total_cells = cols * rows
        self._cell_ids = []
        for i in range(self.total_cells):
            r, c = divmod(i, cols)
            x0 = c * (cell + gap)
            y0 = r * (cell + gap)
            rect = self.grid_canvas.create_rectangle(
                x0, y0, x0 + cell, y0 + cell, fill='#ffffff', width=0)
            self._cell_ids.append(rect)

        # -- leyenda --
        legend = tk.Frame(frame, bg=C_PAGE)
        legend.pack(fill='x', pady=(0, 12))
        for color, text in (('#ffffff', 'Pendiente'), (C_ACCENT, 'Escribiendo'), (C_DONE, 'Formateado')):
            item = tk.Frame(legend, bg=C_PAGE)
            item.pack(side='left', padx=(0, 16))
            tk.Canvas(item, width=9, height=9, bg=color, highlightthickness=1,
                       highlightbackground=C_BORDER_STRONG).pack(side='left', padx=(0, 5))
            tk.Label(item, text=text, bg=C_PAGE, fg=C_MUTED, font=FONT_SMALL).pack(side='left')

        # -- mini stats --
        stats = tk.Frame(frame, bg=C_PAGE)
        stats.pack(fill='x', pady=(0, 4))
        self.stat_written = tk.StringVar(value="0.0 GB escritos")
        self.stat_speed = tk.StringVar(value="0.0 MB/s")
        self.stat_eta = tk.StringVar(value="ETA —")
        self.stat_cluster = tk.StringVar(value="Cluster 0 / 0")
        for var in (self.stat_written, self.stat_speed, self.stat_eta, self.stat_cluster):
            tk.Label(stats, textvariable=var, bg=C_PAGE, fg=C_MUTED, font=FONT_SMALL
                      ).pack(side='left', padx=(0, 18))

        # -- footer: barra + cancelar, misma altura (igual a .footer-progress) --
        footer = tk.Frame(frame, bg=C_PAGE)
        footer.pack(fill='x', pady=(10, 0))
        ttk.Separator(footer).pack(fill='x', pady=(0, 10))

        bar_row = tk.Frame(footer, bg=C_PAGE)
        bar_row.pack(fill='x')

        bar_outer = tk.Frame(bar_row, bg=C_BORDER_STRONG, padx=1, pady=1)
        bar_outer.pack(side='left', fill='x', expand=True, padx=(0, 10))
        bar_track = tk.Frame(bar_outer, bg="#e1e1e1", height=34)
        bar_track.pack(fill='both', expand=True)
        bar_track.pack_propagate(False)
        self.bar_fill = tk.Frame(bar_track, bg=C_ACCENT)
        self.bar_fill.place(relx=0, rely=0, relwidth=0, relheight=1)
        self.bar_pct_var = tk.StringVar(value="0%")
        tk.Label(bar_track, textvariable=self.bar_pct_var, bg="#e1e1e1", fg=C_TEXT,
                  font=FONT_BOLD).place(relx=0.5, rely=0.5, anchor='center')

        self.cancel_fmt_btn = ttk.Button(bar_row, text="🛑 Cancelar", command=self._cancel_format,
                                          style='Danger.TButton')
        self.cancel_fmt_btn.pack(side='left')

        self._format_done = False
        threading.Thread(target=self._run_format, daemon=True).start()

    def _cancel_format(self):
        self.cancel_event.set()
        self.cancel_fmt_btn.config(state='disabled')
        self.stat_speed.set("Cancelando... esperando que termine el bloque actual")

    def _run_format(self):
        def on_progress(p: FormatProgress):
            self.after(0, lambda: self._update_progress_ui(p))

        try:
            low_level_format(
                self.target_path, self.total_bytes,
                cluster_size=self.cluster_size, pattern=self.pattern,
                progress_callback=on_progress,
                cancel_check=self.cancel_event.is_set,
            )
            self._format_done = True
            self.after(0, self._on_format_success)
        except FormatCancelled:
            self.after(0, self._on_format_cancelled)
        except FormatError as e:
            self.after(0, lambda msg=str(e): self._on_format_error(msg))
        except Exception as e:
            self.after(0, lambda msg=str(e): self._on_format_error(msg))

    def _update_progress_ui(self, p: FormatProgress):
        pct = p.percent / 100.0

        # barra + porcentaje
        self.bar_fill.place(relx=0, rely=0, relwidth=pct, relheight=1)
        self.bar_pct_var.set(f"{p.percent:.0f}%")

        # mini stats
        self.stat_written.set(f"{p.bytes_written / (1024**3):.1f} GB escritos")
        self.stat_speed.set(format_speed_text(p.speed_bps))
        self.stat_eta.set(f"ETA {format_eta_text(p.eta_seconds)}")
        self.stat_cluster.set(
            f"Cluster {p.cluster_index:,} / {p.total_clusters:,}")

        # grilla estilo defrag
        done_count = round(pct * self.total_cells)
        active_end = min(done_count + self.GRID_ACTIVE_WINDOW, self.total_cells)
        for i, rect_id in enumerate(self._cell_ids):
            if i < done_count:
                self.grid_canvas.itemconfig(rect_id, fill=C_DONE)
            elif i < active_end:
                self.grid_canvas.itemconfig(rect_id, fill=C_ACCENT)
            else:
                self.grid_canvas.itemconfig(rect_id, fill='#ffffff')

    def _on_format_success(self):
        self.cancel_fmt_btn.config(state='disabled')
        messagebox.showinfo("Formateo completo", "El formateo de bajo nivel finalizó correctamente.", parent=self)
        self.destroy()

    def _on_format_cancelled(self):
        messagebox.showwarning(
            "Cancelado",
            "El formateo fue cancelado. El destino quedó PARCIALMENTE sobrescrito.",
            parent=self)
        self.destroy()

    def _on_format_error(self, msg: str):
        messagebox.showerror("Error de formateo", msg, parent=self)
        self.destroy()

    def _on_close(self):
        if hasattr(self, 'grid_canvas'):
            if not self._format_done:
                if not messagebox.askyesno(
                    "Formateo en curso",
                    "El formateo está en curso. ¿Cancelar y cerrar?", parent=self
                ):
                    return
                self.cancel_event.set()
        self.destroy()


# ==========================================================================
# Ventana flotante de progreso de descarga/extracción
# ==========================================================================
class DownloadOptionsDialog(tk.Toplevel):
    """Paso previo a la descarga: cuántos clips se van a bajar (de
    cuántos hay cargados), el rango de fechas que cubren, carpeta de
    destino, y dos opciones (crear una subcarpeta por fecha, crear
    hash automáticamente al terminar). 'Iniciar' llama a on_start(...)
    con lo elegido; el caller es quien arranca la descarga en sí."""

    def __init__(self, parent, total_to_download: int, total_loaded: int,
                 date_min: Optional[datetime], date_max: Optional[datetime],
                 initial_dir: str = "", on_start=None):
        super().__init__(parent)
        _ensure_styles()
        self.title("Descargar clips")
        self.configure(bg=C_PAGE)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.total_to_download = total_to_download
        self.total_loaded = total_loaded
        self.date_min = date_min
        self.date_max = date_max
        self.on_start = on_start

        self.dir_var = tk.StringVar(value=initial_dir or "")
        self.subfolder_var = tk.BooleanVar(value=True)
        self.hash_var = tk.BooleanVar(value=True)

        self.container = tk.Frame(self, bg=C_PAGE, padx=18, pady=14)
        self.container.pack(fill='both', expand=True)
        self._build()
        center_window(self, parent)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _build(self):
        frame = self.container
        tk.Label(frame, text="⬇ Descargar clips", bg=C_PAGE, fg=C_TEXT,
                 font=FONT_TITLE).pack(anchor='w', pady=(0, 10))

        outer, card = _card(frame)
        outer.pack(fill='x', pady=(0, 12))
        _kv_row(card, "Archivos a descargar", f"{self.total_to_download} de {self.total_loaded}")
        if self.date_min and self.date_max:
            if self.date_min.date() == self.date_max.date():
                fechas = self.date_min.strftime('%d/%m/%y')
            else:
                fechas = f"{self.date_min.strftime('%d/%m/%y')} - {self.date_max.strftime('%d/%m/%y')}"
            _kv_row(card, "Fechas", fechas)

        _section_title(frame, "Carpeta de descarga")
        dir_row = tk.Frame(frame, bg=C_PAGE)
        dir_row.pack(fill='x', pady=(0, 4))
        ttk.Entry(dir_row, textvariable=self.dir_var, width=42).pack(side='left', fill='x', expand=True)
        ttk.Button(dir_row, text="📁 Seleccionar", command=self._browse_dir).pack(side='left', padx=(6, 0))

        self.error_lbl = tk.Label(frame, bg=C_PAGE, fg=C_DANGER, font=FONT_SMALL,
                                   wraplength=420, justify='left', text="")
        self.error_lbl.pack(anchor='w', pady=(2, 0))

        _section_title(frame, "Opciones")
        tk.Checkbutton(
            frame, text="Crear carpeta por fecha (día)", variable=self.subfolder_var,
            bg=C_PAGE, fg=C_TEXT, font=FONT_BASE, activebackground=C_PAGE, selectcolor=C_PANEL,
        ).pack(anchor='w', pady=1)
        tk.Checkbutton(
            frame, text="Crear Hash al finalizar", variable=self.hash_var,
            bg=C_PAGE, fg=C_TEXT, font=FONT_BASE, activebackground=C_PAGE, selectcolor=C_PANEL,
        ).pack(anchor='w', pady=1)

        footer = tk.Frame(frame, bg=C_PAGE)
        footer.pack(fill='x', pady=(14, 0))
        ttk.Separator(footer).pack(fill='x', pady=(0, 10))
        btns = tk.Frame(footer, bg=C_PAGE)
        btns.pack(anchor='e')
        ttk.Button(btns, text="Cancelar", command=self.destroy).pack(side='left', padx=(0, 8))
        ttk.Button(btns, text="Iniciar", style='Accent.TButton', command=self._start).pack(side='left')

    def _browse_dir(self):
        d = filedialog.askdirectory(title="Carpeta de destino", initialdir=self.dir_var.get() or None, parent=self)
        if d:
            self.dir_var.set(d)
            self.error_lbl.config(text="")

    def _start(self):
        output_dir = self.dir_var.get().strip()
        if not output_dir:
            self.error_lbl.config(text="Elegí una carpeta de destino")
            return
        try:
            os.makedirs(output_dir, exist_ok=True)
        except Exception as e:
            self.error_lbl.config(text=f"No se pudo usar esa carpeta: {e}")
            return
        cb = self.on_start
        subfolder = self.subfolder_var.get()
        create_hash = self.hash_var.get()
        self.destroy()
        if cb:
            cb(output_dir, subfolder, create_hash)


class DownloadProgressDialog(tk.Toplevel):
    """Ventana centrada que se abre al empezar una descarga (uno o
    varios clips, con o sin audio) y se queda a la vista con el
    progreso hasta que termina. Mientras descarga sólo se puede
    cancelar; al terminar (OK, con fallidos, o cancelada) cambia a
    'Ver Carpeta' / 'Crear Hash' / 'Cerrar' para encadenar el paso
    siguiente sin tener que ir a buscar los botones de la ventana
    principal."""

    def __init__(self, parent, total: int, on_cancel=None,
                 on_view_folder=None, on_create_hash=None):
        super().__init__(parent)
        _ensure_styles()
        self.title("Descargando…")
        self.configure(bg=C_PAGE)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.total = max(total, 1)
        self.on_cancel = on_cancel
        self.on_view_folder = on_view_folder
        self.on_create_hash = on_create_hash

        self._start_t = time.time()
        self._current_index = 0
        self._done = False
        self._cancel_requested = False
        self._output_path = None

        self.container = tk.Frame(self, bg=C_PAGE, padx=18, pady=14)
        self.container.pack(fill='both', expand=True)
        self._build_progress_view()
        center_window(self, parent)
        self.protocol("WM_DELETE_WINDOW", self._on_close_attempt)
        self._tick()

    # -- construcción --------------------------------------------------
    def _build_progress_view(self):
        frame = self.container
        for w in frame.winfo_children():
            w.destroy()

        tk.Label(frame, text="⬇ Descargando…", bg=C_PAGE, fg=C_TEXT,
                 font=FONT_TITLE).pack(anchor='w', pady=(0, 10))

        self.status_var = tk.StringVar(
            value=f"Se están descargando 1 de {self.total}…" if self.total > 1 else "Descargando…")
        tk.Label(frame, textvariable=self.status_var, bg=C_PAGE, fg=C_MUTED,
                 font=FONT_BASE, wraplength=380, justify='left').pack(anchor='w', pady=(0, 8))

        bar_row = tk.Frame(frame, bg=C_PAGE)
        bar_row.pack(fill='x', pady=(0, 4))
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(
            bar_row, orient='horizontal', mode='determinate',
            variable=self.progress_var, maximum=100, length=340)
        self.progress_bar.pack(side='left', fill='x', expand=True)
        self.pct_var = tk.StringVar(value="0%")
        tk.Label(bar_row, textvariable=self.pct_var, bg=C_PAGE, fg=C_MUTED,
                 font=FONT_SMALL, width=5, anchor='e').pack(side='left', padx=(8, 0))

        times_row = tk.Frame(frame, bg=C_PAGE)
        times_row.pack(fill='x', pady=(6, 14))
        self.start_var = tk.StringVar(value=f"Inicio: {datetime.now().strftime('%H:%M:%S')}")
        self.eta_var = tk.StringVar(value="Estimado: calculando…")
        self.elapsed_var = tk.StringVar(value="Transcurrido: 00m 00s")
        for var in (self.start_var, self.eta_var, self.elapsed_var):
            tk.Label(times_row, textvariable=var, bg=C_PAGE, fg=C_MUTED,
                     font=FONT_SMALL).pack(anchor='w', pady=1)

        footer = tk.Frame(frame, bg=C_PAGE)
        footer.pack(fill='x')
        ttk.Separator(footer).pack(fill='x', pady=(0, 10))
        self.btns = tk.Frame(footer, bg=C_PAGE)
        self.btns.pack(fill='x')
        self.cancel_btn = ttk.Button(self.btns, text="Cancelar", command=self._request_cancel)
        self.cancel_btn.pack(side='left')

    # -- reloj de "tiempo transcurrido" / estimado -----------------------
    def _tick(self):
        if self._done:
            return
        elapsed = time.time() - self._start_t
        self.elapsed_var.set(f"Transcurrido: {self._fmt_hms(elapsed)}")
        if self._current_index > 0:
            avg = elapsed / self._current_index
            remaining = avg * (self.total - self._current_index)
            self.eta_var.set(f"Estimado: {self._fmt_hms(remaining)} más")
        self.after(500, self._tick)

    @staticmethod
    def _fmt_hms(seconds: float) -> str:
        seconds = max(0, int(seconds))
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h:d}h {m:02d}m {s:02d}s"
        return f"{m:02d}m {s:02d}s"

    # -- llamado desde el hilo de descarga (vía root.after(0, ...)) -----
    def update_item(self, index: int, total: int):
        """`index` es 0-based: cuántos clips ya se completaron / cuál
        se está descargando ahora."""
        self._current_index = index
        self.total = max(total, 1)
        shown = min(index + 1, self.total)
        if self.total > 1:
            self.status_var.set(f"Se están descargando {shown} de {self.total}…")
        else:
            self.status_var.set("Descargando…")
        pct = (index / self.total * 100) if self.total else 0
        self.progress_var.set(pct)
        self.pct_var.set(f"{pct:.0f}%")

    def mark_done(self, ok: int, failed: int, cancelled: bool, output_path: Optional[str]):
        self._done = True
        self._output_path = output_path
        self.progress_var.set(100)
        self.pct_var.set("100%")
        self.eta_var.set("Estimado: —")
        self.elapsed_var.set(f"Transcurrido: {self._fmt_hms(time.time() - self._start_t)}")

        if cancelled:
            self.status_var.set(f"Cancelado. {ok} descargado(s), {failed} fallido(s)")
        elif failed and not ok:
            self.status_var.set(f"❌ Error: no se pudo descargar ({failed} fallido(s))")
        elif failed:
            self.status_var.set(f"✅ Descarga completa: {ok} OK, {failed} fallido(s)")
        else:
            self.status_var.set(f"✅ Descarga completa: {ok} de {self.total}")

        for w in self.btns.winfo_children():
            w.destroy()
        if output_path and ok:
            ttk.Button(self.btns, text="📂 Ver Carpeta", command=self._view_folder).pack(side='left')
            ttk.Button(self.btns, text="🔑 Crear Hash", style='Accent.TButton',
                       command=self._create_hash).pack(side='left', padx=(8, 0))
        ttk.Button(self.btns, text="Cerrar", command=self.destroy).pack(side='right')

    # -- acciones ---------------------------------------------------------
    def _request_cancel(self):
        if self._cancel_requested:
            return
        self._cancel_requested = True
        self.cancel_btn.config(state='disabled', text="Cancelando…")
        self.status_var.set("Cancelando…")
        if self.on_cancel:
            self.on_cancel()

    def _view_folder(self):
        if self.on_view_folder:
            self.on_view_folder(self._output_path)

    def _create_hash(self):
        cb = self.on_create_hash
        self.destroy()
        if cb:
            cb(self._output_path)

    def _on_close_attempt(self):
        if self._done:
            self.destroy()
            return
        if messagebox.askyesno("Descarga en curso", "¿Cancelar la descarga en curso?", parent=self):
            self._request_cancel()


# ==========================================================================
# Crear hash de archivos extraídos
# ==========================================================================
class HashDialog(tk.Toplevel):
    """Paso 1: elegir carpeta/archivo. Paso 2: confirmación con resumen.
    Paso 3: opciones (algoritmos, alcance, formato de informe).
    Paso 4: progreso (barra general + listado clásico de archivos)."""

    def __init__(self, parent, initial_dir: str = ""):
        super().__init__(parent)
        _ensure_styles()
        self.title("Hashear — Verificación de integridad")
        self.configure(bg=C_PAGE)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.cancel_event = threading.Event()
        self.target_path = ""
        self._hash_done = False

        self.algo_vars = {a: tk.BooleanVar(value=(a == 'sha256')) for a in ALL_ALGORITHMS}
        self.recursive_var = tk.BooleanVar(value=True)
        self.exclude_temp_var = tk.BooleanVar(value=True)
        self.report_var = tk.StringVar(value='pdf')  # 'pdf' | 'txt' | 'both'

        self._build_step1(initial_dir)
        center_window(self, parent)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _fresh_container(self):
        for w in self.winfo_children():
            w.destroy()
        container = tk.Frame(self, bg=C_PAGE, padx=18, pady=14)
        container.pack(fill='both', expand=True)
        return container

    def _info_banner(self, parent, icon, text):
        outer = tk.Frame(parent, bg=C_ACCENT, padx=1, pady=1)
        outer.pack(fill='x', pady=(0, 14))
        banner = tk.Frame(outer, bg="#e5f1fb", padx=10, pady=8)
        banner.pack(fill='x')
        tk.Label(banner, text=icon, bg="#e5f1fb", font=('Segoe UI', 13)).pack(side='left', padx=(0, 8))
        tk.Label(banner, bg="#e5f1fb", fg=C_TEXT, justify='left', font=FONT_SMALL,
                  wraplength=430, text=text).pack(side='left', fill='x')

    # -- Paso 1: elegir carpeta/archivo --------------------------------------
    def _build_step1(self, initial_dir: str = ""):
        frame = self._fresh_container()

        self._info_banner(
            frame, "ℹ️",
            "Elegí la carpeta (o el archivo) que querés hashear. Si es una "
            "carpeta, por defecto se procesan también las subcarpetas.")

        tk.Label(frame, text="Carpeta o archivo", bg=C_PAGE, fg=C_TEXT, font=FONT_BASE).pack(anchor='w')
        row = tk.Frame(frame, bg=C_PAGE)
        row.pack(fill='x', pady=(4, 4))
        self.target_var = tk.StringVar(value=initial_dir or "")
        ttk.Entry(row, textvariable=self.target_var).pack(side='left', fill='x', expand=True)
        ttk.Button(row, text="Carpeta...", command=self._browse_folder).pack(side='left', padx=(8, 0))
        ttk.Button(row, text="Archivo...", command=self._browse_file).pack(side='left', padx=(4, 0))

        self.step1_error = tk.Label(frame, bg=C_PAGE, fg=C_DANGER, justify='left',
                                      font=FONT_SMALL, wraplength=470, text="")
        self.step1_error.pack(anchor='w', pady=(4, 0))

        footer = tk.Frame(frame, bg=C_PAGE)
        footer.pack(fill='x', pady=(20, 0))
        ttk.Separator(footer).pack(fill='x', pady=(0, 10))
        btns = tk.Frame(footer, bg=C_PAGE)
        btns.pack(anchor='e')
        ttk.Button(btns, text="Cancelar", command=self.destroy).pack(side='left', padx=(0, 8))
        ttk.Button(btns, text="Seleccionar ›", style='Accent.TButton',
                   command=self._go_step2).pack(side='left')

    def _browse_folder(self):
        folder = filedialog.askdirectory(parent=self, initialdir=self.target_var.get() or None)
        if folder:
            self.target_var.set(folder)

    def _browse_file(self):
        path = filedialog.askopenfilename(parent=self, initialdir=self.target_var.get() or None)
        if path:
            self.target_var.set(path)

    def _go_step2(self):
        target = self.target_var.get().strip()
        if not target or not os.path.exists(target):
            self.step1_error.config(text="⚠ Elegí una carpeta o archivo que exista.")
            return
        self.target_path = target
        self._build_step2()

    # -- Paso 2: confirmación con resumen ------------------------------------
    def _build_step2(self):
        frame = self._fresh_container()
        is_file = os.path.isfile(self.target_path)

        self._info_banner(
            frame, "📁",
            "Confirmado. Revisá el resumen y pasá a elegir los algoritmos de hash.")

        files = collect_files(self.target_path)
        total_size = sum(os.path.getsize(p) for p in files if os.path.exists(p))
        subfolders = 0 if is_file else count_subfolders(self.target_path)

        outer, card = _card(frame)
        outer.pack(fill='x', pady=(0, 4))
        _kv_row(card, "Carpeta" if not is_file else "Archivo", self.target_path)
        _kv_row(card, "Archivos", str(len(files)))
        if not is_file:
            _kv_row(card, "Subcarpetas", str(subfolders))
        _kv_row(card, "Tamaño total", f"{total_size / (1024*1024):.1f} MB")

        self._step2_files_count = len(files)

        footer = tk.Frame(frame, bg=C_PAGE)
        footer.pack(fill='x', pady=(20, 0))
        ttk.Separator(footer).pack(fill='x', pady=(0, 10))
        btns = tk.Frame(footer, bg=C_PAGE)
        btns.pack(anchor='e')
        ttk.Button(btns, text="‹ Cambiar carpeta", command=lambda: self._build_step1(self.target_path)
                   ).pack(side='left', padx=(0, 8))
        ttk.Button(btns, text="Configurar hash ›", style='Accent.TButton',
                   command=self._build_step3).pack(side='left')

    # -- Paso 3: opciones (algoritmos, alcance, formato de informe) ----------
    def _build_step3(self):
        frame = self._fresh_container()
        is_file = os.path.isfile(self.target_path)

        outer, card = _card(frame)
        outer.pack(fill='x', pady=(0, 4))
        _kv_row(card, "Carpeta" if not is_file else "Archivo", self.target_path)

        _section_title(frame, "Algoritmos de hash")
        algo_grid = tk.Frame(frame, bg=C_PAGE)
        algo_grid.pack(fill='x', pady=(0, 4))
        algo_labels = {
            'sha256': "SHA-256  (recomendado)", 'sha1': "SHA-1", 'md5': "MD5",
            'sha512': "SHA-512", 'crc32': "CRC32",
        }
        for i, algo in enumerate(ALL_ALGORITHMS):
            r, c = divmod(i, 2)
            tk.Checkbutton(
                algo_grid, text=algo_labels[algo], variable=self.algo_vars[algo],
                bg=C_PAGE, fg=C_TEXT, font=FONT_BASE, activebackground=C_PAGE,
                selectcolor=C_PANEL,
            ).grid(row=r, column=c, sticky='w', padx=(0, 16), pady=2)

        _section_title(frame, "Alcance")
        if not is_file:
            tk.Checkbutton(
                frame, text="Incluir subcarpetas", variable=self.recursive_var,
                bg=C_PAGE, fg=C_TEXT, font=FONT_BASE, activebackground=C_PAGE, selectcolor=C_PANEL,
            ).pack(anchor='w', pady=1)
        tk.Checkbutton(
            frame, text=f"Excluir temporales ({', '.join(DEFAULT_EXCLUDE_SUFFIXES)})",
            variable=self.exclude_temp_var,
            bg=C_PAGE, fg=C_TEXT, font=FONT_BASE, activebackground=C_PAGE, selectcolor=C_PANEL,
        ).pack(anchor='w', pady=1)

        _section_title(frame, "Formato del informe")
        for value, text in (
            ('pdf', "PDF (con cadena de custodia)"),
            ('txt', "TXT (ruta + hash)"),
            ('both', "Ambos"),
        ):
            tk.Radiobutton(
                frame, text=text, value=value, variable=self.report_var,
                bg=C_PAGE, fg=C_TEXT, font=FONT_BASE, activebackground=C_PAGE, selectcolor=C_PANEL,
            ).pack(anchor='w', pady=1)

        self.step3_error = tk.Label(frame, bg=C_PAGE, fg=C_DANGER, justify='left',
                                      font=FONT_SMALL, wraplength=470, text="")
        self.step3_error.pack(anchor='w', pady=(6, 0))

        footer = tk.Frame(frame, bg=C_PAGE)
        footer.pack(fill='x', pady=(14, 0))
        ttk.Separator(footer).pack(fill='x', pady=(0, 10))
        btns = tk.Frame(footer, bg=C_PAGE)
        btns.pack(anchor='e')
        ttk.Button(btns, text="‹ Atrás", command=self._build_step2).pack(side='left', padx=(0, 8))
        ttk.Button(btns, text="Iniciar hasheo ›", style='Accent.TButton',
                   command=self._go_step4).pack(side='left')

    def _selected_algorithms(self):
        return [a for a in ALL_ALGORITHMS if self.algo_vars[a].get()]

    def _go_step4(self):
        if not self._selected_algorithms():
            self.step3_error.config(text="⚠ Elegí al menos un algoritmo de hash.")
            return
        self._build_step4()

    # -- Paso 4: progreso (barra general + listado clásico de archivos) -----
    def _build_step4(self):
        frame = self._fresh_container()
        self.title("Hasheando… — no cierres esta ventana")

        self.title_var = tk.StringVar(value="Hasheando — preparando…")
        tk.Label(frame, textvariable=self.title_var, bg=C_PAGE, fg=C_TEXT,
                  font=FONT_BOLD, anchor='w').pack(anchor='w', pady=(0, 10))

        # -- listado de archivos, con scroll --
        list_outer = tk.Frame(frame, bg=C_BORDER_STRONG, padx=1, pady=1)
        list_outer.pack(fill='x', pady=(0, 12))
        list_canvas = tk.Canvas(list_outer, width=520, height=190, bg=C_PANEL,
                                  highlightthickness=0)
        vbar = ttk.Scrollbar(list_outer, orient='vertical', command=list_canvas.yview)
        list_canvas.configure(yscrollcommand=vbar.set)
        list_canvas.pack(side='left', fill='both', expand=True)
        vbar.pack(side='right', fill='y')

        self._rows_frame = tk.Frame(list_canvas, bg=C_PANEL)
        self._rows_window = list_canvas.create_window((0, 0), window=self._rows_frame, anchor='nw')

        def _on_rows_configure(event):
            list_canvas.configure(scrollregion=list_canvas.bbox('all'))
        self._rows_frame.bind('<Configure>', _on_rows_configure)

        def _on_canvas_configure(event):
            list_canvas.itemconfig(self._rows_window, width=event.width)
        list_canvas.bind('<Configure>', _on_canvas_configure)

        # -- mini stats --
        stats = tk.Frame(frame, bg=C_PAGE)
        stats.pack(fill='x', pady=(0, 4))
        self.stat_written = tk.StringVar(value="0.0 MB hasheados")
        self.stat_speed = tk.StringVar(value="0.0 MB/s")
        self.stat_eta = tk.StringVar(value="ETA —")
        self.stat_file = tk.StringVar(value="Archivo 0 / 0")
        for var in (self.stat_written, self.stat_speed, self.stat_eta, self.stat_file):
            tk.Label(stats, textvariable=var, bg=C_PAGE, fg=C_MUTED, font=FONT_SMALL
                      ).pack(side='left', padx=(0, 18))

        # -- footer: barra general + cancelar, misma altura --
        footer = tk.Frame(frame, bg=C_PAGE)
        footer.pack(fill='x', pady=(10, 0))
        ttk.Separator(footer).pack(fill='x', pady=(0, 10))

        bar_row = tk.Frame(footer, bg=C_PAGE)
        bar_row.pack(fill='x')
        bar_outer = tk.Frame(bar_row, bg=C_BORDER_STRONG, padx=1, pady=1)
        bar_outer.pack(side='left', fill='x', expand=True, padx=(0, 10))
        bar_track = tk.Frame(bar_outer, bg="#e1e1e1", height=34)
        bar_track.pack(fill='both', expand=True)
        bar_track.pack_propagate(False)
        self.bar_fill = tk.Frame(bar_track, bg=C_ACCENT)
        self.bar_fill.place(relx=0, rely=0, relwidth=0, relheight=1)
        self.bar_pct_var = tk.StringVar(value="0%")
        tk.Label(bar_track, textvariable=self.bar_pct_var, bg="#e1e1e1", fg=C_TEXT,
                  font=FONT_BOLD).place(relx=0.5, rely=0.5, anchor='center')

        self.cancel_hash_btn = ttk.Button(bar_row, text="🛑 Cancelar", command=self._cancel_hash,
                                            style='Danger.TButton')
        self.cancel_hash_btn.pack(side='left')

        self._file_rows = []  # [{frame, fname_lbl, state_lbl, mini_fill, mini_pct}]
        self._run_hash_worker()

    def _add_file_row(self, path):
        row = tk.Frame(self._rows_frame, bg=C_PANEL)
        row.pack(fill='x')
        top = tk.Frame(row, bg=C_PANEL)
        top.pack(fill='x', padx=8, pady=3)
        tk.Label(top, text="📄", bg=C_PANEL, font=FONT_SMALL).pack(side='left', padx=(0, 6))
        fname_lbl = tk.Label(top, text=os.path.basename(path), bg=C_PANEL, fg=C_TEXT,
                               font=FONT_SMALL, anchor='w')
        fname_lbl.pack(side='left', fill='x', expand=True)
        state_lbl = tk.Label(top, text="Pendiente", bg=C_PANEL, fg=C_MUTED,
                               font=FONT_SMALL)
        state_lbl.pack(side='right')

        mini_outer = tk.Frame(row, bg=C_PANEL, padx=8)
        mini_track = tk.Frame(mini_outer, bg="#e1e1e1", height=4)
        mini_fill = tk.Frame(mini_track, bg=C_ACCENT)
        mini_fill.place(relx=0, rely=0, relwidth=0, relheight=1)
        # el mini-track sólo se muestra para la fila "actual" (ver _set_row_state)

        sep = ttk.Separator(row)
        sep.pack(fill='x')

        entry = dict(row=row, fname_lbl=fname_lbl, state_lbl=state_lbl,
                     mini_outer=mini_outer, mini_track=mini_track, mini_fill=mini_fill)
        self._file_rows.append(entry)
        return entry

    def _set_row_pending(self, entry):
        entry['state_lbl'].config(text="Pendiente", fg=C_MUTED)
        entry['row'].config(bg=C_PANEL)
        entry['fname_lbl'].config(bg=C_PANEL)
        entry['mini_outer'].pack_forget()

    def _set_row_current(self, entry, pct):
        entry['row'].config(bg="#e5f1fb")
        entry['fname_lbl'].config(bg="#e5f1fb")
        entry['state_lbl'].config(text=f"{pct:.0f}%", fg=C_ACCENT_DARK, bg="#e5f1fb")
        entry['mini_outer'].config(bg="#e5f1fb")
        entry['mini_outer'].pack(fill='x', pady=(0, 4))
        entry['mini_fill'].place(relx=0, rely=0, relwidth=max(pct, 0) / 100, relheight=1)
        entry['mini_track'].pack(fill='x')

    def _set_row_done(self, entry, ok=True):
        entry['row'].config(bg=C_PANEL)
        entry['fname_lbl'].config(bg=C_PANEL)
        entry['mini_outer'].pack_forget()
        if ok:
            entry['state_lbl'].config(text="✓ OK", fg=C_OK, bg=C_PANEL)
        else:
            entry['state_lbl'].config(text="✗ Error", fg=C_DANGER, bg=C_PANEL)

    def _cancel_hash(self):
        self.cancel_event.set()
        self.cancel_hash_btn.config(state='disabled')
        self.stat_speed.set("Cancelando… esperando el archivo actual")

    def _run_hash_worker(self):
        algorithms = self._selected_algorithms()
        exclude = DEFAULT_EXCLUDE_SUFFIXES if self.exclude_temp_var.get() else ()
        recursive = self.recursive_var.get() if not os.path.isfile(self.target_path) else True

        def worker():
            files = collect_files(self.target_path, recursive=recursive, exclude_suffixes=exclude)
            if not files:
                self.after(0, self._finish_empty)
                return

            self.after(0, lambda: self._init_rows(files))

            last_index = {'i': 0}

            def progress_cb(p: HashProgress):
                self.after(0, lambda: self._update_progress_ui(p, last_index))

            try:
                entries = build_hash_entries(
                    files, algorithms=algorithms,
                    progress_callback=progress_cb,
                    cancel_check=self.cancel_event.is_set,
                )
                self._hash_done = True
                self.after(0, lambda: self._on_hash_success(entries, algorithms))
            except HashCancelled:
                self.after(0, self._on_hash_cancelled)
            except Exception as e:
                self.after(0, lambda msg=str(e): self._on_hash_error(msg))

        threading.Thread(target=worker, daemon=True).start()

    def _init_rows(self, files):
        self.stat_file.set(f"Archivo 0 / {len(files)}")
        for path in files:
            self._add_file_row(path)

    def _update_progress_ui(self, p: HashProgress, last_index):
        # marcar como "done" todas las filas de archivos ya completados
        while last_index['i'] < p.file_index - 1:
            self._set_row_done(self._file_rows[last_index['i']])
            last_index['i'] += 1

        current = self._file_rows[p.file_index - 1]
        if p.file_bytes_done >= p.file_bytes_total:
            self._set_row_done(current)
            last_index['i'] = p.file_index
        else:
            self._set_row_current(current, p.file_percent)

        self.title_var.set(f"Hasheando  —  {os.path.basename(p.current_path)}")
        self.bar_fill.place(relx=0, rely=0, relwidth=p.percent / 100, relheight=1)
        self.bar_pct_var.set(f"{p.percent:.0f}%")
        self.stat_written.set(f"{p.total_bytes_done / (1024*1024):.1f} MB hasheados")
        self.stat_speed.set(format_speed_text(p.speed_bps))
        self.stat_eta.set(f"ETA {format_eta_text(p.eta_seconds)}")
        self.stat_file.set(f"Archivo {p.file_index} / {p.total_files}")

    def _finish_empty(self):
        messagebox.showinfo("Sin archivos", "No se encontraron archivos en el destino elegido.", parent=self)
        self.destroy()

    def _on_hash_success(self, entries, algorithms):
        for entry in self._file_rows:
            if entry['state_lbl'].cget('text') not in ("✓ OK", "✗ Error"):
                self._set_row_done(entry)
        self.cancel_hash_btn.config(state='disabled')
        self._ask_save_report(entries, algorithms)

    def _ask_save_report(self, entries, algorithms):
        fmt = self.report_var.get()
        default_ext = '.pdf' if fmt in ('pdf', 'both') else '.txt'
        output = filedialog.asksaveasfilename(
            title="Guardar informe de hash",
            defaultextension=default_ext,
            filetypes=[("Documento PDF", "*.pdf"), ("Archivo de texto", "*.txt"),
                       ("Todos los archivos", "*.*")],
            initialdir=config.get_last_dir('last_report_dir') or None,
            initialfile="informe_hash",
            parent=self,
        )
        if not output:
            self.destroy()
            return
        config.set_last_dir('last_report_dir', os.path.dirname(output))
        self._write_reports(entries, algorithms, output, fmt)

    def _write_reports(self, entries, algorithms, output, fmt):
        base, ext = os.path.splitext(output)
        written = []
        try:
            if fmt in ('pdf', 'both'):
                pdf_path = output if ext.lower() == '.pdf' else base + '.pdf'
                write_hash_report_pdf(entries, pdf_path, algorithms=algorithms,
                                       source_description=self.target_path)
                written.append(pdf_path)
            if fmt in ('txt', 'both'):
                txt_path = output if ext.lower() == '.txt' else base + '.txt'
                write_hash_report(entries, txt_path, algorithms=algorithms,
                                   source_description=self.target_path)
                written.append(txt_path)
        except ImportError:
            # Falta reportlab: ofrecemos instalarlo ahora mismo (pip,
            # con el mismo intérprete que corre la app) en vez de sólo
            # avisar y dejar al usuario ir a la consola.
            if messagebox.askyesno(
                "Falta una dependencia",
                "Para generar el PDF hace falta el paquete 'reportlab' y no está "
                "instalado.\n\n¿Instalarlo ahora automáticamente? "
                "(requiere conexión a internet)",
                parent=self,
            ):
                ReportlabInstallDialog(
                    self, on_done=lambda: self._write_reports(entries, algorithms, output, fmt))
            return
        except OSError as e:
            messagebox.showerror("Error", f"No se pudo guardar el informe:\n{e}", parent=self)
            return

        messagebox.showinfo(
            "Informe generado",
            f"Se generó con {len(entries)} archivo(s):\n" + "\n".join(written),
            parent=self)
        self.destroy()

    def _on_hash_cancelled(self):
        messagebox.showwarning("Cancelado", "El hasheo fue cancelado por el usuario.", parent=self)
        self.destroy()

    def _on_hash_error(self, msg: str):
        messagebox.showerror("Error al hashear", msg, parent=self)
        self.destroy()

    def _on_close(self):
        if hasattr(self, 'cancel_hash_btn') and not self._hash_done:
            if not messagebox.askyesno(
                "Hasheo en curso", "El hasheo está en curso. ¿Cancelar y cerrar?", parent=self
            ):
                return
            self.cancel_event.set()
        self.destroy()


# ==========================================================================
# Acerca de
# ==========================================================================
def _load_about_icon(size=72):
    """Carga sd-card.ico agrandado para el panel izquierdo de 'Acerca
    de'. Devuelve un ImageTk.PhotoImage, o None si no está Pillow o
    no se encuentra el archivo (la ventana igual se arma sin ícono)."""
    try:
        from PIL import Image, ImageTk
        from core.config import APP_ICON_PATH
        if not os.path.isfile(APP_ICON_PATH):
            return None
        # Image.open ya selecciona automáticamente la resolución más
        # grande disponible dentro del .ico (el archivo trae varias,
        # de 16x16 a 256x256).
        img = Image.open(APP_ICON_PATH).convert('RGBA')
        if img.size != (size, size):
            img = img.resize((size, size), Image.LANCZOS)
        return ImageTk.PhotoImage(img)
    except Exception:
        log.warning("No se pudo cargar el ícono para 'Acerca de'", exc_info=True)
        return None


def show_about(parent):
    _ensure_styles()
    win = tk.Toplevel(parent)
    win.title("Acerca de")
    win.configure(bg=C_PAGE)
    win.resizable(False, False)
    win.transient(parent)
    win.grab_set()

    outer = tk.Frame(win, bg=C_PAGE, padx=20, pady=18)
    outer.pack()

    # -- fila superior: dos columnas, ícono grande a la izquierda,
    # texto a la derecha --
    top = tk.Frame(outer, bg=C_PAGE)
    top.pack(fill='x')

    icon_img = _load_about_icon(72)
    icon_col = tk.Frame(top, bg=C_PAGE, width=96, height=96)
    icon_col.pack(side='left', anchor='n', padx=(0, 16))
    icon_col.pack_propagate(False)
    if icon_img is not None:
        icon_lbl = tk.Label(icon_col, image=icon_img, bg=C_PAGE)
        icon_lbl.image = icon_img  # referencia viva: evita garbage collection
        icon_lbl.place(relx=0.5, rely=0.5, anchor='center')
    else:
        # Sin Pillow o sin archivo: no rompemos el layout, dejamos la
        # columna vacía en vez de forzar un ícono roto.
        pass

    text_col = tk.Frame(top, bg=C_PAGE)
    text_col.pack(side='left', fill='both', expand=True)

    tk.Label(text_col, text=APP_NAME, bg=C_PAGE, fg=C_TEXT,
              font=('Segoe UI', 12, 'bold'), justify='left', wraplength=340
              ).pack(anchor='w')
    tk.Label(text_col, text=f"Versión {APP_VERSION}", bg=C_PAGE, fg=C_MUTED,
              font=FONT_BASE).pack(anchor='w', pady=(0, 10))
    tk.Label(
        text_col, bg=C_PAGE, fg=C_TEXT, font=FONT_BASE, justify='left', wraplength=340,
        text=(
            "Herramienta de lectura, previsualización y extracción forense\n"
            "de grabaciones almacenadas en tarjetas SD con formato de\n"
            "índice EZVIZ / Hikvision (index00.bin).\n\n"
            "Funciones:\n"
            "  • Lectura de índices y listado de clips por fecha\n"
            "  • Vista previa embebida y línea de tiempo (horizontal/vertical)\n"
            "  • Extracción a MP4 (con o sin audio) y miniaturas JPG\n"
            "  • Cálculo de hash (MD5/SHA-256) de archivos extraídos\n"
            "  • Formateo de bajo nivel de la tarjeta de origen\n\n"
            "Requiere ffmpeg instalado y disponible en el PATH.\n"
            "La vista previa embebida requiere opencv-python-headless y pillow."
        ),
    ).pack(anchor='w')

    # -- separador --
    ttk.Separator(outer, orient='horizontal').pack(fill='x', pady=(16, 12))

    # -- pie de copyright --
    tk.Label(
        outer, bg=C_PAGE, fg=C_TEXT,
        text=f"Eddie Soft © 2026 · Ver. {APP_VERSION}",
        font=('Segoe UI', 13, 'bold'),
    ).pack()

    ttk.Button(outer, text="Cerrar", command=win.destroy).pack(pady=(15, 0))

    center_window(win, parent)


# ==========================================================================
# Instalación automática de ffmpeg
# ==========================================================================
class FFmpegInstallDialog(tk.Toplevel):
    """Descarga e instala ffmpeg (Windows) con barra de progreso.
    Se abre desde el aviso de 'ffmpeg no encontrado' al iniciar, o
    desde el menú Ayuda en cualquier momento."""

    def __init__(self, parent, on_done=None):
        super().__init__(parent)
        _ensure_styles()
        self.title("Instalar ffmpeg")
        self.configure(bg=C_PAGE)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.on_done = on_done
        self.cancel_event = threading.Event()
        self._done = False

        self.container = tk.Frame(self, bg=C_PAGE, padx=18, pady=14)
        self.container.pack(fill='both', expand=True)
        self._build_confirm()
        center_window(self, parent)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _fresh_container(self):
        for w in self.winfo_children():
            w.destroy()
        container = tk.Frame(self, bg=C_PAGE, padx=18, pady=14)
        container.pack(fill='both', expand=True)
        return container

    def _build_confirm(self):
        frame = self.container
        tk.Label(frame, text="ffmpeg no está instalado", bg=C_PAGE, fg=C_TEXT,
                  font=('Segoe UI', 11, 'bold')).pack(anchor='w', pady=(0, 8))
        tk.Label(
            frame, bg=C_PAGE, fg=C_MUTED, font=FONT_BASE, justify='left', wraplength=380,
            text=("La app necesita ffmpeg para extraer clips, miniaturas y vista previa.\n\n"
                  "Se puede descargar el build oficial para Windows (gyan.dev, "
                  "~130 MB) y agregarlo al PATH de tu usuario automáticamente, "
                  "sin permisos de administrador."),
        ).pack(anchor='w', pady=(0, 16))

        btns = tk.Frame(frame, bg=C_PAGE)
        btns.pack(fill='x')
        ttk.Button(btns, text="Ahora no", command=self.destroy).pack(side='left')
        ttk.Button(btns, text="Descargar e instalar ›", style='Accent.TButton',
                   command=self._start_install).pack(side='right')

    def _start_install(self):
        frame = self._fresh_container()
        tk.Label(frame, text="Instalando ffmpeg…", bg=C_PAGE, fg=C_TEXT,
                  font=('Segoe UI', 11, 'bold')).pack(anchor='w', pady=(0, 10))

        self.stage_var = tk.StringVar(value="Conectando…")
        tk.Label(frame, textvariable=self.stage_var, bg=C_PAGE, fg=C_MUTED,
                  font=FONT_BASE).pack(anchor='w')

        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(
            frame, orient='horizontal', mode='determinate',
            variable=self.progress_var, maximum=100, length=380)
        self.progress_bar.pack(fill='x', pady=(8, 14))

        self.cancel_install_btn = ttk.Button(frame, text="Cancelar", command=self._on_close)
        self.cancel_install_btn.pack(anchor='e')

        threading.Thread(target=self._run_install, daemon=True).start()

    def _run_install(self):
        def progress(p: ffmpeg_setup.DownloadProgress):
            self.after(0, lambda: self._update_progress_ui(p))

        try:
            bin_dir = ffmpeg_setup.download_and_install(
                progress_callback=progress, cancel_check=self.cancel_event.is_set)
            self.after(0, lambda: self._on_success(bin_dir))
        except ffmpeg_setup.FFmpegInstallCancelled:
            self.after(0, self._on_cancelled)
        except ffmpeg_setup.FFmpegInstallError as e:
            # El detalle completo (traceback) ya quedó en el log desde
            # ffmpeg_setup.download_and_install; acá solo mostramos el
            # mensaje corto al usuario.
            self.after(0, lambda msg=str(e): self._on_error(msg))
        except Exception as e:
            log.exception("Error inesperado instalando ffmpeg")
            self.after(0, lambda msg=str(e): self._on_error(msg))

    def _update_progress_ui(self, p: 'ffmpeg_setup.DownloadProgress'):
        labels = {'download': "Descargando", 'extract': "Extrayendo", 'path': "Configurando PATH"}
        if p.stage == 'download' and p.bytes_total:
            mb_done = p.bytes_done / (1024 * 1024)
            mb_total = p.bytes_total / (1024 * 1024)
            self.stage_var.set(f"{labels[p.stage]}… {mb_done:.1f} / {mb_total:.1f} MB")
        else:
            self.stage_var.set(f"{labels.get(p.stage, p.stage)}…")
        self.progress_var.set(p.percent)

    def _on_success(self, bin_dir: str):
        self._done = True
        frame = self._fresh_container()
        tk.Label(frame, text="✅ ffmpeg instalado correctamente", bg=C_PAGE, fg=C_TEXT,
                  font=('Segoe UI', 11, 'bold')).pack(anchor='w', pady=(0, 8))
        tk.Label(frame, text=bin_dir, bg=C_PAGE, fg=C_MUTED, font=FONT_BASE,
                  wraplength=380, justify='left').pack(anchor='w', pady=(0, 16))
        ttk.Button(frame, text="Listo", style='Accent.TButton',
                   command=self.destroy).pack(anchor='e')
        if self.on_done:
            self.on_done()

    def _on_cancelled(self):
        self._done = True
        self.destroy()

    def _on_error(self, msg: str):
        self._done = True
        messagebox.showerror(
            "Error instalando ffmpeg",
            f"{msg}\n\nEl detalle completo quedó guardado en el log de la app "
            f"(menú Ayuda o carpeta %LOCALAPPDATA%\\sd_hik_reader\\logs\\app.log).",
            parent=self)
        self.destroy()

    def _on_close(self):
        if hasattr(self, 'cancel_install_btn') and not self._done:
            if not messagebox.askyesno(
                "Instalación en curso", "¿Cancelar la instalación de ffmpeg?", parent=self
            ):
                return
            self.cancel_event.set()
            return
        self.destroy()


# ==========================================================================
# Chequeo de dependencias Python opcionales al iniciar la app (Pillow,
# OpenCV, reportlab). ffmpeg tiene su propio chequeo/instalador aparte
# (ver _check_ffmpeg en gui/app.py y FFmpegInstallDialog más arriba).
# ==========================================================================
class DependencyCheckDialog(tk.Toplevel):
    """Se muestra al iniciar si falta algún módulo opcional. Lista qué
    falta y para qué se usa, con un check por módulo (todos tildados
    por default), y permite instalarlos ahora con pip (mismo intérprete
    que corre la app) antes de seguir usando la app."""

    def __init__(self, parent, missing: List['deps_check.OptionalDep']):
        super().__init__(parent)
        _ensure_styles()
        self.title("Módulos de la app")
        self.configure(bg=C_PAGE)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.missing = missing
        self._done = False
        self.dep_vars = {d.pip_name: tk.BooleanVar(value=True) for d in missing}

        self.container = tk.Frame(self, bg=C_PAGE, padx=18, pady=14)
        self.container.pack(fill='both', expand=True)
        self._build_list_view()
        center_window(self, parent)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _fresh_container(self):
        for w in self.winfo_children():
            w.destroy()
        container = tk.Frame(self, bg=C_PAGE, padx=18, pady=14)
        container.pack(fill='both', expand=True)
        return container

    def _build_list_view(self):
        frame = self.container
        tk.Label(frame, text="Faltan módulos opcionales", bg=C_PAGE, fg=C_TEXT,
                 font=FONT_TITLE).pack(anchor='w', pady=(0, 6))
        tk.Label(
            frame, bg=C_PAGE, fg=C_MUTED, font=FONT_BASE, wraplength=420, justify='left',
            text="La app funciona igual sin ellos, pero algunas funciones no van a estar "
                 "disponibles. Se pueden instalar ahora (pip) o más tarde a mano.",
        ).pack(anchor='w', pady=(0, 12))

        for dep in self.missing:
            row = tk.Frame(frame, bg=C_PAGE)
            row.pack(fill='x', pady=3, anchor='w')
            tk.Checkbutton(
                row, text=f"{dep.pip_name}", variable=self.dep_vars[dep.pip_name],
                bg=C_PAGE, fg=C_TEXT, font=FONT_BOLD, activebackground=C_PAGE, selectcolor=C_PANEL,
            ).pack(anchor='w')
            tk.Label(
                row, text=f"      → {dep.feature}", bg=C_PAGE, fg=C_MUTED,
                font=FONT_SMALL, wraplength=400, justify='left',
            ).pack(anchor='w')

        if deps_check.is_frozen():
            tk.Label(
                frame, bg=C_PAGE, fg=C_DANGER, font=FONT_SMALL, wraplength=420, justify='left',
                text="Esta es la versión empaquetada (.exe): no tiene pip para instalar "
                     "en caliente. Hay que reconstruir el .exe con estos paquetes ya "
                     "instalados en el entorno de build.",
            ).pack(anchor='w', pady=(10, 0))

        footer = tk.Frame(frame, bg=C_PAGE)
        footer.pack(fill='x', pady=(14, 0))
        ttk.Separator(footer).pack(fill='x', pady=(0, 10))
        btns = tk.Frame(footer, bg=C_PAGE)
        btns.pack(fill='x')
        ttk.Button(btns, text="Omitir", command=self.destroy).pack(side='right')
        self.install_btn = ttk.Button(
            btns, text="Instalar seleccionados", style='Accent.TButton', command=self._start_install)
        self.install_btn.pack(side='right', padx=(0, 8))
        if deps_check.is_frozen():
            self.install_btn.config(state='disabled')

    def _start_install(self):
        selected = [dep for dep in self.missing if self.dep_vars[dep.pip_name].get()]
        if not selected:
            self.destroy()
            return
        self._build_progress_view(selected)
        threading.Thread(target=self._run_install, args=(selected,), daemon=True).start()

    def _build_progress_view(self, selected):
        frame = self._fresh_container()
        self.container = frame
        tk.Label(frame, text="Instalando módulos…", bg=C_PAGE, fg=C_TEXT,
                 font=FONT_TITLE).pack(anchor='w', pady=(0, 10))
        self.stage_var = tk.StringVar(value="Preparando...")
        tk.Label(frame, textvariable=self.stage_var, bg=C_PAGE, fg=C_MUTED,
                 font=FONT_BASE, wraplength=420, justify='left').pack(anchor='w', pady=(0, 10))
        self.pb = ttk.Progressbar(frame, orient='horizontal', mode='indeterminate', length=420)
        self.pb.pack(fill='x', pady=(0, 14))
        self.pb.start(12)

    def _run_install(self, selected):
        pip_names = [d.pip_name for d in selected]
        ok, failed = deps_check.install_packages(
            pip_names, log_callback=lambda msg: self.after(0, lambda m=msg: self.stage_var.set(m)))
        self.after(0, lambda: self._on_install_done(ok, failed))

    def _on_install_done(self, ok, failed):
        self._done = True
        self.pb.stop()
        frame = self._fresh_container()
        if ok:
            tk.Label(frame, text=f"✅ Instalado: {', '.join(ok)}", bg=C_PAGE, fg=C_TEXT,
                     font=FONT_BOLD, wraplength=420, justify='left').pack(anchor='w', pady=(0, 6))
        if failed:
            tk.Label(frame, text=f"❌ No se pudo instalar: {', '.join(failed)}", bg=C_PAGE, fg=C_DANGER,
                     font=FONT_BOLD, wraplength=420, justify='left').pack(anchor='w', pady=(0, 6))
        if ok:
            tk.Label(
                frame, bg=C_PAGE, fg=C_MUTED, font=FONT_SMALL, wraplength=420, justify='left',
                text="Reiniciá la app para que los módulos recién instalados queden disponibles.",
            ).pack(anchor='w', pady=(4, 10))
        footer = tk.Frame(frame, bg=C_PAGE)
        footer.pack(fill='x', pady=(10, 0))
        ttk.Button(footer, text="Cerrar", style='Accent.TButton', command=self.destroy).pack(anchor='e')

    def _on_close(self):
        self.destroy()


# ==========================================================================
# Instalación en caliente de 'reportlab' (dependencia opcional para el
# informe de Hashear en PDF)
# ==========================================================================
class ReportlabInstallDialog(tk.Toplevel):
    """Pantalla simple: confirmación → 'pip install reportlab' corriendo
    en un hilo de fondo (progreso indeterminado, pip no da bytes/porcentaje)
    → éxito o error. Al tener éxito llama a `on_done` (normalmente,
    reintentar la generación del PDF que había fallado)."""

    def __init__(self, parent, on_done=None):
        super().__init__(parent)
        _ensure_styles()
        self.title("Instalar reportlab")
        self.configure(bg=C_PAGE)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.on_done = on_done
        self._done = False

        self.container = tk.Frame(self, bg=C_PAGE, padx=18, pady=14)
        self.container.pack(fill='both', expand=True)
        self._build_view()
        center_window(self, parent)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        threading.Thread(target=self._run_install, daemon=True).start()

    def _fresh_container(self):
        for w in self.winfo_children():
            w.destroy()
        container = tk.Frame(self, bg=C_PAGE, padx=18, pady=14)
        container.pack(fill='both', expand=True)
        return container

    def _build_view(self):
        frame = self.container
        tk.Label(frame, text="Instalando reportlab…", bg=C_PAGE, fg=C_TEXT,
                 font=FONT_TITLE).pack(anchor='w', pady=(0, 10))
        self.stage_var = tk.StringVar(value="pip install reportlab")
        tk.Label(frame, textvariable=self.stage_var, bg=C_PAGE, fg=C_MUTED,
                 font=FONT_BASE, wraplength=380, justify='left').pack(anchor='w', pady=(0, 10))
        self.pb = ttk.Progressbar(frame, orient='horizontal', mode='indeterminate', length=380)
        self.pb.pack(fill='x', pady=(0, 14))
        self.pb.start(12)
        ttk.Button(frame, text="Cerrar", command=self._on_close).pack(anchor='e')

    def _run_install(self):
        try:
            install_reportlab(log_callback=lambda msg: self.after(0, lambda: self.stage_var.set(msg)))
            self.after(0, self._on_success)
        except ReportlabInstallError as e:
            self.after(0, lambda msg=str(e): self._on_error(msg))
        except Exception as e:
            log.exception("Error inesperado instalando reportlab")
            self.after(0, lambda msg=str(e): self._on_error(msg))

    def _on_success(self):
        self._done = True
        frame = self._fresh_container()
        tk.Label(frame, text="✅ reportlab instalado correctamente", bg=C_PAGE, fg=C_TEXT,
                 font=FONT_TITLE).pack(anchor='w', pady=(0, 14))
        ttk.Button(frame, text="Listo", style='Accent.TButton',
                   command=self.destroy).pack(anchor='e')
        if self.on_done:
            self.on_done()

    def _on_error(self, msg: str):
        self._done = True
        messagebox.showerror(
            "No se pudo instalar reportlab",
            f"{msg}\n\nPodés instalarlo a mano abriendo una consola y corriendo:\n"
            f"    pip install reportlab",
            parent=self)
        self.destroy()

    def _on_close(self):
        if not self._done:
            # La instalación de pip corre en el hilo de fondo igual;
            # sólo cerramos la ventana, no la matamos a mitad de camino.
            self.destroy()
            return
        self.destroy()


# ==========================================================================
# "Codex" — diagnóstico completo de ffmpeg (CODEX FFMPEG @ gyan.dev):
# ¿está instalado?, ¿qué versión?, ¿cuál es la última disponible?, y
# ¿está registrado de forma persistente en las variables de entorno
# (usuario / sistema), o sólo quedó agregado en memoria para esta
# sesión de la app?
# ==========================================================================
class CodexInstallDialog(tk.Toplevel):
    """Ventana 'Codex' (chequeo de ffmpeg — el nombre viene del build
    "CODEX FFMPEG" de gyan.dev, nada que ver con OpenAI). Pantalla
    única con todo el estado a la vista (instalado, versión, ubicación,
    variable de entorno persistida) + versión disponible y de dónde se
    descarga, con botones para volver a verificar o instalar/actualizar."""

    def __init__(self, parent, on_done=None):
        super().__init__(parent)
        _ensure_styles()
        self.title("Codex — ffmpeg")
        self.configure(bg=C_PAGE)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.on_done = on_done
        self.cancel_event = threading.Event()
        self._done = False
        self._checking = True  # arranca "Verificando…": todavía no hay self.status
        self._installing = False
        self.status = None  # ffmpeg_setup.FFmpegStatus, una vez chequeado

        self.container = tk.Frame(self, bg=C_PAGE, padx=18, pady=14)
        self.container.pack(fill='both', expand=True)
        self._build_status_view()
        center_window(self, parent)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        threading.Thread(target=self._run_check, daemon=True).start()

    def _fresh_container(self):
        for w in self.winfo_children():
            w.destroy()
        container = tk.Frame(self, bg=C_PAGE, padx=18, pady=14)
        container.pack(fill='both', expand=True)
        return container

    @staticmethod
    def _path_status_text(s) -> str:
        if not s or not s.installed:
            return "— No aplica"
        if s.in_persistent_path:
            where = []
            if s.in_user_path:
                where.append("usuario")
            if s.in_system_path:
                where.append("sistema")
            return f"✅ Persistida ({' + '.join(where)})"
        return "⚠ Sólo esta sesión de la app"

    # -- pantalla única: estado + versión disponible + acciones ------------
    def _build_status_view(self):
        frame = self._fresh_container()
        s = self.status

        tk.Label(frame, text="Estado de ffmpeg (Codex)", bg=C_PAGE, fg=C_TEXT,
                  font=('Segoe UI', 11, 'bold')).pack(anchor='w', pady=(0, 10))

        outer1, card1 = _card(frame)
        outer1.pack(fill='x', pady=(0, 10))
        installed_txt = "Verificando…" if self._checking else ("SÍ" if s.installed else "NO")
        self.row_installed = _kv_row(card1, "Estado", installed_txt)
        self.row_version = _kv_row(card1, "Ver.",
                                     "—" if self._checking else (s.installed_version or "—"))
        loc_txt = "—" if self._checking else (s.bin_dir or "—")
        self.row_location = _kv_row(card1, "Ubicación", loc_txt)
        self.row_pathvar = _kv_row(
            card1, "Variable de Sys.",
            "Verificando…" if self._checking else self._path_status_text(s))

        outer2, card2 = _card(frame)
        outer2.pack(fill='x', pady=(0, 4))
        latest_txt = "—" if self._checking else (s.latest_version or "(sin red)")
        self.row_latest = _kv_row(card2, "Ver. disponible", latest_txt)
        download_lbl = tk.Label(
            card2, text=ffmpeg_setup.FFMPEG_WIN_URL, bg=C_CARD_BG, fg=C_ACCENT,
            font=FONT_SMALL, wraplength=300, justify='right', cursor='hand2')
        download_lbl.bind('<Button-1>', lambda e: self._open_download_url())
        self.row_download = _kv_row(card2, "Download", download_lbl)

        if self._checking or self._installing:
            tk.Frame(frame, bg=C_PAGE, height=6).pack()
            pb = ttk.Progressbar(frame, orient='horizontal',
                                   mode='indeterminate' if self._checking else 'determinate',
                                   length=420)
            if self._installing:
                self.progress_var = tk.DoubleVar(value=0)
                pb.configure(variable=self.progress_var, maximum=100)
                self.stage_var = tk.StringVar(value="Conectando…")
                tk.Label(frame, textvariable=self.stage_var, bg=C_PAGE, fg=C_MUTED,
                          font=FONT_SMALL).pack(anchor='w', pady=(6, 0))
            pb.pack(fill='x', pady=(4, 0))
            if self._checking:
                pb.start(12)
            self._progress_bar = pb

        footer = tk.Frame(frame, bg=C_PAGE)
        footer.pack(fill='x', pady=(16, 0))
        ttk.Separator(footer).pack(fill='x', pady=(0, 10))
        btns = tk.Frame(footer, bg=C_PAGE)
        btns.pack(fill='x')
        busy = self._checking or self._installing
        self.verify_btn = ttk.Button(btns, text="Verificar", command=self._start_check,
                                       state='disabled' if busy else 'normal')
        self.verify_btn.pack(side='left')
        self.install_btn = ttk.Button(
            btns, text="Instalar / Actualizar", style='Accent.TButton',
            command=self._start_install, state='disabled' if busy else 'normal')
        self.install_btn.pack(side='right')
        self.close_btn = ttk.Button(
            btns, text="Cancelar" if self._installing else "Cerrar", command=self._on_close)
        self.close_btn.pack(side='right', padx=(0, 8))

    def _open_download_url(self):
        import webbrowser
        webbrowser.open(ffmpeg_setup.FFMPEG_WIN_URL)

    # -- verificar -----------------------------------------------------
    def _start_check(self):
        self._checking = True
        self._build_status_view()
        threading.Thread(target=self._run_check, daemon=True).start()

    def _run_check(self):
        try:
            status = ffmpeg_setup.check_status()
        except Exception:
            log.exception("Error chequeando ffmpeg")
            status = ffmpeg_setup.FFmpegStatus(False, None, None, None, False, False, False)
        self.after(0, lambda: self._on_check_done(status))

    def _on_check_done(self, status):
        self.status = status
        self._checking = False
        self._build_status_view()

    # -- instalar / actualizar ------------------------------------------
    def _start_install(self):
        self._installing = True
        self.cancel_event.clear()
        self._build_status_view()
        threading.Thread(target=self._run_install, daemon=True).start()

    def _run_install(self):
        def progress(p: ffmpeg_setup.DownloadProgress):
            self.after(0, lambda: self._update_progress_ui(p))

        try:
            bin_dir = ffmpeg_setup.download_and_install(
                progress_callback=progress, cancel_check=self.cancel_event.is_set)
            self.after(0, lambda: self._on_install_success(bin_dir))
        except ffmpeg_setup.FFmpegInstallCancelled:
            self.after(0, self._on_install_cancelled)
        except ffmpeg_setup.FFmpegInstallError as e:
            self.after(0, lambda msg=str(e): self._on_install_error(msg))
        except Exception as e:
            log.exception("Error inesperado instalando ffmpeg (Codex)")
            self.after(0, lambda msg=str(e): self._on_install_error(msg))

    def _update_progress_ui(self, p: 'ffmpeg_setup.DownloadProgress'):
        labels = {'download': "Descargando", 'extract': "Extrayendo", 'path': "Configurando PATH"}
        if not hasattr(self, 'stage_var'):
            return
        if p.stage == 'download' and p.bytes_total:
            mb_done = p.bytes_done / (1024 * 1024)
            mb_total = p.bytes_total / (1024 * 1024)
            self.stage_var.set(f"{labels[p.stage]}… {mb_done:.1f} / {mb_total:.1f} MB")
        else:
            self.stage_var.set(f"{labels.get(p.stage, p.stage)}…")
        self.progress_var.set(p.percent)

    def _on_install_success(self, bin_dir: str):
        self._done = True
        self._installing = False
        messagebox.showinfo(
            "ffmpeg", "ffmpeg se instaló/actualizó correctamente.", parent=self)
        if self.on_done:
            self.on_done()
        self._start_check()  # refresca Estado/Ver./Ubicación con lo recién instalado

    def _on_install_cancelled(self):
        self._done = True
        self._installing = False
        self._start_check()

    def _on_install_error(self, msg: str):
        self._done = True
        self._installing = False
        messagebox.showerror(
            "Error instalando ffmpeg",
            f"{msg}\n\nEl detalle completo quedó guardado en el log de la app "
            f"(menú Ayuda o carpeta %LOCALAPPDATA%\\sd_hik_reader\\logs\\app.log).",
            parent=self)
        self._start_check()

    def _on_close(self):
        if self._installing and not self._done:
            if not messagebox.askyesno(
                "Instalación en curso", "¿Cancelar la instalación de ffmpeg?", parent=self
            ):
                return
            self.cancel_event.set()
            return
        self.destroy()


# ==========================================================================
# Resultado del chequeo previo de la SD (archivos faltantes / índice
# desincronizado respecto a los contenedores mp4)
# ==========================================================================
def show_preflight_report(parent, report):
    """`report` es un core.sd_check.PreflightReport. Sólo informativo:
    no bloquea nada, el usuario puede seguir usando la lista de clips
    y descargar lo que sí esté disponible."""
    win = tk.Toplevel(parent)
    _ensure_styles()
    win.title("Chequeo de la tarjeta SD")
    win.configure(bg=C_PAGE)
    win.resizable(False, False)
    win.transient(parent)
    win.grab_set()

    frame = tk.Frame(win, bg=C_PAGE, padx=18, pady=14)
    frame.pack(fill='both', expand=True)

    tk.Label(frame, text="⚠ Se encontraron inconsistencias en la tarjeta", bg=C_PAGE,
              fg=C_TEXT, font=('Segoe UI', 11, 'bold')).pack(anchor='w', pady=(0, 8))
    tk.Label(
        frame, bg=C_PAGE, fg=C_MUTED, font=FONT_BASE, justify='left', wraplength=460,
        text=(f"De {report.total_segments} clips en el índice, {report.ok_segments} están "
              f"en buen estado. Podés seguir usando la app con normalidad: los que están "
              f"bien se pueden ver y descargar igual."),
    ).pack(anchor='w', pady=(0, 12))

    if report.segments_missing_file:
        outer, card = _card(frame)
        outer.pack(fill='x', pady=(0, 8))
        _kv_row(card, "Clips sin archivo de video en la SD",
                str(report.segments_missing_file))
        _kv_row(card, "Archivos referenciados que faltan",
                str(len(report.missing_files)))

    if report.segments_offset_mismatch:
        outer2, card2 = _card(frame)
        outer2.pack(fill='x', pady=(0, 8))
        _kv_row(card2, "Clips con índice desincronizado del contenedor",
                str(report.segments_offset_mismatch))
        tk.Label(
            card2, bg=C_CARD_BG, fg=C_MUTED, font=FONT_SMALL, justify='left', wraplength=420,
            text=("El índice dice que el clip ocupa bytes que el archivo .mp4 real "
                  "no tiene (archivo truncado o sobrescrito parcialmente)."),
        ).pack(anchor='w', pady=(4, 0))

    if report.missing_files:
        tk.Label(frame, text="Archivos faltantes:", bg=C_PAGE, fg=C_TEXT,
                  font=FONT_BOLD).pack(anchor='w', pady=(4, 2))
        list_frame = tk.Frame(frame, bg=C_PAGE)
        list_frame.pack(fill='both', pady=(0, 10))
        scrollbar = ttk.Scrollbar(list_frame, orient='vertical')
        listbox = tk.Listbox(list_frame, height=min(8, len(report.missing_files)),
                              width=64, yscrollcommand=scrollbar.set, font=FONT_SMALL)
        scrollbar.config(command=listbox.yview)
        for path in report.missing_files:
            listbox.insert('end', path)
        listbox.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')

    ttk.Button(frame, text="Entendido", style='Accent.TButton',
               command=win.destroy).pack(anchor='e', pady=(6, 0))

    center_window(win, parent)
