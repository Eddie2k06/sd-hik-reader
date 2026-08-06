#!/usr/bin/env python3
"""
main.py

Punto de entrada. Ejecutar con: python main.py
"""

import logging
import logging.handlers
import os
import sys
import tkinter as tk

from gui.app import SDReaderApp

LOG_DIR = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'sd_hik_reader', 'logs')
LOG_PATH = os.path.join(LOG_DIR, 'app.log')


def _setup_logging():
    """Consola (si hay una visible) + archivo rotativo persistente,
    para poder diagnosticar fallas (ej. instalación de ffmpeg, errores
    de parseo) aunque la app se haya lanzado sin consola."""
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=3, encoding='utf-8')
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)


_setup_logging()
log = logging.getLogger("ezviz_reader.main")
log.info("Iniciando app. Log en: %s", LOG_PATH)


def _log_uncaught(exc_type, exc_value, exc_tb):
    """Cualquier excepción no atrapada en algún lado (fuera de los
    try/except normales de la app) queda igual registrada en el log,
    en vez de perderse en una consola que puede ni estar visible."""
    log.critical("Excepción no capturada", exc_info=(exc_type, exc_value, exc_tb))


sys.excepthook = _log_uncaught


def _set_windows_app_id():
    """Windows agrupa/identifica la ventana en la barra de tareas por
    su 'AppUserModelID', no por el .ico solo. Si el proceso corre como
    python.exe/pythonw.exe (no como un .exe propio compilado), Windows
    lo agrupa bajo el ID de Python y termina mostrando el ícono de
    Python en la barra de tareas aunque root.iconbitmap() esté seteado
    (ese sólo aplica al ícono de la ventana/Alt+Tab). Fijar un
    AppUserModelID propio ANTES de crear cualquier ventana hace que
    Windows trate esto como su propia app y tome el ícono seteado más
    abajo también para el botón de la barra de tareas.
    No-op fuera de Windows."""
    if not sys.platform.startswith('win'):
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('EddieSoft.SDReader.3')
    except Exception:
        log.warning("No se pudo fijar el AppUserModelID de Windows", exc_info=True)


def _set_app_icon(root):
    """Ícono de la ventana y de la barra de tareas. El archivo ya
    estaba en el proyecto (sd-card.ico) pero nada lo aplicaba todavía.
    .ico funciona nativo en Windows vía iconbitmap; en Linux/Mac Tk no
    soporta .ico ahí, así que se intenta con iconphoto (usa Pillow, ya
    presente para la vista previa) y si no está disponible se sigue
    sin ícono en vez de romper el arranque."""
    from core.config import APP_ICON_PATH
    icon_path = APP_ICON_PATH
    if not os.path.isfile(icon_path):
        log.warning("No se encontró el ícono de la app en: %s", icon_path)
        return
    try:
        # default=... registra el ícono para root Y para todos los
        # Toplevel hijos que no fijen uno propio (diálogos de
        # Formatear/Hash/Acerca de/instaladores). Sin 'default=', Tk
        # sólo lo aplica a esta ventana puntual y los diálogos quedan
        # con el ícono genérico de Tk (la "pluma").
        root.iconbitmap(default=icon_path)
        return
    except tk.TclError:
        pass
    try:
        from PIL import Image, ImageTk
        icon_img = ImageTk.PhotoImage(Image.open(icon_path))
        root.iconphoto(True, icon_img)
        root._icon_img_ref = icon_img  # referencia viva: evita garbage collection
    except Exception:
        log.warning("No se pudo aplicar el ícono de la app", exc_info=True)


def main():
    _set_windows_app_id()
    root = tk.Tk()
    _set_app_icon(root)

    # Mismo tema y misma paleta de grises para toda la app (ventana
    # principal + diálogos) — ver gui/dialogs._ensure_styles. Se llama
    # acá, antes de armar la ventana principal, para que ya tome el
    # tema 'clam' desde el arranque en vez de heredar el tema nativo
    # de Windows (que se ve distinto).
    from gui.dialogs import _ensure_styles
    _ensure_styles()

    def _log_tk_callback_exception(exc_type, exc_value, exc_tb):
        # Los errores dentro de callbacks de Tkinter (botones, .after())
        # no pasan por sys.excepthook — Tk los intercepta acá.
        log.error("Excepción en callback de Tkinter", exc_info=(exc_type, exc_value, exc_tb))

    root.report_callback_exception = _log_tk_callback_exception

    SDReaderApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
