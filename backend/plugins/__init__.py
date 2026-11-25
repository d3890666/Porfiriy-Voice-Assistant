from __future__ import annotations

import logging

from .telegram_bot import TelegramBotPlugin
from .memory import MemoryPlugin

logger = logging.getLogger("plugins")


def register_builtin_plugins(registry) -> None:
    """Регистрирует все встроенные плагины в едином реестре."""
    for plugin_cls in (TelegramBotPlugin, MemoryPlugin):
        try:
            registry.register_builtin(plugin_cls())
        except Exception as e:
            logger.warning(f"Failed to register builtin plugin {plugin_cls}: {e}")


__all__ = ["TelegramBotPlugin", "MemoryPlugin", "register_builtin_plugins"]
