from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_ENV_LINE = re.compile(r"^\s*([A-Z0-9_]+)\s*=\s*(.*)$")
_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


ROOT = Path(__file__).resolve().parent.parent
load_env(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    api_key: str = os.getenv("MINIMAX_API_KEY", "")
    host: str = os.getenv("MINIMAX_API_HOST", "https://api.minimaxi.com")
    model_brain: str = os.getenv("MODEL_BRAIN", "MiniMax-M3.1-Flash-Preview")
    model_fast: str = os.getenv("MODEL_FAST", "MiniMax-M2.5-highspeed")
    model_backup: str = os.getenv("MODEL_BACKUP", "MiniMax-M2.5")
    temperature: float = float(os.getenv("LLM_TEMPERATURE", "0") or "0")
    timeout: int = int(os.getenv("LLM_TIMEOUT", "60") or "60")
    mock: bool = os.getenv("MOCK_MODE", "false").strip().lower() in _TRUTHY

    @property
    def endpoint(self) -> str:
        return self.host.rstrip("/") + "/v1/chat/completions"


def get_settings() -> Settings:
    return Settings()
