from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Settings:
    root: Path
    values: dict[str, Any]

    def section(self, name: str) -> dict[str, Any]:
        return self.values.get(name, {})

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self.section(section).get(key, default)

    def path(self, section: str, key: str) -> Path:
        value = Path(str(self.get(section, key)))
        return value if value.is_absolute() else self.root / value


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_settings(config_path: str | Path = "config.toml") -> Settings:
    path = Path(config_path).resolve()
    _load_env(path.parent / ".env")
    with path.open("rb") as handle:
        values = tomllib.load(handle)
    return Settings(root=path.parent, values=values)
