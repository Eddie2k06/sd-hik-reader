# sd-hik-reader

**Extractor de clips de vídeo desde tarjetas SD de cámaras Hikvision/EZVIZ**

Herramienta de escritorio (un solo archivo Python, sin dependencias externas) para leer, inspeccionar y extraer clips directamente desde la tarjeta SD, sin red ni acceso a la nube.

> Ingeniería inversa original sobre el modelo **CS-EB3-R200-1K3FL4GA-LA** (EZVIZ) con firmware **V5.4.0 build 250520**. Compatible con otros modelos que usen el filesystem HFS y el formato de índice OFNI.

---

## Características

- **Carga automática** del índice `index00.bin` / `index00p.bin` desde la raíz de la SD
- **Tabla de clips** con columnas de inicio, fin, duración, tamaño, archivo HIV y estado de integridad
- **Duración real por SCR** — lee el System Clock Reference del stream MPEG-PS; no confía en los timestamps del índice (que pueden estar desfasados hasta ~13s respecto al OSD del video)
- **Filtros por fecha y hora** — combos de día/mes/año + HH:MM para ambos extremos del rango
- **Orden por columna** — click en cualquier cabecera, ascendente/descendente
- **Verificación de integridad** — compara duración SCR contra estimado por tamaño de bytes (`✓` / `!`)
- **Detección de gaps** — ventana con los períodos sin grabación mayores a 1 minuto
- **Exportar CSV** — metadatos completos de todos los clips visibles
- **Extracción de clips** — copia directa de bytes desde el HIV, sin re-encode
- **Conversión a MP4** — vía ffmpeg en un solo paso (requiere ffmpeg en el PATH)
- **Vista previa** — doble click o botón "Ver clip" abre el archivo en el reproductor del sistema
- **Cálculo SCR en background** — la tabla se actualiza progresivamente sin bloquear la UI

---

## Requisitos

- **Python 3.11+** (solo stdlib — sin pip install)
- **ffmpeg** *(opcional)* — solo para la opción "Convertir a .mp4"
  - Windows: https://ffmpeg.org/download.html → agregar la carpeta `bin/` al PATH
  - O copiar `ffmpeg.exe` a `C:\ffmpeg\bin\`

---

## Uso

```bash
python sd-hik-reader.py
```

### Workflow típico

1. Insertar la SD en un lector de tarjetas
2. En Windows: la SD aparece como unidad (ej. `D:\`) — si Windows pide formatear → **cancelar**
3. En **SD / carpeta** ingresar la ruta raíz de la SD (ej. `D:\`) y hacer click en **Cargar**
4. La tabla se pobla con todos los clips; las duraciones se calculan en background via SCR
5. Usar los filtros de fecha/hora para acotar el rango
6. Seleccionar uno o varios clips → **⬇ Extraer seleccionados**
7. Opcional: activar **Convertir a .mp4** antes de extraer (requiere ffmpeg)

---

## Formato de la SD — resumen técnico

El filesystem es propietario **HFS** (Hikvision File System). La herramienta accede a los archivos por ruta directa sin necesidad de montar el filesystem.

| Archivo | Descripción |
|---------|-------------|
| `index00p.bin` | Índice activo — entradas OFNI (80 bytes c/u) |
| `index00.bin` | Índice backup |
| `hiv00000.mp4` … `hiv0000N.mp4` | Bloques de video de 256 MB — **MPEG-PS raw** (la extensión `.mp4` es convención del firmware) |

### Estructura de una entrada OFNI (80 bytes, little-endian)

| Offset | Tipo | Campo |
|--------|------|-------|
| `+00` | uint32 | Timestamp de escritura del slot (no es el inicio real del video) |
| `+08` | uint32 | Timestamp real de inicio del video (±1s del OSD) |
| `+24` | uint32 | Byte offset de inicio del clip dentro del HIV |
| `+28` | uint32 | Byte offset de fin del clip dentro del HIV |
| `+72` | uint32 | Timestamp de fin — solo válido si `ts_fin - ts_ini` está entre 1s y 1200s |

### Firmas binarias

| Firma | Descripción |
|-------|-------------|
| `OFNI` (`0x494E464F`) | Entrada de índice |
| `00 00 01 BA` | PS Pack Header — inicio de cada chunk de video en el HIV |

### Bug de timestamps (verificado CS-EB3 / V5.4.0)

El firmware graba la **hora local** directamente en el campo Unix timestamp, sin offset UTC. Leer con `timezone.utc` devuelve la hora correcta tal como aparece en el OSD.

```python
# CORRECTO
datetime.fromtimestamp(ts, tz=timezone.utc)

# INCORRECTO — desplaza el offset de la zona horaria del sistema
datetime.fromtimestamp(ts, tz=zona_local)
```

### Duración de clips

El campo `+72` del índice no es confiable para todos los clips — en algunos registros el firmware escribe el timestamp de cierre del archivo HIV en lugar del fin del clip individual, produciendo duraciones falsas de varios minutos para clips de pocos segundos. La herramienta usa exclusivamente la diferencia de SCR (`max(scrs_tail) - min(scrs_head)`) como fuente de duración real.

---

## Compatibilidad

| Modelo | Firmware | Estado |
|--------|----------|--------|
| CS-EB3-R200-1K3FL4GA-LA (EZVIZ) | V5.4.0 build 250520 | ✅ Verificado |

Si tenés otro modelo compatible, abrí un issue con modelo, versión de firmware y los primeros 256 bytes en hex de `index00p.bin`.

---

## Limitaciones conocidas

- Los clips extraídos corresponden al **sub-stream** almacenado en SD (≈15fps, ~3.4 Mb/s H.265). El stream principal solo está disponible por WiFi.
- El filesystem HFS no es montable nativamente en Windows. Los archivos HIV e índices son accesibles por ruta directa desde el explorador de archivos o la herramienta.
- Los archivos `hiv*.mp4` tienen extensión `.mp4` por convención del firmware, pero son **MPEG-PS raw** — se reproducen directamente en VLC y MPC-HC; QuickTime y el reproductor de Windows no los soportan sin conversión.
- Clips con SCR no determinable (stream corrupto o tamaño 0) aparecen con duración `—` y se omiten de los totales.

---

## No afiliado con Hikvision, Dahua ni EZVIZ. Licencia MIT.
