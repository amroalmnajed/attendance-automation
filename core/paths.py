from __future__ import annotations
import os
from pathlib import Path

def data_dir() -> Path:
    """Folder for runtime data. Set DATA_DIR env var (Railway: /app/data). Default: ./data"""
    p = Path(os.getenv("DATA_DIR", "data")).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p

def data_path(value: str | os.PathLike) -> Path:
    """Absolute paths are returned unchanged. Relative paths are placed inside data_dir()."""
    p = Path(value)
    return p if p.is_absolute() else data_dir() / p.name
