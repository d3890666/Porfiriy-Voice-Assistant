from __future__ import annotations

import logging

from .telegram_bot import TelegramBotPlugin
from .memory import MemoryPlugin
from .telegram_userbot import TelegramUserbotPlugin
from .mail_imap import MailImapPlugin
from .keenetic import KeeneticPlugin
from .kinowatch import KinoWatchPlugin

logger = logging.getLogger("plugins")


def register_builtin_plugins(registry) -> None:
    """Регистрирует все встроенные плагины в едином реестре."""
    for plugin_cls in (TelegramBotPlugin, MemoryPlugin, TelegramUserbotPlugin, MailImapPlugin, KeeneticPlugin, KinoWatchPlugin):
        try:
            registry.register_builtin(plugin_cls())
        except Exception as e:
            logger.warning(f"Failed to register builtin plugin {plugin_cls}: {e}")


__all__ = [
    "TelegramBotPlugin",
    "MemoryPlugin",
    "TelegramUserbotPlugin",
    "MailImapPlugin",
    "KeeneticPlugin",
    "KinoWatchPlugin",
    "register_builtin_plugins",
]
