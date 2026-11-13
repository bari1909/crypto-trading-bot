import logging

import requests

from app.config import settings

log = logging.getLogger(__name__)


def telegram_send(message: str):
    # Tag every message so the spot and futures bots are distinguishable in the same chat.
    message = f"[{settings.trading_mode} {settings.market}] {message}"
    if not settings.telegram_bot_token or not settings.allowed_chat_ids:
        log.info("notify: %s", message)
        return
    for chat_id in settings.allowed_chat_ids:
        try:
            requests.post(f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                          json={"chat_id": chat_id, "text": message[:4000]}, timeout=8).raise_for_status()
        except Exception:  # noqa: BLE001 - notifications must never break trading
            log.exception("Telegram notification failed")
