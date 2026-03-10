# sd-hik-reader

**Visor e inspector de tarjetas SD para cámaras con firmware Hikvision**

Herramienta de escritorio para leer, inspeccionar y extraer clips de vídeo directamente desde tarjetas SD de cámaras con firmware Hikvision (incluyendo modelos comercializados bajo otras marcas que usan el mismo sistema de archivos HFS), sin necesidad de red ni acceso a la nube.

> Investigación original por ingeniería inversa sobre el modelo **CS-EB3-R200-1K3FL4GA-LA** (comercializado bajo marca EZVIZ) con firmware **V5.4.0 build 250520**. Compatible con otros modelos que usen el filesystem HFS y el formato OFNI/RATS.

---

## Características

- **Dashboard** — resumen visual con métricas, lista de archivos detectados y notas técnicas
- **Línea de tiempo** — visualización por día/hora de todos los eventos grabados
- **Clips** — tabla completa con filtros, búsqueda, orden y descarga individual como MP4
- **Logs RATS** — eventos de movimiento y sistema del log interno de la cámara
- **Índices OFNI** — inspección detallada de cada entrada del índice con campos binarios
- **Visor HEX** — explorador hexadecimal de cualquier archivo de la SD (index, log, HIV)
- **Exportar JSON** — volcado completo del escaneo en JSON para análisis externo

### Extracción de clips

Lee directamente los archivos `hiv*.mp4` (streams MPEG-PS raw) sin pasar por la cámara ni por la red. Requiere **ffmpeg** instalado.

- Copia directa sin re-encodear (más rápido)
- Re-escalado a 1080p / 720p / 480p con libx265
- Soporte para chunks que cruzan el límite de 256 MB entre archivos HIV (cross-block)

---

## Requisitos

- **Python 3.11+**
- **ffmpeg** (solo para descarga de clips)
  - Windows: https://ffmpeg.org/download.html → agregar `bin/` al PATH
  - O copiar a `C:\ffmpeg\bin\ffmpeg.exe`

Sin dependencias de terceros — solo stdlib de Python.

---

## Instalación y uso

```bash
git clone https://github.com/TU_USUARIO/sd-hik-reader.git
cd sd-hik-reader
python main.py
```

### Workflow típico

1. Insertar la SD en un lector de tarjetas
2. En Windows: la SD aparece como unidad (ej. `E:\`) — Windows pedirá formatear → **cancelar**
3. Abrir sd-hik-reader → **Carpeta SD** o **Unidad**
4. Navegar entre los tabs para inspeccionar índices, logs y clips
5. Seleccionar un clip en la tab **Clips** → **⬇ Descargar clip**

> El filesystem HFS no es legible por Windows nativamente, pero los archivos individuales son accesibles por ruta directa. En algunos casos puede ser necesario usar un lector en Linux.

---

## Generar ejecutable Windows (.exe)

```bash
pip install pyinstaller
python build_exe.py
# → dist/sd-hik-reader.exe  (no requiere Python instalado)
```

---

## Formato de la SD — resumen técnico

El filesystem es propietario **HFS** (Hikvision File System). Archivos en la raíz:

| Archivo | Descripción |
|---------|-------------|
| `index00p.bin` | Índice activo — entradas OFNI (80 bytes c/u) |
| `index00.bin` | Índice backup |
| `index01p.bin`, `index01.bin` | Redundancia del canal (mismo contenido) |
| `logCurFile.bin` | Log de eventos RATS (72 bytes c/u) |
| `logMainFile.bin` | Log histórico |
| `hiv00000.mp4` … | Bloques de video de 256 MB — **MPEG-PS raw**, no MP4 real |

### Firmas binarias

| Firma | Descripción |
|-------|-------------|
| `OFNI` | Entrada de índice (80 bytes) |
| `RATS` | Registro de log (72 bytes) |
| `00 00 01 BA` | Pack header MPEG-PS (inicio de cada chunk de video) |

### Bug de timestamps (verificado en CS-EB3 / V5.4.0)

El firmware graba la **hora local** directamente en el campo Unix timestamp, sin aplicar offset UTC. Al leer con `timezone.utc` se obtiene la hora correcta tal como aparece en el OSD del video.

```python
# CORRECTO — muestra la hora local de la cámara
datetime.fromtimestamp(ts_s, tz=timezone.utc)

# INCORRECTO — desplaza el offset de la zona horaria configurada
datetime.fromtimestamp(ts_s, tz=zona_local)
```

Para la documentación completa del formato binario ver [`HIK_SD_Format.md`](HIK_SD_Format.md).

---

## Compatibilidad

Probado con:

| Modelo | Firmware | Estado |
|--------|----------|--------|
| CS-EB3-R200-1K3FL4GA-LA | V5.4.0 build 250520 | ✅ Verificado |

Si tenés otro modelo compatible, abrí un issue con el modelo, versión de firmware y los primeros 256 bytes en hex de `index00p.bin`.

---

## Limitaciones conocidas

- Los clips extraídos son del **sub-stream** almacenado en SD (~0.667s por slot, 15fps). El stream principal solo se obtiene por WiFi.
- El filesystem HFS no es montable en Windows sin software adicional.
- Los archivos `hiv*.mp4` tienen extensión `.mp4` por convención del firmware, pero son MPEG-PS raw.
- No afiliado ni respaldado por Hikvision, Dahua Technology ni ningún fabricante.

---

## Licencia

MIT — ver [`LICENSE`](LICENSE)
