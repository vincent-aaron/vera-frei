"""Vera Frei for Hermes. Her code lives in profiles\\vera\\app (package `vera`); this only loads it.

The app folder is added at the END of sys.path, so nothing in it can shadow Hermes's own modules.
"""

import os
import sys
from pathlib import Path


def _app_folder() -> Path:
    """Next to this plugin normally; Hermes may load a copy from elsewhere (Plugin Doctor does), so known places too."""
    candidates = [Path(__file__).resolve().parents[2] / "app"]
    if os.getenv("VERA_APP"):
        candidates.insert(0, Path(os.environ["VERA_APP"]))
    if os.getenv("LOCALAPPDATA"):
        candidates.append(Path(os.environ["LOCALAPPDATA"]) / "hermes" / "profiles" / "vera" / "app")
    for folder in candidates:
        if (folder / "vera" / "__init__.py").is_file():
            return folder
    raise ImportError("Vera's app folder wasn't found. Set VERA_APP to profiles\\vera\\app.")


APP = _app_folder()
if str(APP) not in sys.path:
    sys.path.append(str(APP))


def register(ctx):
    from vera.hermes.plugin import register as register_vera
    register_vera(ctx)
