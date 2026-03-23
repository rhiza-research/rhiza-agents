"""Chat event logging configuration."""

import logging

chat_event_logger = logging.getLogger("rhiza_agents.chat_events")


def setup_logging(log_level: str = "INFO", chat_event_logging: str = "false"):
    """Configure structured logging with JSON formatter for chat events."""
    from pythonjsonlogger.json import JsonFormatter

    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Replace any existing handlers on root with a basic stderr handler
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s: %(name)s: %(message)s"))
    root.addHandler(console)

    # Configure chat event logger with JSON output
    chat_event_logger.handlers.clear()
    chat_event_logger.propagate = False
    if chat_event_logging != "false":
        json_handler = logging.StreamHandler()
        json_handler.setFormatter(
            JsonFormatter(
                fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
                rename_fields={"asctime": "timestamp", "levelname": "level", "name": "logger"},
            )
        )
        chat_event_logger.addHandler(json_handler)
        chat_event_logger.setLevel(logging.INFO)
    else:
        chat_event_logger.setLevel(logging.CRITICAL)
