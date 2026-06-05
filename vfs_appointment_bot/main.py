import argparse
import logging
import sys
from typing import Dict

from vfs_appointment_bot.utils.browser_profile import (
    clear_vfs_browser_fingerprints,
    quit_cdp_chrome,
)
from vfs_appointment_bot.utils.config_reader import get_config_value, initialize_config
from vfs_appointment_bot.utils.timer import countdown
from vfs_appointment_bot.vfs_bot.vfs_bot import (
    LoginError,
    VfsConnectivityError,
    VfsRateLimitError,
)
from vfs_appointment_bot.vfs_bot.vfs_bot_factory import (
    UnsupportedCountryError,
    get_vfs_bot,
)


class KeyValueAction(argparse.Action):
    """Custom action class for parsing appointment parameters.

    This class handles parsing comma-separated key-value pairs provided through
    the `--appointment-params` argument. It ensures the format is valid (key=value)
    and stores the parsed parameters as a dictionary.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        try:
            appointment_params: Dict[str, str] = {
                key.strip(): value.strip()
                for key, value in (item.split("=") for item in values.split(","))
            }
            setattr(namespace, "appointment_params", appointment_params)
        except ValueError:
            parser.error(
                f"Invalid value format for {option_string}, use key=value pairs"
            )


def _restriction_sleep_seconds() -> int:
    cooldown = get_config_value("anti_lock", "restriction_sleep_seconds", "7200")
    try:
        sec = int(cooldown or "7200")
    except ValueError:
        sec = 7200
    return max(60, min(sec, 86_400))


def _handle_long_cooldown(
    exc: Exception,
    *,
    countdown_label: str,
    telegram_intro: str,
) -> None:
    """Wait restriction_sleep_seconds, wipe browser profiles, then outer loop continues."""
    logging.warning("%s", exc)
    sec = _restriction_sleep_seconds()
    _h, _m = sec // 3600, (sec % 3600) // 60
    try:
        from vfs_appointment_bot.notification.telegram_client import TelegramClient

        TelegramClient().send_notification(
            f"{telegram_intro} (~{_h}h {_m}m). "
            "Browser profile will be cleared before the next attempt.\n\n"
            f"{exc}"
        )
    except Exception as alert_err:
        logging.warning("Could not send Telegram alert for long cooldown: %s", alert_err)
    countdown(sec, countdown_label)
    quit_cdp_chrome()
    clear_vfs_browser_fingerprints()


def main() -> None:
    """
    Entry point for the VFS Appointment Bot.

    This function sets up logging, parses command-line arguments, and runs the VFS appointment
    checking process in a continuous loop. It catches exceptions for unsupported countries and
    unexpected errors, logging them appropriately.

    Raises:
        UnsupportedCountryError: If the provided country code is not supported by the bot.
        Exception: For any other unexpected errors encountered during execution.
    """
    initialize_logger()
    initialize_config()

    parser = argparse.ArgumentParser(
        description="VFS Appointment Bot: Checks for appointments at VFS Global"
    )
    required_args = parser.add_argument_group("required arguments")
    required_args.add_argument(
        "-sc",
        "--source-country-code",
        type=str,
        help="The ISO 3166-1 alpha-2 source country code (refer to README)",
        metavar="<country_code>",
        required=True,
    )

    required_args.add_argument(
        "-dc",
        "--destination-country-code",
        type=str,
        help="The ISO 3166-1 alpha-2 destination country code (refer to README)",
        metavar="<country_code>",
        required=True,
    )

    parser.add_argument(
        "-ap",
        "--appointment-params",
        type=str,
        default=None,
        help="Comma-separated key-value pairs for additional appointment details (refer to VFS website)",
        action=KeyValueAction,
        metavar="<key1=value1,key2=value2,...>",
    )

    args = parser.parse_args()
    source_country_code = args.source_country_code
    destination_country_code = args.destination_country_code
    # from vfs_appointment_bot.notification.email_client import EmailClient
    # client = EmailClient()
    # client.send_notification("hello")
    try:
        while True:
            vfs_bot = get_vfs_bot(source_country_code, destination_country_code)
            try:
                appointment_found = vfs_bot.run(args)
            except VfsRateLimitError as e:
                _handle_long_cooldown(
                    e,
                    countdown_label=(
                        "VFS rate limit / permission cooldown (e.g. 429201) — next run after"
                    ),
                    telegram_intro=(
                        "VFS bot: permission / rate limit (e.g. 429201) — every configured "
                        "account was blocked this run. Entering long cooldown. "
                        "Try another Wi‑Fi/VPN if needed"
                    ),
                )
                continue
            except LoginError as e:
                _handle_long_cooldown(
                    e,
                    countdown_label="All accounts failed login — long cooldown, next run after",
                    telegram_intro=(
                        "VFS bot: every configured account failed login this run "
                        "(passwords, VFS errors, or page-not-found). Entering long cooldown"
                    ),
                )
                continue
            except VfsConnectivityError:
                countdown(
                    int(
                        get_config_value(
                            "default", "connectivity_retry_seconds", "15"
                        )
                    ),
                    "Next retry after VFS connectivity / page-not-found",
                )
                continue
            if appointment_found:
                break
            countdown(
                int(get_config_value("default", "interval")),
                "Next appointment check in",
            )

    except UnsupportedCountryError as e:
        logging.error(e)
    except Exception as e:
        logging.exception(e)


def initialize_logger():
    file_handler = logging.FileHandler("app.log", mode="a")
    file_handler.setFormatter(
        logging.Formatter(
            "[%(asctime)s] %(levelname)s [%(filename)s:%(lineno)d] %(message)s"
        )
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s"))
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s [%(filename)s:%(lineno)d] %(message)s",
        handlers=[
            file_handler,
            stream_handler,
        ],
    )


if __name__ == "__main__":
    main()
