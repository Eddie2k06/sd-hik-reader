# 📇 SD Reader — Lector de Tarjetas SD EZVIZ / Hikvision

> App de escritorio (Tkinter, Windows) para leer el índice `index00.bin` de
> tarjetas SD grabadas por cámaras EZVIZ/Hikvision, previsualizar y extraer
> clips a MP4/JPG, generar hashes de los archivos extraídos, y formatear la
> tarjeta a bajo nivel.

![Python](https://img.shields.io/badge/Python-3.12-blue)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)
![License](https://img.shields.io/badge/License-MIT-green)
![Status](https://img.shields.io/badge/Status-Activo-brightgreen)

---

## Índice

- [¿Qué hace?](#qué-hace)
- [Capturas](#capturas)
- [Requisitos](#requisitos)
- [Instalación](#instalación)
- [Uso](#uso)
- [Estructura del proyecto](#estructura-del-proyecto)
- [Menús de la app](#menús-de-la-app)
- [Cómo se parsean y extraen los clips](#cómo-se-parsean-y-extraen-los-clips-coreparserpy)
- [Formateo de bajo nivel — ⚠️ leer antes de usar](#formateo-de-bajo-nivel--️-leer-antes-de-usar)
- [Detección de ffmpeg](#detección-de-ffmpeg-y-variables-de-entorno-coreffmpeg_setuppy)
- [Compilar un .exe standalone](#compilar-un-exe-standalone)
- [Licencia](#licencia)
- [Disclaimer](#disclaimer)

---

## ¿Qué hace?

Las cámaras EZVIZ/Hikvision graban sus SD con un formato propietario propio
(no es un sistema de archivos estándar): un índice binario (`index00.bin`)
que apunta a segmentos dentro de archivos contenedor `hivXXXXX.mp4`. Esta
app permite, sin depender del software oficial:

- 🔍 **Leer el índice** de video (`index00.bin`) y de imágenes (`index00p.bin`).
- 🎬 **Previsualizar** los clips embebidos en la propia app.
- 📤 **Extraer** clips a `.mp4` (con o sin audio), miniaturas a `.jpg`, o por
  rango de fechas/horas.
- 🗺️ **Visualizar el mapa de segmentos** de cada `avFile` (256 slots:
  libre / video / evento-alarma / corrupto).
- 🔐 **Generar hashes** (MD5/SHA-256) de lo extraído, con informe `.txt`/`.pdf`.
- 💣 **Formatear a bajo nivel** el origen (sobrescritura completa por
  clusters), sólo sobre unidades removibles detectadas por el sistema.
- 🎞️ **Detectar/instalar ffmpeg** automáticamente si no está en el `PATH`.

## Capturas

> _Agregar acá 2-3 capturas de la ventana principal, el mapa de segmentos y
> el diálogo de formateo antes de publicar (ej. `docs/screenshot-main.png`)._

## Requisitos

- Windows 10/11 (es donde se probó y se apoya el flujo de formateo de bajo
  nivel; en Linux/Mac corre pero sin el ícono nativo `.ico` ni el
  instalador automático de ffmpeg).
- Python 3.10+ (se desarrolló/probó con 3.12).
- `tkinter` (viene incluido con la instalación estándar de Python en
  Windows).
- `ffmpeg` / `ffprobe` en el `PATH` — si no están, la app los detecta e
  ofrece instalarlos sola (ver [menú Codex](#detección-de-ffmpeg-y-variables-de-entorno-coreffmpeg_setuppy)).

## Instalación

```bash
git clone https://github.com/<tu-usuario>/<tu-repo>.git
cd <tu-repo>
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

Las dependencias de `requirements.txt` son todas opcionales:

| Paquete                  | Para qué sirve                                  |
|---------------------------|-------------------------------------------------|
| `opencv-python-headless`  | Vista previa embebida de video (Ver > Vista Previa) |
| `pillow`                  | Vista previa + ícono de la app en Linux/Mac      |
| `reportlab`                | Informe de hash en PDF (si falta, se genera en TXT) |

Sin ellas la app arranca igual con las funciones core (parseo, extracción,
formateo).

## Uso

```bash
python main.py
```

1. Menú **SD** → elegí la unidad detectada o una carpeta manual.
2. Se carga el índice y aparecen los paneles de Info / Filtro / Línea de
   Tiempo / Vista Previa / Detalles.
3. Seleccioná un segmento y extraelo desde los botones o el menú
   **Extraer**.

Los logs quedan en `%LOCALAPPDATA%\sd_hik_reader\logs\app.log` (rotativo,
útil para diagnosticar fallas de parseo o instalación de ffmpeg).

## Estructura del proyecto

```
.
├── main.py                # Punto de entrada (python main.py)
├── requirements.txt
├── sd-card.ico             # Ícono de la app (16 a 256px)
├── SD_READER.spec           # Spec de PyInstaller para compilar a .exe
├── core/                   # Lógica sin GUI (reutilizable / testeable)
│   ├── parser.py           # Parser del índice EZVIZ + extracción vía ffmpeg
│   ├── drives.py            # Detección de unidades + tamaño real del dispositivo
│   ├── formatter.py         # Formateo de bajo nivel
│   ├── hashing.py            # Cálculo de hash (MD5/SHA-256) + informe
│   ├── sd_check.py           # ¿Esta carpeta es una SD EZVIZ válida?
│   ├── config.py              # Persistencia de preferencias (JSON en AppData)
│   ├── ffmpeg_setup.py         # Detección/instalación/actualización de ffmpeg
│   └── codex_setup.py           # Instalación de OpenAI Codex CLI (no conectado a la GUI aún)
└── gui/
    ├── app.py              # Ventana principal, paneles, extracción/preview
    ├── menu.py              # Barra de menú
    └── dialogs.py            # Diálogos (Formatear, Hash, Info de SD, Mapa de segmentos, etc.)
```

## Menús de la app

- **SD** — lista unidades detectadas (se recalcula al abrir el menú),
  permite elegir carpeta manualmente y dispara la carga.
- **Ver** — muestra/oculta cada panel, pasa la línea de tiempo a modo
  vertical, y abre **Información de la tarjeta SD** y el **Mapa de
  segmentos** (grilla de los 256 slots por `avFile`, en bloques de 32).
- **Extraer** — MP4 con/sin audio, miniatura JPG, extracción por rango,
  cancelar, y **Crear hash de archivos extraídos**.
- **Formatear** — formateo de bajo nivel, sólo sobre unidades removibles
  detectadas (nunca discos fijos), con confirmación explícita ("FORMATEAR")
  y ventana de progreso (%, cluster actual/total, velocidad, ETA).
- **Codex** — chequeo/instalación de **ffmpeg** (el nombre es por el build
  "CODEX FFMPEG" de gyan.dev, sin relación con OpenAI). Muestra estado,
  versión instalada, ubicación, y si el PATH quedó persistido en el
  registro de Windows. Botones **Verificar** e **Instalar / Actualizar**.
- **Acerca de** — información de la app y sus dependencias.

## Cómo se parsean y extraen los clips (`core/parser.py`)

Cada raíz de SD EZVIZ/Hikvision tiene esta forma:

```
<raíz de la SD>/
├── info.bin              # opcional: serial, MAC, cuántos datadirN hay
├── datadir0/
│   ├── index00.bin        # índice de VIDEO
│   ├── index00p.bin        # índice de IMÁGENES
│   ├── hiv00000.mp4         # clips de video, uno por "avFile"
│   ├── hiv00001.mp4
│   └── ...
└── datadir1/ ...           # puede haber más de un datadir (según info.bin)
```

- **`info.bin`** (68 bytes, struct `48s4sB3I`): si no existe, se asume 1
  solo `datadir` y se sigue igual — no es obligatorio.
- **`index00.bin` / `index00p.bin`**: header fijo de 1280 bytes (`avFiles`
  = cantidad de archivos `hivXXXXX`), tabla de nombres, y tabla de
  segmentos: **256 slots fijos por `avFile`**, cada uno un registro de 80
  bytes con tipo, estado, resolución, timestamps de inicio/fin, offsets
  dentro del `hivXXXXX`, etc. Un slot con `endTime == 0` es una posición
  libre y se descarta. Los timestamps son Unix de 32 bits.
- **Nombres de archivo**: cada slot `n` corresponde a `hiv{n:05d}.mp4` (o
  `.pic` para imágenes) dentro del mismo `datadir` — la app arma ese path
  a partir del número de slot, no lee nombres reales del disco.
- **Extracción (vía ffmpeg)**: dado un `Segment`, se recorta el rango de
  bytes correspondiente y se remuxea a `.mp4`. Dos particularidades
  reverse-engineadas:
  - **FIX 1** — en cámaras HEVC (ej. EB3 4G) los `hivXXXXX.mp4` no son un
    stream elemental puro: son contenedores **MPEG-PS** (video + audio AAC
    mezclados). Se deja que ffmpeg autodetecte el contenedor real en vez
    de forzar el demuxer crudo.
  - **FIX 2** — un mismo segmento puede contener **varios contenedores
    MPEG-PS concatenados**. Se valida la duración del resultado contra la
    esperada y, si quedó corto, se parte el volcado crudo por cada
    `system_header`, se remuxea cada trozo a `.ts` y se concatenan.
- **Mapa de segmentos** (`get_segment_map`): recorre el índice slot por
  slot sin descartar nada, y clasifica cada uno como `free` / `video` /
  `alarm` (heurística sobre el byte `status`) / `corrupt`.

## Formateo de bajo nivel — ⚠️ leer antes de usar

> **Esta operación es irreversible.** Sobrescribe por completo los
> clusters de la unidad seleccionada. No hay confirmación posterior ni
> forma de deshacerlo.

- Sólo lista **unidades removibles** (SD/USB) que el sistema operativo
  detecta como tales — un disco fijo nunca aparece como opción.
- En Windows, la raíz montada (ej. `D:\`) se convierte internamente al
  path de dispositivo crudo (`\\.\D:`) y se abre vía `CreateFile`,
  bloqueando el volumen (`FSCTL_LOCK_VOLUME`) para escribir directo. Esto
  **casi siempre requiere correr la app como Administrador**.
- En Linux/otros sistemas se abre el dispositivo crudo (`/dev/sdX`),
  requiere permisos root/sudo.

## Detección de ffmpeg y variables de entorno (`core/ffmpeg_setup.py`)

`find_ffmpeg()` prueba, en orden:

1. El `PATH` heredado por el proceso actual (`shutil.which`).
2. En Windows, el `PATH` leído directamente del registro (usuario +
   sistema) — cubre el caso de haber agregado ffmpeg *después* de abrir
   la terminal desde la que corre la app.
3. La carpeta local donde la app dejó su propia instalación previa
   (`%LOCALAPPDATA%\sd_hik_reader\ffmpeg`).

Al instalar/actualizar, esa carpeta se agrega **al principio** del PATH de
usuario, para tener precedencia sobre cualquier otra instalación existente.
Si el ffmpeg detectado está en el PATH de **sistema**, la app no puede
cambiar esa precedencia sin permisos de administrador (limitación real de
Windows).

## Compilar un .exe standalone

El repo incluye `SD_READER.spec` para [PyInstaller](https://pyinstaller.org/):

```bash
pip install pyinstaller
pyinstaller SD_READER.spec
```

> Editá las rutas absolutas (`C:/Pythons/sd_3/...`) dentro del `.spec`
> para que apunten a tu clon local antes de compilar.

El ejecutable queda en `dist/SD_READER.exe`, con el ícono `sd-card.ico`
embebido y sin consola (`console=False`).

## Licencia

Este proyecto se distribuye bajo licencia [MIT](LICENSE). Agregá un
archivo `LICENSE` con el texto correspondiente antes de publicar (o
reemplazá esta sección si preferís otra licencia).

## Disclaimer

Este proyecto es un cliente **no oficial**, resultado de ingeniería
inversa del formato de índice EZVIZ/Hikvision, sin afiliación con EZVIZ,
Hikvision ni sus marcas. Usalo bajo tu propia responsabilidad, en
particular la función de formateo de bajo nivel, que es destructiva e
irreversible.
