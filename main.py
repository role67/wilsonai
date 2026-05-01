import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass

from wilsonai.core.config import settings


@dataclass(frozen=True)
class LaunchOptions:
    check_only: bool
    show_config: bool


def parse_args() -> LaunchOptions:
    parser = argparse.ArgumentParser(
        prog="telegram-account-agent",
        description="Run Telegram account automation agent.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate basic config and exit without connecting to Telegram.",
    )
    parser.add_argument(
        "--show-config",
        action="store_true",
        help="Print non-sensitive runtime settings.",
    )
    args = parser.parse_args()
    return LaunchOptions(check_only=args.check, show_config=args.show_config)


def setup_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    class PrettyFormatter(logging.Formatter):
        LEVEL_ICON = {
            "DEBUG": "·",
            "INFO": "i",
            "WARNING": "!",
            "ERROR": "x",
            "CRITICAL": "!!",
        }

        def format(self, record: logging.LogRecord) -> str:
            ts = self.formatTime(record, "%H:%M:%S")
            icon = self.LEVEL_ICON.get(record.levelname, "?")
            level = record.levelname.ljust(8)
            name = record.name
            message = record.getMessage()
            return f"{ts} {icon} {level} {name}: {message}"

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    handler = logging.StreamHandler()
    handler.setFormatter(PrettyFormatter())
    root.addHandler(handler)


def print_runtime_settings() -> None:
    print(f"session_name={settings.session_name}")
    print(f"model={settings.model}")
    print(f"fallback_models={len(settings.fallback_models)} configured")
    print(f"database_enabled={bool(settings.database_url)}")
    print(f"respond_to_all_private={settings.respond_to_all_private}")
    print(f"typing_wait_enabled={settings.typing_wait_enabled}")
    print(f"message_batch_delay={settings.message_batch_delay}")


def validate_settings() -> None:
    missing = []
    if not settings.api_id:
        missing.append("TELEGRAM_API_ID")
    if not settings.api_hash:
        missing.append("TELEGRAM_API_HASH")
    if not settings.google_api_key:
        missing.append("GOOGLE_API_KEY")
    if not settings.admin_id:
        missing.append("ADMIN_ID")
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"Missing required settings: {joined}")
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required for this app.")


def validate_runtime_dependencies() -> None:
    try:
        import psycopg  # noqa: F401
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing dependency 'psycopg'. Install with: "
            f"\"{sys.executable}\" -m pip install \"psycopg[binary]==3.2.3\". "
            f"Current interpreter: {sys.executable}"
        ) from exc


def main() -> int:
    setup_logging()
    options = parse_args()
    validate_runtime_dependencies()
    validate_settings()
    from wilsonai.telegram.agent import run
    if options.show_config:
        print_runtime_settings()
    if options.check_only:
        print("Configuration check passed.")
        return 0
    asyncio.run(run())
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Stopped by user.", file=sys.stderr)
        raise SystemExit(130)
