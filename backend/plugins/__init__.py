from __future__ import annotations

import logging

from .telegram_bot import TelegramBotPlugin

logger = logging.getLogger("plugins")


def register_builtin_plugins(registry) -> None:
    """Регистрирует все встроенные плагины в едином реестре."""
    for plugin_cls in (TelegramBotPlugin,):
        try:
            registry.register_builtin(plugin_cls())
        except Exception as e:
            logger.warning(f"Failed to register builtin plugin {plugin_cls}: {e}")


__all__ = ["TelegramBotPlugin", "register_builtin_plugins"]
