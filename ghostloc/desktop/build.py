"""Construit l'app GhostLoc avec PyInstaller : dist/GhostLoc/ (+ archive zip).

    pip install -r requirements.txt pyinstaller
    python build.py
"""

import os
import shutil
import sys
from pathlib import Path

import PyInstaller.__main__

HERE = Path(__file__).resolve().parent
SEP = ";" if os.name == "nt" else ":"
CONFIG = HERE.parent / "config.json"


def main() -> None:
    os.chdir(HERE)
    PyInstaller.__main__.run([
        str(HERE / "ghostloc_desktop" / "__main__.py"),
        "--name", "GhostLoc",
        "--noconfirm",
        "--clean",
        "--console",  # la fenêtre de console sert d'indicateur : la fermer arrête GhostLoc
        "--paths", str(HERE),
        "--add-data", f"{HERE / 'ghostloc_desktop' / 'ui'}{SEP}ui",
        "--add-data", f"{CONFIG}{SEP}.",
        "--collect-all", "pymobiledevice3",
        "--collect-all", "pytun_pmd3",  # embarque wintun.dll, chargé à l'import sous Windows
        # Plusieurs dépendances lisent leur propre version via importlib.metadata au chargement.
        "--recursive-copy-metadata", "pymobiledevice3",
        # Modules du CLI pymobiledevice3 sans rapport avec la localisation (vidéo, shell interactif).
        *[arg for mod in ("av", "IPython", "jedi", "matplotlib", "tkinter", "pytest") for arg in ("--exclude-module", mod)],
        "--collect-submodules", "uvicorn",
        "--collect-submodules", "ghostloc_desktop",
    ])
    label = {"win32": "Windows", "darwin": "macOS"}.get(sys.platform, "Linux")
    archive = shutil.make_archive(str(HERE / "dist" / f"GhostLoc-{label}"), "zip", HERE / "dist", "GhostLoc")
    print("Archive :", archive)


if __name__ == "__main__":
    main()
