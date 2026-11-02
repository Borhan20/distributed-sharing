from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


def config_path() -> Path:
    base = os.environ.get("POOL_CONFIG_DIR") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "pool")
    return Path(base) / "config.json"


@dataclass
class Config:
    server: str = "http://127.0.0.1:8000"
    token: str | None = None
    default_group: str | None = None

    @classmethod
    def load(cls) -> Config:
        p = config_path()
        data = json.loads(p.read_text()) if p.exists() else {}
        cfg = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        cfg.server = os.environ.get("POOL_SERVER", cfg.server)
        cfg.token = os.environ.get("POOL_TOKEN", cfg.token)
        return cfg

    def save(self) -> Path:
        p = config_path()
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The file holds a bearer token: owner-only permissions.
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(self), f, indent=2)
        os.chmod(p, 0o600)
        return p
