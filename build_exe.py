"""
build_exe.py — Genera sd-hik-reader.exe con PyInstaller (Windows)

Uso:
    pip install pyinstaller
    python build_exe.py
"""
import subprocess
import sys

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--onefile",
    "--windowed",
    "--name", "sd-hik-reader",
    "--add-data", "src;src",
    "main.py",
]

print("Ejecutando PyInstaller…")
print(" ".join(cmd))
subprocess.run(cmd, check=True)
print("\n✔ Ejecutable generado en dist/sd-hik-reader.exe")
