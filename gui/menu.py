#!/usr/bin/env python3
"""
gui/menu.py

Arma la barra de menú principal:
  SD | Ver | Extraer | Formatear | Hashear | Codex | Acerca de
"""

import tkinter as tk
from tkinter import messagebox

from core.drives import list_available_drives


def build_menu(app):
    root = app.root
    menubar = tk.Menu(root)

    _build_sd_menu(app, menubar)
    _build_ver_menu(app, menubar)
    _build_extraer_menu(app, menubar)
    _build_formatear_menu(app, menubar)
    _build_hashear_menu(app, menubar)
    _build_codex_menu(app, menubar)
    _build_ayuda_menu(app, menubar)

    root.config(menu=menubar)
    app.menubar = menubar


# ----------------------------------------------------------------------
# Menú SD
# ----------------------------------------------------------------------
def _build_sd_menu(app, menubar):
    sd_menu = tk.Menu(menubar, tearoff=0)

    def refresh_drive_list():
        # Elimina y vuelve a armar sólo la sección de unidades detectadas
        # (los ítems fijos de abajo se re-agregan después).
        sd_menu.delete(0, 'end')
        try:
            drives = list_available_drives()
        except Exception:
            drives = []

        if drives:
            sd_menu.add_command(label="Unidades detectadas:", state='disabled')
            for d in drives:
                sd_menu.add_command(
                    label="   " + d.display_text(),
                    command=lambda p=d.path: app.select_drive_path(p),
                )
            sd_menu.add_separator()
        else:
            sd_menu.add_command(label="(No se detectaron unidades removibles)", state='disabled')
            sd_menu.add_separator()

        sd_menu.add_command(label="Seleccionar carpeta...", command=app.browse_folder)
        sd_menu.add_command(label="🔍 Cargar", command=app.load_data)
        sd_menu.add_separator()
        sd_menu.add_command(label="Salir", command=root_quit)

    def root_quit():
        app.on_close()

    sd_menu.configure(postcommand=refresh_drive_list)
    menubar.add_cascade(label="SD", menu=sd_menu)


# ----------------------------------------------------------------------
# Menú Ver
# ----------------------------------------------------------------------
def _build_ver_menu(app, menubar):
    ver_menu = tk.Menu(menubar, tearoff=0)

    ver_menu.add_command(
        label="👁 Información de la tarjeta SD...", command=app.open_drive_info)
    ver_menu.add_command(
        label="🗺 Mapa de segmentos...", command=app.open_segment_map)
    ver_menu.add_separator()

    ver_menu.add_checkbutton(
        label="Info", variable=app.show_info, command=app.toggle_info_panel)
    ver_menu.add_checkbutton(
        label="Filtro", variable=app.show_filter, command=app.toggle_filter_panel)
    ver_menu.add_checkbutton(
        label="Línea de Tiempo", variable=app.show_timeline, command=app.toggle_timeline_tab)
    ver_menu.add_checkbutton(
        label="Vista Previa", variable=app.show_preview, command=app.toggle_preview_tab)
    ver_menu.add_checkbutton(
        label="Detalles", variable=app.show_detail, command=app.toggle_detail_panel)

    menubar.add_cascade(label="Ver", menu=ver_menu)


# ----------------------------------------------------------------------
# Menú Extraer
# ----------------------------------------------------------------------
def _build_extraer_menu(app, menubar):
    extraer_menu = tk.Menu(menubar, tearoff=0)

    extraer_menu.add_command(label="▶ Extraer MP4 (sin audio)", command=app.extract_selected)
    extraer_menu.add_command(label="🔊 Extraer MP4 (con audio)", command=app.extract_selected_with_audio)
    extraer_menu.add_command(label="🖼 Extraer miniatura (JPG)", command=app.extract_thumbnail)
    extraer_menu.add_command(label="📦 Extraer todos (rango filtrado)", command=app.extract_range)
    extraer_menu.add_command(label="🛑 Cancelar extracción en curso", command=app.cancel_extraction)
    extraer_menu.add_separator()
    extraer_menu.add_command(label="🔑 Hashear archivos extraídos...", command=lambda: app.open_hash_dialog())

    menubar.add_cascade(label="Extraer", menu=extraer_menu)


# ----------------------------------------------------------------------
# Menú Formatear
# ----------------------------------------------------------------------
def _build_formatear_menu(app, menubar):
    formatear_menu = tk.Menu(menubar, tearoff=0)
    formatear_menu.add_command(
        label="Formatear tarjeta SD (bajo nivel)...",
        command=lambda: _open_format_dialog(app),
    )
    menubar.add_cascade(label="Formatear", menu=formatear_menu)


def _open_format_dialog(app):
    app.open_format_dialog()


# ----------------------------------------------------------------------
# Menú Hashear
# ----------------------------------------------------------------------
def _build_hashear_menu(app, menubar):
    hashear_menu = tk.Menu(menubar, tearoff=0)
    hashear_menu.add_command(
        label="Hashear carpeta o archivo...",
        command=lambda: app.open_hash_dialog(),
    )
    menubar.add_cascade(label="Hashear", menu=hashear_menu)


# ----------------------------------------------------------------------
# Menú Codex
# ----------------------------------------------------------------------
def _build_codex_menu(app, menubar):
    codex_menu = tk.Menu(menubar, tearoff=0)
    codex_menu.add_command(
        label="Verificar / instalar ffmpeg (versión, última, PATH)...",
        command=lambda: _open_ffmpeg_status_dialog(app),
    )
    menubar.add_cascade(label="Codex", menu=codex_menu)


def _open_ffmpeg_status_dialog(app):
    from gui.dialogs import CodexInstallDialog
    CodexInstallDialog(app.root)


# ----------------------------------------------------------------------
# Menú Acerca de
# ----------------------------------------------------------------------
def _build_ayuda_menu(app, menubar):
    ayuda_menu = tk.Menu(menubar, tearoff=0)
    ayuda_menu.add_command(label="Acerca de...", command=lambda: _show_about(app))
    menubar.add_cascade(label="Acerca de", menu=ayuda_menu)


def _show_about(app):
    from gui.dialogs import show_about
    show_about(app.root)
