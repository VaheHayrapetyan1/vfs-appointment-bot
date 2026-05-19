import smtplib
import logging

from vfs_appointment_bot.notification.notification_client import NotificationClient


class EmailClient(NotificationClient):
    def __init__(self):
        """
        Initializes the email client with configuration data.

        This constructor retrieves configuration settings from the designated
        section (e.g., `"email"`) of the application configuration and
        validates them using the base class validation logic.
        """
        required_keys = ["email", "password"]
        super().__init__("email", required_keys)

    def send_notification(self, message: str) -> None:
        """
        Sends a notification message through the email channel.

        This method sends an email notification using the provided message content.
        It connects securely to the configured SMTP server (e.g., Gmail's SMTP),
        authenticates with the provided credentials, and constructs a well-formatted
        email before sending it.

        Args:
            message (str): The message content to be included in the email.
        """
        email: str = self.config.get("email")
        password: str = self.config.get("password")
        # Recipient: optional `to` or `to_email`; otherwise send to self (same as `email`)
        to_addr = (
            self.config.get("to") or self.config.get("to_email") or email
        ).strip()
        email_text = self.__construct_email_text(email, to_addr, message)
        smtp_server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
        smtp_server.ehlo()
        smtp_server.login(email, password)
        smtp_server.sendmail(email, to_addr, email_text)
        smtp_server.close()
        logging.info("Email sent successfully to %s", to_addr)

    def __construct_email_text(self, from_addr: str, to_addr: str, message: str) -> str:
        """
        Constructs a formatted email text with sender, receiver, subject,
        and message body.

        Args:
            message (str): The message content to be included in the email body.

        Returns:
            str: The formatted email text ready for sending.
        """
        return (
            f"From: {from_addr}\nTo: {to_addr}\n"
            f"Subject: VFS Appointment Bot Notification\n\n{message}"
        )
