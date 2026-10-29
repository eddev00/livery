"""Where the desktop engine keeps its data, and where the packaged resources are."""
from __future__ import annotations

import os
from pathlib import Path

NEUTRAL_DIR = Path(__file__).resolve().parent / "neutral"
NEUTRAL_FONTS = NEUTRAL_DIR / "assets" / "fonts"
# Files of a design revision: the project theme, the design layer, the custom rules and the
# import mapping.
DESIGN_FILES = ("theme.toml", "design.json", "custom.typ", "mapping.toml")
# App-owned template files: never edited by an agent, copied into every render root.
CORE_FILES = ("template.typ",)


def app_home() -> Path:
    """$LIVERY_HOME, else $XDG_DATA_HOME/livery, else ~/.local/share/livery."""
    explicit = os.environ.get("LIVERY_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve()
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg).expanduser().resolve() / "livery"


def projects_dir() -> Path:
    return app_home() / "projects"


def profiles_dir() -> Path:
    """Livery's own login profiles for the agents, one folder per provider."""
    return app_home() / "profiles"


def workspaces_dir() -> Path:
    """Sanitized agent workspaces. Deliberately a sibling of projects/, never inside it."""
    return app_home() / "workspaces"
