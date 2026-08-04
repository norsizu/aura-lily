"""Lily-native Aura persona gateway.

This package keeps the clean Lily runtime boundary: persona/state context is
assembled locally, then sent through ``integrations.hermes_lily_cli``.
"""

from .config import PersonaGatewayConfig
from .turn import AuraPersonaGateway

__all__ = ["AuraPersonaGateway", "PersonaGatewayConfig"]
