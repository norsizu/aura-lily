from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import PersonaGatewayConfig


@dataclass(frozen=True)
class PersonaAssets:
    soul: str
    source_path: str

    @property
    def available(self) -> bool:
        return bool(self.soul.strip())


def load_persona_assets(config: PersonaGatewayConfig) -> PersonaAssets:
    candidates = _asset_candidates(config)
    for path in candidates:
        if path.exists() and path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return PersonaAssets(soul=_clip(text, config.max_soul_chars), source_path=str(path))
    return PersonaAssets(soul="", source_path="")


def _asset_candidates(config: PersonaGatewayConfig) -> list[Path]:
    persona_root = Path(config.persona_home).expanduser() / "persona"
    return [persona_root / "soul.md"]


def _clip(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip()
