# EZVIZ / Hikvision — Formato de tarjeta SD

> Guía técnica de referencia · Ingeniería inversa · Marzo 2026  
> **Scope:** Mapeo empírico obtenido por análisis directo de archivos binarios reales.  
> Hikvision no publica estas especificaciones. Los resultados pueden diferir  
> en otros modelos o versiones de firmware.

---

## 0. Hardware analizado

| Campo                | Valor                              |
|----------------------|------------------------------------|
| Marca / App          | EZVIZ                              |
| Modelo               | **CS-EB3-R200-1K3FL4GA-LA**        |
| Número de serie      | BE5693419                          |
| Firmware             | **V5.4.0 build 250520**            |
| Zona horaria         | **UTC-03:00** (Argentina)          |
| Horario de verano    | Activado                           |
| Formato de fecha OSD | DD-MM-YYYY                         |
| Alimentación         | Batería + panel solar opcional     |
| Resoluciones         | 1280×720 (HD) y 2304×1296 (2K)     |
| Codecs               | H.265 (hvc1) y H.264 (avc1)        |
| Audio                | AAC mp4a a 16 kHz                  |
| SD analizada         | 32 GB — filesystem propietario HFS  |
| Archivos analizados  | index00p.bin (372 slots), hiv00000.mp4 (256 MB), logCurFile.bin (325 eventos), 10× MP4 descargados |

> **Nota FS:** Windows pide formatear la SD al insertarla directamente.
> El filesystem es propietario Hikvision (HFS) — no legible por Windows/macOS nativamente.

---

## 1. Estructura de archivos en la SD

```
SD:/                           ← estructura PLANA, sin carpetas
├── index00.bin       16 MB    Índice BACKUP  canal 0 — 130 entradas
├── index00p.bin      32 MB    Índice ACTIVO  canal 0 — 372 entradas ← usar
├── index01.bin       16 MB    Índice BACKUP  canal 1 — idéntico a index00
├── index01p.bin      32 MB    Índice ACTIVO  canal 1 — idéntico a index00p
├── logCurFile.bin    ~16 MB   Log de eventos RATS — 325 eventos
├── logMainFile.bin   ~32 MB   Log histórico (vacío/rotado en este caso)
├── hiv00000.mp4      256 MB   Bloque de video 0
├── hiv00001.mp4      256 MB   Bloque de video 1
└── ...                        hasta hivNNNNN.mp4
```

**Reglas:**
- `index*p.bin` = índice activo, siempre más entradas — usar este
- `index*.bin`  = backup automático del firmware
- `index00` e `index01` representan el mismo canal (redundancia del firmware)
- Los HIV tienen extensión `.mp4` pero **NO son contenedores MP4**

---

## 2. Archivos de índice — formato OFNI

### 2.1 Header del archivo (antes del primer OFNI)

El archivo NO empieza con datos de slots. Hay un header de gestión del ring-buffer
cuyo tamaño varía según el tipo de índice:

| Archivo       | Offset del 1er OFNI | Tamaño header |
|---------------|---------------------|---------------|
| index00p.bin  | `0x0654` (1,620 B)  | 1,620 bytes   |
| index00.bin   | `0x3f34` (16,180 B) | 16,180 bytes  |

**Header de index00p.bin — campos identificados (primeros 64 bytes):**

```
Offset  Valor           Interpretación
──────  ──────────────  ────────────────────────────────────────────
+00     0x000002EB      total de entradas (747) o magic
+08     0x00000003      desconocido (constante 3)
+0C     0x00000009      desconocido (constante 9)
+10     0x00000001      desconocido (constante 1)
+1C     0x69A19380      timestamp inicio SD = 27-02-2026 12:52:16
+20     0x69AED760      timestamp fin SD   = 09-03-2026 14:21:20
+30     0x0000FFFF      desconocido
```

Buscar `OFNI` para localizar el inicio de los slots:
```python
start = data.find(b"OFNI")   # NO asumir offset fijo
```

### 2.2 Estructura de un slot (80 bytes)

Cada entrada ocupa exactamente **80 bytes** a partir del primer `OFNI`.
Todos los campos numéricos son **little-endian**.

```
Offset  Size  Tipo     Descripción
──────  ────  ───────  ──────────────────────────────────────────────────────
+00     4     char[4]  Firma: "OFNI"  (0x4F 0x46 0x4E 0x49)
+04     8     bytes    Ceros (reserved)
+12     4     uint32   Estado del slot: 0x7FFFFFFF = vacío / 0x00000000 = activo
+16     4     bytes    Ceros (reserved)
+20     4     uint32   Constante 0x10000000 = 268,435,456 = BLOCK size
+24     4     uint32   Constante 0x10000000 = 268,435,456 = BLOCK size
+28     4     uint32   FLAGS — tipo de clip (ver §2.3)
+32     4     bytes    Ceros (reserved)
+36     4     uint32   ts_s — timestamp inicio
+40     4     uint32   us_s — microsegundos del inicio (generalmente 0)
+44     4     uint32   ts_e — timestamp fin  ← SIEMPRE = ts_s en index*p.bin
+48     4     uint32   us_e — microsegundos del fin
+52     4     uint32   Duplicado de ts_s (write timestamp)
+56     4     uint32   Duplicado de us_s
+60     4     uint32   stream_bytes — bytes del stream de video
+64     4     uint32   block_bytes  — bytes totales del bloque
+68     4     uint32   gl_s — offset global absoluto INICIO en HIV (LE)
+72     4     uint32   gl_e — offset global absoluto FIN   en HIV (LE)
+76     4     bytes    Ceros (reserved)
```

**Parsing Python:**
```python
flags        = struct.unpack_from("<I", ch, 28)[0]
ts_s         = struct.unpack_from("<I", ch, 36)[0]
ts_e         = struct.unpack_from("<I", ch, 44)[0]
stream_bytes = struct.unpack_from("<I", ch, 60)[0]
block_bytes  = struct.unpack_from("<I", ch, 64)[0]
gl_s         = struct.unpack_from("<I", ch, 68)[0]
gl_e         = struct.unpack_from("<I", ch, 72)[0]
```

### 2.3 Flags conocidos

| Valor (hex)  | Tipo  | Archivo           |
|--------------|-------|-------------------|
| `0x0020000D` | Video | index\*p.bin      |
| `0x00A0000D` | Video | index\*.bin       |
| `0x00200020` | Foto  | index\*p.bin      |
| `0x00A00020` | Foto  | index\*.bin       |

El bit `0x00800000` diferencia archivo activo (`0x00`) vs backup (`0xA0`).
Ambos valores son equivalentes para identificar Video/Foto.

### 2.4 Timestamps — ⚠️ bug de firmware crítico (VERIFICADO)

**Causa confirmada:** El firmware CS-EB3 V5.4.0 graba la **hora local**
directamente en el campo Unix timestamp, sin aplicar conversión UTC.

**Prueba irrefutable:**
```
Slot 337  →  ts_s = 1772653089
             ts_s leído como UTC  = 04-03-2026 19:38:09
             OSD del video        = 04-03-2026 19:38:09 ART

             Son IDÉNTICOS → el firmware graba hora local como UTC.
             Si fuera UTC real, el ts_s debería representar 22:38:09 UTC
             para que el OSD muestre 19:38 en Argentina (UTC-3).
```

> ⚠️ **Nota sobre el KB externo:** Documentos genéricos sobre Hikvision NVR/DVR
> afirman que ts_s es "UTC puro vía NTP". Eso es incorrecto para el CS-EB3.
> La cámara a batería sincroniza el reloj desde el teléfono vía app/Bluetooth
> y aparentemente recibe la hora local sin el offset UTC aplicado.

**Regla de conversión:**
```python
# CORRECTO — tratar ts_s como si fuera UTC (aunque es hora local)
datetime.fromtimestamp(ts_s, tz=timezone.utc)

# INCORRECTO — desplaza 3 horas en Argentina
datetime.fromtimestamp(ts_s, tz=local_tz)
```

**ts_e en index\*p.bin siempre = ts_s** — el firmware activo nunca escribe la
duración. Confirmado en 372/372 slots (100%).  
**ts_e en index\*.bin sí tiene duración real** (8s a 17362s observados).

### 2.5 Cálculo de HIV file y offset

```python
BLOCK = 268_435_456   # 256 MB exactos

hiv_idx    = gl_s // BLOCK           # número de archivo hiv
off_in_hiv = gl_s %  BLOCK           # offset dentro de ese archivo
hiv_name   = f"hiv{hiv_idx:05d}.mp4"
cross_block = gl_e > (hiv_idx + 1) * BLOCK  # ¿cruza al siguiente HIV?
```

### 2.6 Validación de timestamps

```python
TS_MIN = 1_577_836_800   # 2020-01-01 00:00:00
TS_MAX = 1_893_456_000   # 2030-01-01 00:00:00

valid = TS_MIN < ts_s < TS_MAX
```

### 2.7 Iteración de slots

```python
off = start
while off + 80 <= len(data):
    ch = data[off:off+80]
    if ch[:4] != b"OFNI":
        break
    if ch[12:16] == b"\xff\xff\xff\x7f":
        off += 80
        continue   # slot vacío — saltar
    # ... parsear ...
    off += 80
```

### 2.8 Comparativa index00.bin vs index00p.bin

| Característica       | index00.bin (backup)     | index00p.bin (activo)    |
|----------------------|--------------------------|--------------------------|
| Tamaño               | 16 MB                    | 32 MB                    |
| Header offset        | 0x3f34 (16,180 bytes)    | 0x0654 (1,620 bytes)     |
| Slots totales        | 131                      | 373                      |
| Entradas válidas     | 130                      | 372                      |
| Flags video          | `0x00A0000D`             | `0x0020000D`             |
| ts_e vs ts_s         | ts_e > ts_s (dur real)   | ts_e == ts_s (sin dur)   |
| Período              | 27-02 → 28-02-2026       | 27-02 → 09-03-2026       |
| Usar para extracción | ✗ backup                 | ✓ **este**               |

### 2.9 Estadísticas observadas (index00p.bin)

| Métrica                        | Valor                        |
|--------------------------------|------------------------------|
| Total slots válidos            | 372                          |
| Slots con ts_e = ts_s          | 372 (100%)                   |
| Tamaño mínimo de chunk         | 11,788 bytes (11.5 KB)       |
| Tamaño máximo de chunk         | 99,456 bytes (97.1 KB)       |
| Tamaño medio de chunk          | 76,596 bytes (76.6 KB)       |
| Rango de fechas                | 27-02-2026 → 09-03-2026      |
| Slots contiguos en HIV         | 1 par (0.3%)                 |
| Slots aislados (sin contiguos) | 370 (99.7%)                  |

---

## 3. Archivos de log — formato RATS

### 3.1 Localización

- `logCurFile.bin` — header de 2,048 bytes, primer RATS en offset `0x800`
- `logMainFile.bin` — misma estructura, vacío/rotado en la SD analizada
- Los eventos NO están en posiciones fijas — buscar firma `RATS`

**Header de logCurFile.bin (offset 0x00):**
```
+00:  0x69AF202B = 1773084715  →  09-03-2026 19:31:55  (último timestamp de escritura)
```

### 3.2 Estructura de un registro RATS (72 bytes)

```
Offset  Size  Descripción
──────  ────  ──────────────────────────────────────────────
+00     4     Firma: "RATS"  (0x52 0x41 0x54 0x53)
+04     4     uint32 = 0x00000002  (versión o tipo, constante)
+08     4     uint32 = Unix timestamp del evento (mismo bug tz que OFNI)
+12     4     uint32 = código de evento (ver §3.3)
+16     56    datos adicionales (generalmente ceros)
```

### 3.3 Códigos de evento conocidos

| Código (hex)   | Descripción                  |
|----------------|------------------------------|
| `0x00410003`   | Detección de movimiento + Video grabado |
| `0x00410001`   | Detección de movimiento      |
| `0x00000002`   | Evento de sistema            |

---

## 4. Archivos HIV — formato MPEG-PS raw

### 4.1 Formato real — ⚠️ NO son MP4

La extensión `.mp4` es una convención del firmware, **no el formato real**.
Son streams **MPEG-PS (MPEG-2 Program Stream) puros** desde el byte 0.

```
Offset 0x00000000:  00 00 01 BA  ← MPEG-PS Pack Start Code
```

**Verificado con hex editor (hiv00000.mp4):**
No hay `ftyp`, no hay `moov`, no hay `mdat`. El stream MPEG-PS comienza en el byte 0.
ffmpeg los reconoce directamente como `-f mpegps`.

### 4.2 Buffer circular

Cada HIV es un buffer circular de exactamente **256 MB**.
El firmware escribe secuencialmente; al llegar al final vuelve al inicio
sobreescribiendo las grabaciones más antiguas.

**El SCR no se reinicia al wrappear:**
```
Ejemplo real en hiv00000.mp4:
  Offset       0 bytes  →  SCR = 12.238s  (grabación post-wrap, más reciente)
  Offset  27 MB bytes   →  SCR =  8.733s  (grabación pre-wrap, anterior)
```
El SCR no es monótono — **no usar `ffmpeg -ss` lineal sobre el HIV completo**.

### 4.3 Cabecera MPEG-PS del sistema (~88 KB)

Los primeros **90,112 bytes** del HIV son exclusivamente cabecera del sistema:

```
[Pack header]     00 00 01 BA  — SCR, mux rate, stuffing
[System Header]   00 00 01 BB  — parámetros y lista de streams
[PS Map]          00 00 01 BC  — mapeo stream_id → codec
[Primeros frames] IDR + VPS + SPS + PPS del stream HEVC
```

Esta región es esencial para la extracción — contiene los parámetros de codec.

### 4.4 Pack header MPEG-PS — anatomía

Estructura observada en hiv00000.mp4 offset 0x00000000:

```
Offset  Bytes  Valor hex          Descripción
──────  ─────  ─────────────────  ────────────────────────────────────────
+0      4      00 00 01 BA        Pack Start Code
+4      6      44 01 0E 75 84 01  SCR field (5 bytes + 2 bits extensión)
+10     3      01 EB 8B           Mux rate (22 bits + marcadores)
+13     1      FE                 Stuffing length byte (bits 2-0 = 6 bytes)
+14     6      FF FF FF FF FF FF  Stuffing bytes (relleno)
──────────────────────────────────────────────────────────────────────────
Total pack header: 20 bytes (14 fijos + 6 stuffing en V5.4.0)
```

Secuencia de identificadores MPEG-PS relevantes:
```
00 00 01 BA  →  Pack Header       (inicio de grupo de paquetes)
00 00 01 BB  →  System Header     (info de streams)
00 00 01 BC  →  Program Stream Map (mapeo codec)
00 00 01 E0  →  PES Video         (stream HEVC)
00 00 01 C0  →  PES Audio         (stream MP2)
00 00 01 B9  →  Program End Code  (fin del stream)
```

### 4.5 Estructura interna de un chunk del índice

Cada entrada `gl_s → gl_e` del índice contiene:

```
[HIK header]        ~2440 bytes — propietario Hikvision, no decodificado
[MPEG-PS Pack 1]    00 00 01 BA  +  SCR  +  mux_rate  +  stuffing
  [Audio PES]       00 00 01 C0  +  longitud  +  payload MP2
  [Video PES]       00 00 01 E0  +  longitud  +  payload HEVC Annex-B
[MPEG-PS Pack 2]    ...
...
[MPEG-PS Pack 11]   (11 packs por chunk = 11 frames de video)
```

**Características del sub-stream en SD** (CS-EB3 / V5.4.0):

| Parámetro           | Valor                                              |
|---------------------|----------------------------------------------------|
| Codec video         | HEVC (H.265) Annex-B                               |
| FPS                 | 15 fps                                             |
| Resolución          | Sub-stream — no confirmada (est. 640×360–1280×720) |
| Audio               | MP2 (MPEG Audio Layer 2)                           |
| Frames por chunk    | 11 frames                                          |
| Duración por chunk  | ~0.667 s                                           |
| Tamaño por chunk    | 11 KB – 97 KB  (media 76 KB)                       |
| Bitrate estimado    | ~827 kbps                                          |
| Tipo de frames      | Solo P-frames — sin IDR propio en cada chunk       |

> ⚠️ **El stream principal NO está en la SD.**
> El video 2304×1296 con AAC solo se obtiene por WiFi vía protocolo ISAPI HTTP.

### 4.6 SCR — System Clock Reference

```python
def read_scr(data, pos):
    """pos = offset de '00 00 01 BA' dentro de data"""
    b = data[pos+4:pos+9]
    scr_base = (((b[0] & 0x38) >> 3) << 30 |
                ((b[0] & 0x03))       << 28 |
                (b[1]                 << 20) |
                ((b[2] & 0xf8) >> 3)  << 15 |
                ((b[2] & 0x03))       << 13 |
                (b[3]                 <<  5) |
                ((b[4] & 0xf8) >> 3))
    return scr_base / 90000.0   # clock MPEG-PS = 90 kHz

def parse_pts(pts_bytes):
    """PTS = 33 bits en 5 bytes con marker bits"""
    pts  = (pts_bytes[0] & 0x0E) << 29
    pts |= (pts_bytes[1] & 0xFF) << 22
    pts |= (pts_bytes[2] & 0xFE) << 14
    pts |= (pts_bytes[3] & 0xFF) << 7
    pts |= (pts_bytes[4] & 0xFE) >> 1
    return pts / 90000.0
```

**Duración real de un chunk:**
```
duración = SCR_último_pack − SCR_primer_pack + (1 / fps)
```

**Ejemplo verificado (slot 337):**
```
gl_s = 27,089,408  en hiv00000.mp4
SCR pack  1 =  8.733s
SCR pack 11 =  9.400s
Duración    =  9.400 − 8.733 + 0.067 = 0.734s
```

### 4.7 Extracción correcta de un clip

**El problema:** los chunks contienen solo P-frames.
Los parámetros HEVC (VPS, SPS, PPS) y el IDR están en los primeros
frames del HIV. Sin ellos el decodificador falla o produce imagen corrupta.

**Solución — prepend cabecera del sistema + chunk:**

```
[Primer pack del HIV]   ← System Header + PS Map + IDR + VPS/SPS/PPS
        +
[Bytes gl_s → gl_e]     ← chunk del clip (P-frames)
        =
archivo .mpg temporal   ← MPEG-PS válido y decodificable
```

```python
# 1. Leer el primer pack del HIV (cabecera del sistema)
with open(hiv_path, "rb") as f:
    raw = f.read(4096)
second_pack = raw.find(b"\x00\x00\x01\xba", 4)
ps_header = raw[:second_pack]      # solo el primer pack

# 2. Leer chunk (con manejo de cross-block)
BLOCK = 268_435_456
hiv_n  = gl_s // BLOCK
off    = gl_s %  BLOCK
size   = gl_e - gl_s
cross  = gl_e > (hiv_n + 1) * BLOCK

if not cross:
    with open(f"hiv{hiv_n:05d}.mp4", "rb") as f:
        f.seek(off)
        chunk = f.read(size)
else:
    with open(f"hiv{hiv_n:05d}.mp4", "rb") as f:
        f.seek(off)
        p1 = f.read((hiv_n + 1) * BLOCK - gl_s)
    with open(f"hiv{hiv_n+1:05d}.mp4", "rb") as f:
        p2 = f.read(gl_e - (hiv_n + 1) * BLOCK)
    chunk = p1 + p2

# 3. Crear MPEG-PS temporal y convertir
import tempfile, os
tmp = tempfile.NamedTemporaryFile(suffix=".mpg", delete=False)
tmp.write(ps_header)
tmp.write(chunk)
tmp.flush(); tmp.close()

# ffmpeg -y -i tmp.name -c:v copy -an -movflags +faststart output.mp4
os.unlink(tmp.name)
```

---

## 5. Relación índice ↔ HIV

```
index00p.bin                      hiv00000.mp4 (256 MB MPEG-PS raw)
────────────────────              ─────────────────────────────────────────────
                                  offset      0  [Pack header del sistema]
                                              :  [System Header 00 00 01 BB]
                                              :  [PS Map        00 00 01 BC]
                                              :  [IDR + VPS + SPS + PPS]
                                              :  (~88 KB de cabecera)
slot   0  gl_s=   90,112  ───────► offset  90,112  [HIK 2440B][11 PS packs]
slot   1  gl_s=  176,128  ───────► offset 176,128  [HIK 2440B][11 PS packs]
slot   2  gl_s=  264,192  ───────► offset 264,192  [HIK 2440B][11 PS packs]
...
slot 337  gl_s=27,089,408 ───────► offset 27,089,408  SCR=8.733s
slot 338  gl_s=27,160,064 ───────► offset 27,160,064  SCR=9.467s
...
                                  offset ~256 MB  [wrap → vuelve a offset 0]
```

---

## 6. MP4 descargados por la app EZVIZ

Los archivos que descarga la app al teléfono son **MP4 estándar ISO**,
completamente diferentes del formato HIV.

### 6.1 Identificación

**Nombre de archivo:** `{timestamp_unix_ms}.mp4`  
El timestamp = momento de **descarga**, NO de grabación.

**Firma en `ftyp` box:**
```
major brand : 'mp42'
compatible  : ['mp42', 'isom', 'HKMI']   ← brand Hikvision
```

### 6.2 Propiedades (10 archivos analizados)

| Archivo              | Duración | FPS  | Codec | Resolución   |
|----------------------|----------|------|-------|--------------|
| 1773092437095.mp4    | 13.0s    | 15.2 | avc1  | 1280×720     |
| 1773092467658.mp4    | 12.0s    | 15.2 | hvc1  | 1280×720     |
| 1773092476911.mp4    | 15.1s    | 14.9 | hvc1  | 2304×1296    |
| 1773092480917.mp4    | 5.0s     | 14.9 | hvc1  | 2304×1296    |
| 1773092483055.mp4    | 9.9s     | 14.9 | hvc1  | 1280×720     |
| 1773092487852.mp4    | 5.9s     | 14.9 | avc1  | 2304×1296    |
| 1773092509661.mp4    | 27.0s    | 15.2 | avc1  | 2304×1296    |
| 1773092513060.mp4    | 13.0s    | 15.2 | avc1  | 1280×720     |
| 1773095607442.mp4    | 11.4s    | 14.9 | hvc1  | 2304×1296    |
| 1773095657607.mp4    | 63.0s    | 4.0  | hvc1  | 1280×720     |

**Características comunes:**
- **GOP = 1** en la mayoría — cada frame es I-frame (seek instantáneo)
- Frames en formato **HVCC** (length-prefixed), **NO Annex-B**
- `mvhd.creation_time = 0` — Hikvision no usa este campo
- **Sin timestamps de grabación** en los metadatos del MP4

### 6.3 Formato de frames H.265 (HVCC)

Los frames dentro del `mdat` usan formato HVCC — cada NALU precedido
de 4 bytes big-endian con su longitud:

```
[4 bytes BE: longitud] [NALU bytes...]
[4 bytes BE: longitud] [NALU bytes...]
...
```

El primer frame siempre contiene los parámetros del codec:
```
NALU type 32  →  VPS (Video Parameter Set)
NALU type 33  →  SPS (Sequence Parameter Set) ← resolución, profile
NALU type 34  →  PPS (Picture Parameter Set)
NALU type 19/20 →  IDR frame (I-frame completo)
```

NAL start codes para H.265 Annex-B (en el HIV/MPEG-PS):
```
00 00 01 26  →  IDR_W_RADL  (keyframe H.265)
```

NAL start codes para H.264 (en algunos clips):
```
00 00 01 65  →  IDR frame
00 00 01 67  →  SPS
00 00 01 68  →  PPS
```

### 6.4 ¿Dónde está el timestamp real de grabación?

| Fuente                       | Timestamp       | Confiable |
|------------------------------|-----------------|-----------|
| Nombre del archivo descargado | Momento descarga | Para saber cuándo se descargó |
| `mvhd.creation_time`         | Siempre 0        | ✗ No usable |
| OSD quemado en el video      | Hora local       | Parcialmente — se resetea si la cámara pierde energía |
| **Índice OFNI `ts_s`**       | Hora local grabada como UTC | ✓ Más confiable |

---

## 7. Sistema de timestamps — resumen

| Fuente           | Formato          | Zona         | Confiable |
|------------------|------------------|--------------|-----------|
| OFNI `ts_s`      | uint32 LE        | Hora local grabada como UTC ⚠️ | Sí (más completo) |
| RATS timestamp   | uint32 LE        | Mismo bug que OFNI | Sí |
| OSD en video     | Texto en frames  | Hora local   | Parcialmente |
| Nombre MP4 descargado | uint64 ms LE | UTC real    | Solo para fecha de descarga |
| `mvhd.creation_time` | uint32       | N/A          | No (siempre 0) |

**Conversión correcta:**
```python
from datetime import datetime, timezone

# Para mostrar la hora TAL COMO la ve el usuario en el OSD:
dt = datetime.fromtimestamp(ts_s, tz=timezone.utc)
# → muestra la hora local de Argentina directamente

# Para calcular la hora UTC real (si se necesita):
# agregar el offset de la zona configurada en la cámara (UTC-3)
dt_utc_real = datetime.fromtimestamp(ts_s + 3*3600, tz=timezone.utc)
```

---

## 8. HIK header (2440 bytes — no decodificado)

Cada chunk comienza con **~2440 bytes** de cabecera propietaria Hikvision
antes del primer `00 00 01 BA`.

- Alta entropía — aparenta estar encriptado o scrambled
- Sin firmas conocidas internas
- Tamaño constante de 2440 bytes en todos los chunks analizados
  *(específico de V5.4.0 — puede variar en otros firmwares)*
- Para extracción: **ignorar** — buscar `00 00 01 BA` para el MPEG-PS

**Hipótesis no verificadas:**
- Podría contener metadatos del evento (tipo de trigger, resolución)
- Podría ser variante del formato HISI de otros modelos Hikvision

---

## 9. Comportamiento de grabación — modo batería

```
Evento de movimiento detectado
          │
          ▼
  [Graba ráfaga ~0.667s]     → 1 slot en index00p.bin
  [11 frames HEVC @ 15fps]   → ~69 KB en hivXXXXX.mp4
          │
          ▼
  [Vuelve a bajo consumo]
```

**Distribución de gaps entre eventos (372 slots):**

| Rango          | Cantidad | %      |
|----------------|----------|--------|
| 2–5 segundos   | 2        | 0.5%   |
| 5–10 segundos  | 6        | 1.6%   |
| 10–30 segundos | 28       | 7.5%   |
| 30–60 segundos | 7        | 1.9%   |
| 1–2 minutos    | 138      | 37.1%  |
| > 2 minutos    | 190      | 51.1%  |

**Comparativa main stream vs sub-stream:**

| Parámetro    | Main stream (WiFi/ISAPI) | Sub-stream (SD)          |
|--------------|--------------------------|--------------------------|
| Resolución   | 2304×1296 (2K)           | Est. 640×360–1280×720    |
| FPS          | ~15 fps                  | 15 fps                   |
| Codec        | HEVC / H.264             | HEVC Annex-B             |
| Audio        | AAC 16kHz                | MP2                      |
| Contenedor   | MP4 estándar + HKMI      | MPEG-PS raw (ext. .mp4)  |
| GOP          | 1 (cada frame = I-frame) | P-frames (sin IDR/chunk) |
| Duración     | segundos–minutos         | ~0.667s por slot         |
| Acceso       | WiFi / HTTP ISAPI        | SD card directa          |

---

## 10. Firmas binarias — tabla de referencia

| Firma      | Hex                       | Aplica al CS-EB3 |
|------------|---------------------------|------------------|
| `OFNI`     | `4F 46 4E 49`             | ✓ índice principal |
| `RATS`     | `52 41 54 53`             | ✓ log de eventos |
| `HKMI`     | `48 4B 4D 49`             | ✓ en ftyp de MP4 descargados |
| `HIKVISION`| `48 49 4B 56 49 53 49 4F 4E` | ✗ No encontrada |
| `HFS\x00`  | `48 46 53 00`             | ✗ No encontrada |
| `HIKVIDX`  | `48 49 4B 56 49 44 58 00` | ✗ No encontrada (es de NVR/DVR) |

> **Nota:** Documentos externos sobre Hikvision genérico describen firmas
> `HIKVIDX` y entradas de 16 bytes. Eso corresponde a sistemas NVR/DVR de red,
> **no** a la cámara autónoma CS-EB3. Siempre verificar contra esta guía.

---

## 11. Errores comunes y sus causas

| Error                        | Causa                                          | Solución                           |
|------------------------------|------------------------------------------------|------------------------------------|
| Horarios desplazados 3h      | Uso de timezone local en la conversión         | `tz=timezone.utc`                  |
| Clip de 257 bytes            | `dur = ts_e − ts_s = 0` → ffmpeg extrae 0s    | Calcular duración desde SCR        |
| Video corrupto / sin imagen  | P-frames sin IDR/VPS/SPS/PPS                  | Prepend del primer pack del HIV    |
| ffmpeg no reconoce el HIV    | Se intenta abrir como MP4                      | Es MPEG-PS raw — sin `-f mp4`      |
| seek `-ss` incorrecto        | SCR no monótono por buffer circular            | Usar estrategia prepend + chunk    |
| Clips no aparecen en búsqueda | ts_s parece UTC pero es hora local            | Buscar con `tz=timezone.utc`       |
| Slot no encontrado           | Slot vacío (`+12 = 0x7FFFFFFF`) no filtrado   | Verificar estado antes de parsear  |
| Clip cortado al cruzar 256MB | No se maneja `cross_block`                    | Leer parte de hiv{N} + hiv{N+1}   |

---

## 12. Checklist de implementación

- [ ] Buscar firma `OFNI` — no asumir offset fijo
- [ ] Filtrar slots vacíos (`ch[12:16] == b"\xff\xff\xff\x7f"`)
- [ ] Validar `TS_MIN < ts_s < TS_MAX`
- [ ] Usar `tz=timezone.utc` para timestamps — **no** zona local
- [ ] `ts_e == ts_s` en index\*p.bin — calcular duración desde SCR
- [ ] `ts_e > ts_s` en index\*.bin — duración real disponible
- [ ] Calcular `hiv_idx = gl_s // 268_435_456`
- [ ] Manejar `cross_block` para clips que atraviesan dos HIV
- [ ] Extracción: prepend primer pack del HIV + chunk → ffmpeg
- [ ] El HIV es MPEG-PS raw — no parsear como MP4
- [ ] SCR no monótono — no usar `-ss` lineal sobre el HIV completo
- [ ] HIK header de 2440 bytes al inicio de cada chunk — ignorar

---

*Investigación por ingeniería inversa directa sobre archivos reales.*  
*Hardware: EZVIZ CS-EB3-R200-1K3FL4GA-LA · Firmware: V5.4.0 build 250520*  
*SD: 32 GB · Archivos analizados: index (×4), log (×2), hiv00000.mp4, 10× MP4 descargados*  
*Herramientas: Python 3, ffprobe, HxD hex editor*
