"""Point d'entrée de l'exécutable (PyInstaller) : double-clic = interface dans le navigateur."""
import sys

from vinted_bot.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
