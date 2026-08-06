#!/usr/bin/env python3
"""
gui/app.py

Ventana principal. Arma los paneles (Info, Filtro, Lista de clips,
Línea de Tiempo, Vista previa, Detalles), la barra de menú, y expone
los métodos que usan tanto los botones como los ítems de menú.
"""

import logging
import os
import threading
import tkinter as tk
from datetime import datetime
from tkinter import ttk, filedialog, messagebox, scrolledtext
from typing import Optional

from core import config as cfgmod
from core import ffmpeg_setup
from core import sd_check
from core.parser import EZVIZIndexParser, EZVIZParseError, Segment
from gui.menu import build_menu
from gui.dialogs import C_PAGE, _ensure_styles

log = logging.getLogger("ezviz_reader.app")

try:
    import cv2
    from PIL import Image, ImageTk
    PREVIEW_AVAILABLE = True
except ImportError:
    PREVIEW_AVAILABLE = False


class SDReaderApp:
    APP_TITLE = "Lector de Tarjetas SD (EZVIZ / Hikvision)"

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(self.APP_TITLE)

        # Config persistente (tamaño de ventana, paneles ocultos,
        # últimas carpetas usadas) — ver core/config.py.
        self.config = cfgmod.load_config()
        win_cfg = self.config['window']
        self.root.geometry(f"{win_cfg['width']}x{win_cfg['height']}")
        self.root.minsize(1000, 600)

        # Mismo gris para toda la app (ver gui/dialogs._ensure_styles):
        # se llama de nuevo acá por las dudas (no hace nada si ya se
        # llamó desde main.py), y se usa la misma constante C_PAGE en
        # vez de una consulta al tema, para que quede exactamente
        # igual al de "Acerca de" y el resto de los diálogos.
        _ensure_styles()
        self.root.configure(bg=C_PAGE)

        self.parser: Optional[EZVIZIndexParser] = None
        self.segments: list[Segment] = []
        self.selected_segment: Optional[Segment] = None
        self.cancel_batch = threading.Event()
        self.last_extract_dir: Optional[str] = self.config['paths'].get('last_extract_dir') or None
        self.last_source_dir: Optional[str] = self.config['paths'].get('last_source_dir') or None

        # -- estado del reproductor de vista previa --
        self.preview_cap = None
        self.preview_playing = False
        self.preview_fps = 25.0
        self.preview_photo = None
        self.preview_after_id = None
        self.preview_duration = 0.0
        self.preview_seeking = False

        # -- estado de visibilidad de paneles (menú Ver) --
        panels_cfg = self.config['panels']
        self.show_info = tk.BooleanVar(value=panels_cfg['show_info'])
        self.show_filter = tk.BooleanVar(value=panels_cfg['show_filter'])
        self.show_timeline = tk.BooleanVar(value=panels_cfg['show_timeline'])
        self.show_preview = tk.BooleanVar(value=panels_cfg['show_preview'])
        self.show_detail = tk.BooleanVar(value=panels_cfg['show_detail'])

        # Días expandidos en la línea de tiempo (por defecto, todos
        # contraídos: la primera vista muestra sólo la lista de fechas).
        self._timeline_expanded_days: set = set()

        self.setup_ui()
        build_menu(self)

        # Aplica el estado de paneles restaurado de la config (setup_ui
        # los deja todos visibles por defecto).
        self._relayout_optional_panels()
        self.toggle_timeline_tab()
        self.toggle_preview_tab()
        self.toggle_detail_panel()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # Se dispara con un pequeño delay para que la ventana principal
        # ya esté dibujada antes de mostrar el aviso/diálogo de ffmpeg.
        self.root.after(200, self._check_ffmpeg)

    # ----------------------------------------------------------------
    # Ciclo de vida
    # ----------------------------------------------------------------
    def on_close(self):
        self.stop_preview()
        self._save_window_state()
        self.root.destroy()

    def _save_window_state(self):
        """Persiste tamaño de ventana actual y estado de paneles.
        Recarga la config del disco antes de escribir para no pisar
        rutas (last_source_dir, etc.) que ya se hayan guardado
        durante la sesión desde otros lugares."""
        cfg = cfgmod.load_config()
        try:
            cfg['window']['width'] = self.root.winfo_width()
            cfg['window']['height'] = self.root.winfo_height()
        except tk.TclError:
            pass
        cfg['panels'] = {
            'show_info': self.show_info.get(),
            'show_filter': self.show_filter.get(),
            'show_timeline': self.show_timeline.get(),
            'show_preview': self.show_preview.get(),
            'show_detail': self.show_detail.get(),
        }
        cfgmod.save_config(cfg)

    def _check_ffmpeg(self):
        """Al iniciar: si no se encuentra ffmpeg (ni en el PATH ni en
        una instalación previa hecha por esta misma app), ofrece
        descargarlo e instalarlo automáticamente (sólo Windows)."""
        if ffmpeg_setup.ensure_on_session_path():
            return  # ya estaba, o lo encontramos en nuestra carpeta local

        if ffmpeg_setup.IS_WINDOWS:
            if messagebox.askyesno(
                "ffmpeg no encontrado",
                "No se detectó 'ffmpeg' en el PATH.\n"
                "La extracción de video/thumbnails y la vista previa no van a "
                "funcionar hasta que lo instales.\n\n"
                "¿Querés que lo descargue e instale ahora automáticamente?",
            ):
                from gui.dialogs import FFmpegInstallDialog
                FFmpegInstallDialog(self.root)
        else:
            messagebox.showwarning(
                "ffmpeg no encontrado",
                "No se detectó 'ffmpeg' en el PATH.\n"
                "La extracción de video/thumbnails no funcionará hasta que lo instales "
                "(por ejemplo: 'sudo apt install ffmpeg' o 'brew install ffmpeg')."
            )

    # ----------------------------------------------------------------
    # Construcción de la interfaz
    # ----------------------------------------------------------------
    def setup_ui(self):
        top_frame = ttk.Frame(self.root, padding=10)
        top_frame.pack(fill='x')

        ttk.Label(top_frame, text="Origen (SD):", font=('Segoe UI', 10)).pack(side='left')
        self.path_var = tk.StringVar(value=self.last_source_dir or "")
        ttk.Entry(top_frame, textvariable=self.path_var, width=60).pack(side='left', padx=5)
        ttk.Button(top_frame, text="📁 Examinar", command=self.browse_folder).pack(side='left')
        self.load_btn = ttk.Button(top_frame, text="🔍 Cargar", command=self.load_data)
        self.load_btn.pack(side='left', padx=5)

        # Separador vertical entre el grupo "cargar origen" y las acciones
        # de inspección/formateo de la tarjeta.
        ttk.Separator(top_frame, orient='vertical').pack(side='left', fill='y', padx=10, pady=2)

        ttk.Button(top_frame, text="👁 Ver", command=self.open_drive_info).pack(side='left')
        ttk.Button(top_frame, text="🗺 Mapa", command=self.open_segment_map).pack(side='left', padx=5)
        ttk.Button(top_frame, text="🧹 Formatear", command=self.open_format_dialog).pack(side='left', padx=5)

        # Separador vertical entre "Formatear" y "Hashear".
        ttk.Separator(top_frame, orient='vertical').pack(side='left', fill='y', padx=10, pady=2)

        ttk.Button(top_frame, text="🔑 Hashear", command=self.open_hash_dialog).pack(side='left', padx=5)

        # --- Panel: Info (togglable) ---
        self.info_frame = ttk.LabelFrame(self.root, text="Información del Dispositivo", padding=10)
        self.info_labels = {}
        info_items = ['Serial', 'MAC', 'DataDirs', 'Archivos', 'Segmentos']
        for i, item in enumerate(info_items):
            ttk.Label(self.info_frame, text=f"{item}:").grid(row=0, column=i * 2, sticky='e')
            self.info_labels[item] = ttk.Label(self.info_frame, text="-", font=('Segoe UI', 9, 'bold'))
            self.info_labels[item].grid(row=0, column=i * 2 + 1, sticky='w', padx=5)

        # --- Panel: Filtro (togglable) ---
        self.filter_frame = ttk.LabelFrame(self.root, text="Filtros de Fecha", padding=10)
        ttk.Label(self.filter_frame, text="Desde:").pack(side='left')
        self.from_date = tk.StringVar(value="")
        ttk.Entry(self.filter_frame, textvariable=self.from_date, width=18).pack(side='left', padx=5)
        ttk.Label(self.filter_frame, text="Hasta:").pack(side='left')
        self.to_date = tk.StringVar(value="")
        ttk.Entry(self.filter_frame, textvariable=self.to_date, width=18).pack(side='left', padx=5)
        ttk.Button(self.filter_frame, text="🔄 Filtrar", command=self.apply_filter).pack(side='left', padx=5)
        ttk.Button(self.filter_frame, text="❌ Limpiar", command=self.clear_filter).pack(side='left')
        ttk.Label(self.filter_frame, text="(Formato: YYYY-MM-DD HH:MM:SS)").pack(side='left', padx=10)

        # Empaquetar los paneles opcionales según su estado inicial
        self._relayout_optional_panels()

        main_paned = ttk.PanedWindow(self.root, orient='horizontal')
        main_paned.pack(fill='both', expand=True, padx=10, pady=5)
        self.main_paned = main_paned

        # --- Lista de clips (siempre visible) ---
        list_frame = ttk.LabelFrame(main_paned, text="Clips de Grabación", padding=5)

        columns = ('#', 'Fecha Inicio', 'Fecha Fin', 'Duración', 'Archivo', 'Offset Inicio', 'Offset Fin')
        self.tree = ttk.Treeview(list_frame, columns=columns, show='headings', height=20)
        for col in columns:
            self.tree.heading(col, text=col)
            self.tree.column(col, width=100 if col == '#' else 150)
        self.tree.column('#', width=50, anchor='center')
        self.tree.column('Duración', width=80, anchor='center')

        vsb = ttk.Scrollbar(list_frame, orient='vertical', command=self.tree.yview)
        hsb = ttk.Scrollbar(list_frame, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        list_frame.grid_rowconfigure(0, weight=1)
        list_frame.grid_columnconfigure(0, weight=1)

        btn_frame = ttk.Frame(list_frame)
        btn_frame.grid(row=2, column=0, columnspan=2, pady=5)

        self.extract_btn = ttk.Button(btn_frame, text="⬇ Descargar", command=self.download_selected)
        self.extract_btn.pack(side='left', padx=2)
        self.preview_btn = ttk.Button(btn_frame, text="👁 Vista Previa", command=self.start_preview)
        self.preview_btn.pack(side='left', padx=2)
        self.thumb_btn = ttk.Button(btn_frame, text="🖼 Extraer JPG", command=self.extract_thumbnail)
        self.thumb_btn.pack(side='left', padx=2)
        self.batch_btn = ttk.Button(btn_frame, text="📦 Extraer Todos (rango)", command=self.extract_range)
        self.batch_btn.pack(side='left', padx=2)
        self.cancel_btn = ttk.Button(btn_frame, text="🛑 Cancelar", command=self.cancel_extraction, state='disabled')
        self.cancel_btn.pack(side='left', padx=2)

        main_paned.add(list_frame, weight=3)

        # --- Panel derecho: Notebook (Timeline + Preview) + Detalles ---
        right_frame = ttk.Frame(main_paned)
        self.right_frame = right_frame

        right_notebook = ttk.Notebook(right_frame)
        right_notebook.pack(fill='both', expand=True, pady=5)
        self.right_notebook = right_notebook

        # -- Pestaña: Línea de Tiempo --
        self.timeline_tab = ttk.Frame(right_notebook)
        right_notebook.add(self.timeline_tab, text="Línea de Tiempo")

        self.timeline_canvas = tk.Canvas(
            self.timeline_tab, bg=C_PAGE,
            highlightthickness=1, highlightbackground='#b0b0b0')
        timeline_vsb = ttk.Scrollbar(self.timeline_tab, orient='vertical', command=self.timeline_canvas.yview)
        self.timeline_canvas.configure(yscrollcommand=timeline_vsb.set)
        self.timeline_canvas.pack(side='left', fill='both', expand=True)
        timeline_vsb.pack(side='right', fill='y')
        self.timeline_canvas.bind('<Configure>', self.draw_timeline)

        # -- Pestaña: Vista Previa --
        self.preview_tab = ttk.Frame(right_notebook)
        right_notebook.add(self.preview_tab, text="Vista Previa")

        self.preview_canvas = tk.Canvas(self.preview_tab, bg='black', highlightthickness=0)
        self.preview_canvas.pack(fill='both', expand=True, padx=5, pady=(5, 0))
        self.preview_canvas.bind('<Configure>', self._on_preview_canvas_resize)

        preview_controls = ttk.Frame(self.preview_tab)
        preview_controls.pack(fill='x', padx=5, pady=5)

        self.preview_playpause_btn = ttk.Button(
            preview_controls, text="▶", width=3, command=self.toggle_preview_play, state='disabled')
        self.preview_playpause_btn.pack(side='left')
        self.preview_stop_btn = ttk.Button(
            preview_controls, text="⏹", width=3, command=self.stop_preview, state='disabled')
        self.preview_stop_btn.pack(side='left', padx=(2, 8))

        self.preview_time_var = tk.StringVar(value="00:00 / 00:00")
        ttk.Label(preview_controls, textvariable=self.preview_time_var).pack(side='left')

        self.preview_scale_var = tk.DoubleVar(value=0)
        self.preview_scale = ttk.Scale(
            preview_controls, from_=0, to=100, orient='horizontal', variable=self.preview_scale_var)
        self.preview_scale.pack(side='left', fill='x', expand=True, padx=8)
        self.preview_scale.bind('<ButtonPress-1>', self._on_preview_seek_start)
        self.preview_scale.bind('<ButtonRelease-1>', self._on_preview_seek_end)

        if not PREVIEW_AVAILABLE:
            ttk.Label(
                self.preview_tab,
                text="⚠ Vista previa no disponible: instalá 'opencv-python-headless' y 'pillow'\n"
                     "(pip install opencv-python-headless pillow)",
                foreground='#c0392b',
            ).pack(fill='x', padx=5, pady=(0, 5))

        # -- Detalles (togglable) --
        self.detail_frame = ttk.LabelFrame(right_frame, text="Detalles del Clip", padding=10)
        self.detail_text = scrolledtext.ScrolledText(self.detail_frame, height=8, wrap=tk.WORD)
        self.detail_text.pack(fill='both', expand=True)
        self.detail_frame.pack(fill='x', pady=5)

        self.progress = ttk.Progressbar(right_frame, mode='determinate', maximum=100)
        self.progress.pack(fill='x', pady=5)

        self.status_label = ttk.Label(right_frame, text="Listo", anchor='w')
        self.status_label.pack(fill='x')

        main_paned.add(right_frame, weight=2)

        self.tree.bind('<<TreeviewSelect>>', self.on_select)
        self.tree.bind('<Double-1>', self.on_double_click)
        self.tree.bind('<Button-3>', self._show_tree_context_menu)

    # ----------------------------------------------------------------
    # Menú "Ver": mostrar/ocultar paneles
    # ----------------------------------------------------------------
    def _relayout_optional_panels(self):
        """Vuelve a empaquetar Info y Filtro en orden fijo, según su
        BooleanVar. Se llama al iniciar y cada vez que se togglea uno."""
        self.info_frame.pack_forget()
        self.filter_frame.pack_forget()
        if self.show_info.get():
            self.info_frame.pack(fill='x', padx=10, pady=5, before=self.main_paned if hasattr(self, 'main_paned') else None)
        if self.show_filter.get():
            self.filter_frame.pack(fill='x', padx=10, pady=5, before=self.main_paned if hasattr(self, 'main_paned') else None)

    def toggle_info_panel(self):
        self._relayout_optional_panels()

    def toggle_filter_panel(self):
        self._relayout_optional_panels()

    def toggle_timeline_tab(self):
        if self.show_timeline.get():
            # .add() en una pestaña ya gestionada por el Notebook (aunque
            # esté oculta con .hide()) la vuelve a mostrar en su posición
            # anterior; no hace falta (ni sirve) chequear .tabs().
            self.right_notebook.add(self.timeline_tab, text="Línea de Tiempo")
        else:
            self.right_notebook.hide(self.timeline_tab)

    def toggle_preview_tab(self):
        if self.show_preview.get():
            self.right_notebook.add(self.preview_tab, text="Vista Previa")
        else:
            if self.preview_playing:
                self.stop_preview()
            self.right_notebook.hide(self.preview_tab)

    def toggle_detail_panel(self):
        self.detail_frame.pack_forget()
        if self.show_detail.get():
            self.detail_frame.pack(fill='x', pady=5, before=self.progress)

    def toggle_timeline_orientation(self):
        self.draw_timeline()

    # ----------------------------------------------------------------
    # Helpers de estado (thread-safe)
    # ----------------------------------------------------------------
    def set_status(self, text: str):
        self.root.after(0, lambda: self.status_label.config(text=text))

    def set_progress(self, value: float):
        self.root.after(0, lambda: self.progress.config(value=value))

    def set_buttons_busy(self, busy: bool):
        state = 'disabled' if busy else 'normal'
        for btn in (self.load_btn, self.extract_btn,
                    self.thumb_btn, self.batch_btn, self.preview_btn):
            btn.config(state=state)
        self.cancel_btn.config(state='normal' if busy else 'disabled')

    # ----------------------------------------------------------------
    # Menú "SD": selección de origen y carga
    # ----------------------------------------------------------------
    def browse_folder(self):
        folder = filedialog.askdirectory(initialdir=self.last_source_dir or None)
        if folder:
            self.path_var.set(folder)
            self.last_source_dir = folder
            cfgmod.set_last_dir('last_source_dir', folder)

    def select_drive_path(self, path: str):
        """Usado por el menú SD al elegir una unidad detectada."""
        self.path_var.set(path)

    def open_drive_info(self):
        """Botón '👁 Ver': muestra la ventana de información de la tarjeta
        (volumen + índice EZVIZ si ya se cargó)."""
        from gui.dialogs import DriveInfoDialog
        DriveInfoDialog(self.root, self)

    def open_segment_map(self):
        """Menú 'Ver > Mapa de segmentos': grilla agrupada por bloques de
        32 slots (por avFile) + resumen de ocupación de todos los avFiles."""
        from gui.dialogs import SegmentMapDialog
        if self.parser is None:
            messagebox.showinfo(
                "Mapa de segmentos",
                "Primero elegí la carpeta de la tarjeta SD y tocá \"🔍 Cargar\".",
                parent=self.root)
            return
        SegmentMapDialog(self.root, self)

    def open_format_dialog(self):
        """Botón '🧹 Formatear': abre el formateador de bajo nivel.
        Comparte lógica con el ítem de menú Formatear."""
        from gui.dialogs import FormatDialog
        if self.parser is not None or self.preview_playing:
            self.stop_preview()
        FormatDialog(self.root, initial_path=self.path_var.get())

    def open_hash_dialog(self):
        """Botón '🔑 Hashear' (y menú Hashear / Extraer): abre el
        wizard de verificación de integridad. Preferimos la última
        carpeta de extracción como punto de partida; si no hay
        ninguna, usamos la carpeta de origen (SD) actual."""
        from gui.dialogs import HashDialog
        initial_dir = self.last_extract_dir or self.path_var.get()
        HashDialog(self.root, initial_dir=initial_dir)

    def load_data(self):
        path = self.path_var.get()
        if not path or not os.path.isdir(path):
            messagebox.showerror("Error", "Seleccioná una carpeta válida")
            return

        try:
            self.status_label.config(text="Cargando índices...")
            self.root.update()

            self.parser = EZVIZIndexParser(path, 'video')
            self.segments = self.parser.get_segments()
            self._timeline_expanded_days = set()

            preflight = sd_check.run_preflight(self.segments)

            serial = self.parser.info.get('serialNumber', b'').decode('ascii', 'ignore').strip('\x00')
            mac_bytes = self.parser.info.get('MACAddr', b'\x00' * 6)
            mac = ':'.join(f'{b:02x}' for b in mac_bytes)

            self.info_labels['Serial'].config(text=serial or 'N/A')
            self.info_labels['MAC'].config(text=mac)
            self.info_labels['DataDirs'].config(text=str(self.parser.info.get('DataDirs', 1)))
            self.info_labels['Archivos'].config(text=str(self.parser.header.get('avFiles', 0)))
            self.info_labels['Segmentos'].config(text=str(len(self.segments)))

            self.refresh_tree()
            self.draw_timeline()

            if self.segments:
                self.status_label.config(text=f"Cargados {len(self.segments)} segmentos")
            else:
                self.status_label.config(text="No se encontraron clips en esta tarjeta")

            if preflight.has_issues:
                from gui.dialogs import show_preflight_report
                show_preflight_report(self.root, preflight)

        except (EZVIZParseError, ValueError, OSError) as e:
            messagebox.showerror("Error", f"No se pudo cargar:\n{e}")
            self.status_label.config(text="Error al cargar")
        except Exception as e:
            log.exception("Error inesperado cargando datos")
            messagebox.showerror("Error inesperado", str(e))
            self.status_label.config(text="Error al cargar")

    def refresh_tree(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

        for i, seg in enumerate(self.segments):
            start = seg.startTimeDt.strftime('%Y-%m-%d %H:%M:%S')
            end = seg.endTimeDt.strftime('%Y-%m-%d %H:%M:%S')
            duration = f"{seg.duration:.1f}s"
            file_name = os.path.basename(seg.filePath)

            self.tree.insert('', 'end', values=(i, start, end, duration, file_name,
                                                  seg.startOffset, seg.endOffset))

    # ----------------------------------------------------------------
    # Línea de tiempo (Día > Hora, con días contraíbles)
    # ----------------------------------------------------------------
    def draw_timeline(self, event=None):
        """Línea de tiempo vertical agrupada Día > Hora. Los días arrancan
        contraídos (primera vista: sólo la lista de fechas en línea); un
        clic en la fecha expande/contrae sus horas. Dentro de cada hora,
        cada clip es un 'tick' (estilo gráfico de velas) ubicado según el
        minuto en que empezó."""
        canvas = self.timeline_canvas
        canvas.delete('all')

        if not self.segments:
            canvas.create_text(canvas.winfo_width() / 2, 30,
                                text="No hay datos cargados", fill='#666', font=('Segoe UI', 11))
            canvas.configure(scrollregion=(0, 0, canvas.winfo_width(), 60))
            return

        width = max(canvas.winfo_width(), 260)
        colors = ['#2e7d32', '#1565c0', '#e65100', '#ad1457', '#6a1b9a']

        margin_top = 20
        rail_x = 40
        day_row_h = 26
        hour_row_h = 24
        day_gap = 12

        # Agrupar: día -> hora -> [(índice en self.segments, segmento)]
        days = {}
        for i, seg in enumerate(self.segments):
            d = seg.startTimeDt.date()
            h = seg.startTimeDt.hour
            days.setdefault(d, {}).setdefault(h, []).append((i, seg))

        strip_x0 = rail_x + 70
        strip_x1 = max(width - 50, strip_x0 + 60)
        strip_w = strip_x1 - strip_x0

        y = margin_top
        prev_y = margin_top - 10  # punto de partida del riel

        for day in sorted(days.keys()):
            hours = days[day]
            expanded = day in self._timeline_expanded_days
            total_clips_day = sum(len(v) for v in hours.values())

            # Tramo de riel hasta este día
            canvas.create_line(rail_x, prev_y, rail_x, y, fill='#aaa', width=2)

            # Fondo del renglón completo (verde clarito cuando está expandido)
            if expanded:
                canvas.create_rectangle(
                    0, y - day_row_h / 2, width, y + day_row_h / 2,
                    fill='#e8f5e9', outline='')

            arrow = '▾' if expanded else '▸'

            circle = canvas.create_oval(
                rail_x - 5, y - 5, rail_x + 5, y + 5,
                fill='#2e7d32' if expanded else '#999', outline='', tags=('day_circle',))
            canvas.create_text(
                rail_x + 16, y, anchor='w',
                text=f"{arrow} {day.strftime('%d/%m')}",
                fill='#1a1a1a', font=('Segoe UI', 10, 'bold'),
            )
            canvas.create_text(
                rail_x + 118, y, anchor='w',
                text=f"{total_clips_day} clip{'s' if total_clips_day != 1 else ''} · "
                     f"{len(hours)} hora{'s' if len(hours) != 1 else ''}",
                fill='#888', font=('Segoe UI', 8, 'italic'),
            )

            # Área de clic para todo el renglón del día (encima del texto y
            # el círculo, para que hacer clic en cualquier parte de la
            # línea —no sólo después de la fecha— expanda/contraiga).
            head_hit = canvas.create_rectangle(
                0, y - day_row_h / 2, width, y + day_row_h / 2, fill='', outline='')
            canvas.tag_bind(head_hit, '<Button-1>', lambda e, d=day: self._toggle_timeline_day(d))
            canvas.tag_bind(circle, '<Button-1>', lambda e, d=day: self._toggle_timeline_day(d))

            prev_y = y
            y += day_row_h

            if expanded:
                for hour in sorted(hours.keys()):
                    clips_in_hour = hours[hour]

                    canvas.create_line(rail_x, prev_y, rail_x, y, fill='#aaa', width=2)
                    canvas.create_line(rail_x, y, rail_x + 12, y, fill='#aaa')
                    canvas.create_text(
                        rail_x + 18, y, anchor='w', text=f"{hour:02d}:00",
                        fill='#333', font=('Segoe UI', 9),
                    )

                    # Franja de fondo de la hora (el "eje" de 0 a 60 minutos)
                    canvas.create_rectangle(
                        strip_x0, y - 8, strip_x1, y + 8, fill='#e6e6e6', outline='#cfcfcf')

                    for idx, seg in clips_in_hour:
                        minute = seg.startTimeDt.minute + seg.startTimeDt.second / 60.0
                        x = strip_x0 + (minute / 60.0) * strip_w
                        # Alto tipo "vela": más largo cuanto más dura el clip
                        # (acotado para que un clip largo no tape a los demás).
                        tick_h = min(max(seg.duration / 8.0, 5), 15)
                        color = colors[idx % len(colors)]

                        tick = canvas.create_rectangle(
                            x - 1.5, y - tick_h / 2, x + 1.5, y + tick_h / 2,
                            fill=color, outline='')
                        canvas.tag_bind(tick, '<Button-1>', lambda e, i=idx: self.select_segment(i))
                        # Área de clic un poco más generosa que el tick visual
                        hit = canvas.create_rectangle(
                            x - 4, y - hour_row_h / 2, x + 4, y + hour_row_h / 2,
                            fill='', outline='')
                        canvas.tag_bind(hit, '<Button-1>', lambda e, i=idx: self.select_segment(i))

                    canvas.create_text(
                        strip_x1 + 8, y, anchor='w',
                        text=f"{len(clips_in_hour)} clip" + ("s" if len(clips_in_hour) != 1 else ""),
                        fill='#777', font=('Segoe UI', 8),
                    )

                    prev_y = y
                    y += hour_row_h

            y += day_gap

        # Los círculos de día van siempre por encima de las líneas del
        # riel (una línea nunca debe tapar un círculo).
        canvas.tag_raise('day_circle')

        canvas.configure(scrollregion=(0, 0, width, y + 10))

    def _toggle_timeline_day(self, day):
        if day in self._timeline_expanded_days:
            self._timeline_expanded_days.discard(day)
        else:
            self._timeline_expanded_days.add(day)
        self.draw_timeline()

    def select_segment(self, index):
        if 0 <= index < len(self.segments):
            item = self.tree.get_children()[index]
            self.tree.selection_set(item)
            self.tree.see(item)
            self.on_select()

    def on_select(self, event=None):
        selection = self.tree.selection()
        if not selection:
            self.selected_segment = None
            return

        if len(selection) == 1:
            item = selection[0]
            values = self.tree.item(item, 'values')
            idx = int(values[0])
            seg = self.segments[idx]
            self.selected_segment = seg

            details = (
                f"📹 Clip #{idx}\n"
                f"📁 Archivo: {seg.filePath}\n"
                f"🕐 Inicio: {seg.startTimeDt.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
                f"🕑 Fin: {seg.endTimeDt.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
                f"⏱️  Duración: {seg.duration:.2f} segundos\n"
                f"📍 Offset: {seg.startOffset} → {seg.endOffset}\n"
                f"📐 Resolución: {seg.resolution}\n"
                f"📊 Tipo: {seg.type} | Estado: {seg.status}\n"
            )
        else:
            segs = self._get_selected_segments()
            self.selected_segment = segs[0] if segs else None
            total_duration = sum(s.duration for s in segs)
            details = (
                f"📦 {len(segs)} clips seleccionados\n"
                f"⏱️  Duración total: {total_duration:.2f} segundos\n\n"
                f"Presioná '⬇ Descargar' (o clic derecho > Descargar) para\n"
                f"guardarlos todos en una carpeta.\n"
            )

        self.detail_text.delete('1.0', tk.END)
        self.detail_text.insert('1.0', details)

    def on_double_click(self, event):
        # Doble clic = vista previa (no descarga)
        row_id = self.tree.identify_row(event.y)
        if row_id:
            self.tree.selection_set(row_id)
            self.on_select()
        self.start_preview()

    def _get_selected_segments(self) -> list:
        segs = []
        for item in self.tree.selection():
            values = self.tree.item(item, 'values')
            idx = int(values[0])
            segs.append(self.segments[idx])
        return segs

    # ----------------------------------------------------------------
    # Menú contextual (clic derecho) sobre el treeview
    # ----------------------------------------------------------------
    def _show_tree_context_menu(self, event):
        row_id = self.tree.identify_row(event.y)
        if row_id and row_id not in self.tree.selection():
            self.tree.selection_set(row_id)
            self.on_select()

        segs = self._get_selected_segments()
        if not segs:
            return

        menu = tk.Menu(self.root, tearoff=0)
        if len(segs) == 1:
            menu.add_command(label="👁 Vista previa", command=self.start_preview)
            menu.add_separator()
        menu.add_command(
            label=f"⬇ Descargar ({len(segs)})" if len(segs) > 1 else "⬇ Descargar",
            command=lambda: self.download_selected(with_audio=False))
        menu.add_command(label="🔊 Descargar con audio", command=lambda: self.download_selected(with_audio=True))
        if len(segs) == 1:
            menu.add_separator()
            menu.add_command(label="🖼 Extraer miniatura (JPG)", command=self.extract_thumbnail)
        menu.add_separator()
        menu.add_command(label="📦 Extraer todos (rango filtrado)", command=self.extract_range)

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ----------------------------------------------------------------
    # Filtro de fechas
    # ----------------------------------------------------------------
    def apply_filter(self):
        if not self.parser:
            messagebox.showwarning("Atención", "Cargá una tarjeta primero")
            return

        try:
            from_time = None
            to_time = None

            if self.from_date.get().strip():
                from_time = datetime.strptime(self.from_date.get().strip(), '%Y-%m-%d %H:%M:%S')
            if self.to_date.get().strip():
                to_time = datetime.strptime(self.to_date.get().strip(), '%Y-%m-%d %H:%M:%S')

            if from_time and to_time and from_time >= to_time:
                messagebox.showerror("Error", "'Desde' debe ser anterior a 'Hasta'")
                return

            self.segments = self.parser.get_segments(from_time, to_time)
            self._timeline_expanded_days = set()
            self.refresh_tree()
            self.draw_timeline()
            self.status_label.config(text=f"Filtrados: {len(self.segments)} segmentos")

        except ValueError:
            messagebox.showerror("Error", "Formato de fecha inválido. Usá: YYYY-MM-DD HH:MM:SS")

    def clear_filter(self):
        self.from_date.set("")
        self.to_date.set("")
        if self.parser:
            self.segments = self.parser.get_segments()
            self._timeline_expanded_days = set()
            self.refresh_tree()
            self.draw_timeline()

    # ----------------------------------------------------------------
    # Menú "Extraer"
    # ----------------------------------------------------------------
    # Alias usados por el menú "Extraer" (gui/menu.py) — quedan disponibles
    # aunque el botón de audio ya no exista en la barra de herramientas.
    def extract_selected(self):
        self.download_selected(with_audio=False)

    def extract_selected_with_audio(self):
        self.download_selected(with_audio=True)

    def download_selected(self, with_audio: bool = False):
        """Descarga los clips seleccionados en el treeview (uno o varios).
        Con un solo clip pide un nombre de archivo; con varios, pide una
        carpeta de destino y usa un nombre automático por clip."""
        segs = self._get_selected_segments()
        if not segs:
            messagebox.showwarning("Atención", "Seleccioná uno o más clips primero")
            return

        if len(segs) == 1:
            seg = segs[0]
            suffix = '_audio' if with_audio else ''
            output = filedialog.asksaveasfilename(
                defaultextension=".mp4",
                filetypes=[("MP4 files", "*.mp4"), ("All files", "*.*")],
                initialdir=self.last_extract_dir or None,
                initialfile=f"clip_{seg.startTimeDt.strftime('%Y%m%d_%H%M%S')}{suffix}.mp4"
            )
            if not output:
                return
            self._run_download(segs, with_audio, single_output=output)
        else:
            output_dir = filedialog.askdirectory(
                title=f"Carpeta de destino para {len(segs)} clips",
                initialdir=self.last_extract_dir or None)
            if not output_dir:
                return
            self._run_download(segs, with_audio, output_dir=output_dir)

    def _run_download(self, segs: list, with_audio: bool,
                       single_output: Optional[str] = None, output_dir: Optional[str] = None):
        self.cancel_batch.clear()
        total = len(segs)

        def do_download():
            self.set_buttons_busy(True)
            ok, failed = 0, 0
            last_path = None

            for i, seg in enumerate(segs):
                if self.cancel_batch.is_set():
                    break
                try:
                    self.set_status(f"Descargando {i + 1}/{total}...")
                    self.set_progress((i / total) * 100)

                    if single_output is not None:
                        dest = single_output
                    else:
                        suffix = '_audio' if with_audio else ''
                        dest = os.path.join(
                            output_dir, f"clip_{seg.startTimeDt.strftime('%Y%m%d_%H%M%S')}{suffix}.mp4")

                    if with_audio:
                        self.parser.extract_segment_mp4_with_audio(seg, dest)
                    else:
                        self.parser.extract_segment_mp4(seg, dest)
                    last_path = dest
                    ok += 1
                except Exception as e:
                    log.warning("Error descargando segmento: %s", e)
                    failed += 1

            self.last_extract_dir = output_dir or (os.path.dirname(single_output) if single_output else None)
            if self.last_extract_dir:
                cfgmod.set_last_dir('last_extract_dir', self.last_extract_dir)
            self.set_progress(100)
            self.set_buttons_busy(False)

            if self.cancel_batch.is_set():
                self.set_status(f"Cancelado. {ok} descargados, {failed} fallidos")
                self.root.after(0, lambda: messagebox.showinfo(
                    "Cancelado", f"Descarga cancelada.\nOK: {ok}  Fallidos: {failed}"))
            elif total == 1:
                if ok:
                    self.set_status(f"✅ Guardado: {last_path}")
                    self.root.after(0, lambda: messagebox.showinfo("Éxito", f"Clip extraído:\n{last_path}"))
                else:
                    self.set_status("❌ Error")
                    self.root.after(0, lambda: messagebox.showerror("Error", "No se pudo extraer el clip"))
            else:
                self.set_status(f"✅ Descarga completa: {ok} OK, {failed} fallidos")
                self.root.after(0, lambda: messagebox.showinfo(
                    "Éxito", f"Se descargaron {ok} de {total} clips"
                    + (f"\n({failed} fallaron, ver consola)" if failed else "")))

        threading.Thread(target=do_download, daemon=True).start()

    def extract_thumbnail(self):
        if self.selected_segment is None:
            messagebox.showwarning("Atención", "Seleccioná un clip primero")
            return

        seg = self.selected_segment
        output = filedialog.asksaveasfilename(
            defaultextension=".jpg",
            filetypes=[("JPEG files", "*.jpg"), ("All files", "*.*")],
            initialdir=self.last_extract_dir or None,
            initialfile=f"thumb_{seg.startTimeDt.strftime('%Y%m%d_%H%M%S')}.jpg"
        )
        if not output:
            return

        def do_extract():
            self.set_buttons_busy(True)
            self.set_status("Generando thumbnail...")
            try:
                self.parser.extract_segment_jpg(seg, output)
                self.last_extract_dir = os.path.dirname(output)
                cfgmod.set_last_dir('last_extract_dir', self.last_extract_dir)
                self.set_status(f"✅ Thumbnail: {output}")
                self.root.after(0, lambda: messagebox.showinfo("Éxito", f"Thumbnail guardado:\n{output}"))
            except Exception as e:
                log.exception("Error generando thumbnail")
                self.set_status("❌ Error")
                msg = str(e)
                self.root.after(0, lambda: messagebox.showerror("Error", msg))
            finally:
                self.set_buttons_busy(False)

        threading.Thread(target=do_extract, daemon=True).start()

    def cancel_extraction(self):
        self.cancel_batch.set()
        self.set_status("Cancelando...")

    def extract_range(self):
        if not self.segments:
            messagebox.showwarning("Atención", "No hay clips cargados")
            return

        output_dir = filedialog.askdirectory(title="Carpeta de destino", initialdir=self.last_extract_dir or None)
        if not output_dir:
            return

        self.cancel_batch.clear()

        def do_extract_all():
            self.set_buttons_busy(True)
            total = len(self.segments)
            ok, failed = 0, 0

            for i, seg in enumerate(self.segments):
                if self.cancel_batch.is_set():
                    break
                try:
                    self.set_status(f"Extrayendo {i + 1}/{total}...")
                    self.set_progress((i / total) * 100)

                    filename = os.path.join(
                        output_dir, f"clip_{seg.startTimeDt.strftime('%Y%m%d_%H%M%S')}.mp4"
                    )
                    self.parser.extract_segment_mp4(seg, filename)
                    ok += 1
                except Exception as e:
                    log.warning("Error en segmento %s: %s", i, e)
                    failed += 1

            self.last_extract_dir = output_dir
            cfgmod.set_last_dir('last_extract_dir', output_dir)
            self.set_progress(100)
            self.set_buttons_busy(False)

            if self.cancel_batch.is_set():
                self.set_status(f"Cancelado. {ok} extraídos, {failed} fallidos")
                self.root.after(0, lambda: messagebox.showinfo(
                    "Cancelado", f"Extracción cancelada.\nOK: {ok}  Fallidos: {failed}"))
            else:
                self.set_status(f"✅ Extracción completa: {ok} OK, {failed} fallidos")
                self.root.after(0, lambda: messagebox.showinfo(
                    "Éxito", f"Se extrajeron {ok} de {total} clips a:\n{output_dir}"
                    + (f"\n({failed} fallaron, ver consola)" if failed else "")))

        threading.Thread(target=do_extract_all, daemon=True).start()

    # ----------------------------------------------------------------
    # Vista previa embebida
    # ----------------------------------------------------------------
    def start_preview(self):
        if not PREVIEW_AVAILABLE:
            messagebox.showerror(
                "Vista previa no disponible",
                "Para usar la vista previa instalá las dependencias:\n\n"
                "pip install opencv-python-headless pillow"
            )
            return
        if self.selected_segment is None:
            messagebox.showwarning("Atención", "Seleccioná un clip primero")
            return
        if not self.show_preview.get():
            self.show_preview.set(True)
            self.toggle_preview_tab()

        seg = self.selected_segment
        self.stop_preview()
        self.right_notebook.select(self.preview_tab)
        self._draw_preview_placeholder("Generando vista previa...")

        def do_prepare():
            self.set_status("Generando vista previa...")
            self.root.after(0, lambda: self.preview_btn.config(state='disabled'))
            try:
                path = self.parser.prepare_preview(seg)
                self.root.after(0, lambda: self._open_preview(path))
                self.set_status("Listo")
            except Exception as e:
                log.exception("Error generando vista previa")
                msg = str(e)
                self.root.after(0, lambda: self._draw_preview_placeholder("Error generando vista previa"))
                self.root.after(0, lambda: messagebox.showerror(
                    "Error", f"No se pudo generar la vista previa:\n{msg}"))
                self.set_status("❌ Error")
            finally:
                self.root.after(0, lambda: self.preview_btn.config(state='normal'))

        threading.Thread(target=do_prepare, daemon=True).start()

    def _open_preview(self, path: str):
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            self._draw_preview_placeholder("No se pudo abrir el video para vista previa")
            messagebox.showerror("Error", f"No se pudo abrir el video generado:\n{path}")
            return

        self.preview_cap = cap
        fps = cap.get(cv2.CAP_PROP_FPS)
        self.preview_fps = fps if fps and fps > 1 else 25.0
        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        self.preview_duration = (frame_count / self.preview_fps) if frame_count > 0 else 0.0

        self.preview_scale.config(to=max(self.preview_duration, 0.1))
        self.preview_scale_var.set(0)
        self.preview_playpause_btn.config(state='normal', text="⏸")
        self.preview_stop_btn.config(state='normal')

        self.preview_playing = True
        self._play_preview_loop()

    def _play_preview_loop(self):
        if not self.preview_playing or self.preview_cap is None:
            return

        ret, frame = self.preview_cap.read()
        if not ret:
            self.preview_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            self.preview_playing = False
            self.preview_playpause_btn.config(text="▶")
            return

        self._show_preview_frame(frame)

        if not self.preview_seeking:
            current_sec = self.preview_cap.get(cv2.CAP_PROP_POS_FRAMES) / self.preview_fps
            self.preview_scale_var.set(current_sec)
            self.preview_time_var.set(
                f"{self._fmt_time(current_sec)} / {self._fmt_time(self.preview_duration)}"
            )

        delay = max(int(1000 / self.preview_fps), 1)
        self.preview_after_id = self.root.after(delay, self._play_preview_loop)

    def _show_preview_frame(self, frame):
        canvas_w = self.preview_canvas.winfo_width() or 480
        canvas_h = self.preview_canvas.winfo_height() or 270

        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(frame_rgb)

        img_ratio = img.width / img.height
        canvas_ratio = canvas_w / canvas_h if canvas_h else img_ratio
        if img_ratio > canvas_ratio:
            new_w = canvas_w
            new_h = max(int(canvas_w / img_ratio), 1)
        else:
            new_h = canvas_h
            new_w = max(int(canvas_h * img_ratio), 1)
        img = img.resize((new_w, new_h), Image.LANCZOS)

        self.preview_photo = ImageTk.PhotoImage(img)
        self.preview_canvas.delete('all')
        self.preview_canvas.create_image(canvas_w // 2, canvas_h // 2, image=self.preview_photo)

    def _draw_preview_placeholder(self, message: str):
        self.preview_canvas.delete('all')
        w = self.preview_canvas.winfo_width() or 480
        h = self.preview_canvas.winfo_height() or 270
        self.preview_canvas.create_text(
            w // 2, h // 2, text=message, fill='#888', font=('Segoe UI', 11), justify='center'
        )

    def _on_preview_canvas_resize(self, event=None):
        if self.preview_cap is None:
            self._draw_preview_placeholder("Seleccioná un clip y tocá '👁 Vista Previa'")

    def toggle_preview_play(self):
        if self.preview_cap is None:
            return
        self.preview_playing = not self.preview_playing
        self.preview_playpause_btn.config(text="⏸" if self.preview_playing else "▶")
        if self.preview_playing:
            self._play_preview_loop()

    def stop_preview(self):
        if self.preview_after_id is not None:
            try:
                self.root.after_cancel(self.preview_after_id)
            except Exception:
                pass
            self.preview_after_id = None

        self.preview_playing = False
        if self.preview_cap is not None:
            self.preview_cap.release()
            self.preview_cap = None

        self.preview_photo = None
        self.preview_scale_var.set(0)
        self.preview_time_var.set("00:00 / 00:00")
        if hasattr(self, 'preview_playpause_btn'):
            self.preview_playpause_btn.config(text="▶", state='disabled')
            self.preview_stop_btn.config(state='disabled')
        if hasattr(self, 'preview_canvas'):
            self._draw_preview_placeholder("Seleccioná un clip y tocá '👁 Vista Previa'")

    def _on_preview_seek_start(self, event=None):
        self.preview_seeking = True

    def _on_preview_seek_end(self, event=None):
        self.preview_seeking = False
        if self.preview_cap is None:
            return
        target_sec = self.preview_scale_var.get()
        frame_no = int(target_sec * self.preview_fps)
        self.preview_cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
        self.preview_time_var.set(f"{self._fmt_time(target_sec)} / {self._fmt_time(self.preview_duration)}")
        if not self.preview_playing:
            ret, frame = self.preview_cap.read()
            if ret:
                self._show_preview_frame(frame)
                self.preview_cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)

    @staticmethod
    def _fmt_time(seconds: float) -> str:
        seconds = max(0, int(seconds))
        return f"{seconds // 60:02d}:{seconds % 60:02d}"
    