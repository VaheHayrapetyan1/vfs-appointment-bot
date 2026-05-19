import logging

import requests

from vfs_appointment_bot.notification.notification_client import NotificationClient


class TelegramClient(NotificationClient):
    """Concrete implementation of NotificationClient for the Telegram channel.

    This class provides functionality for sending notifications through the Telegram
    messaging platform. It inherits from the abstract `NotificationClient` class
    and implements the required `send_notification` method for Telegram-specific
    notification sending logic.
    """

    def __init__(self):
        """
        Initializes the Telegram client with configuration data.

        This constructor retrieves configuration settings from the "telegram"
        section of the application configuration and validates them using the
        base class validation logic.
        """
        required_keys = ["bot_token", "chat_id"]
        super().__init__("telegram", required_keys)

    def send_notification(self, message: str) -> None:
        """
        Sends a notification message through the Telegram channel.

        This method sends a POST to the Telegram Bot API with JSON body (avoids
        broken GET URLs when the message contains spaces or special characters).
        Optional [telegram] parse_mode applies when set (e.g. HTML, Markdown).

        Args:
            message (str): The message content to be sent as a Telegram notification.
        """
        bot_token: str = self.config.get("bot_token")
        chat_id: str = self.config.get("chat_id")
        parse_mode = self.config.get("parse_mode")

        payload = {"chat_id": chat_id.strip(), "text": message}
        if parse_mode and str(parse_mode).strip():
            payload["parse_mode"] = str(parse_mode).strip()

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        resp = requests.post(url, json=payload, timeout=30)
        data = resp.json()
        if not data.get("ok"):
            logging.error("Telegram API error: %s", data)
            raise RuntimeError(f"Telegram sendMessage failed: {data}")
        logging.info("Telegram message sent successfully!")
