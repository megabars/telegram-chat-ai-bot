import logging
import re
import sys
from collections.abc import Sequence


class SafeLogFilter(logging.Filter):
    def __init__(self, secrets: Sequence[str] = ()) -> None:
        super().__init__()
        self.secrets = tuple(value for value in secrets if value)

    def filter(self, record: logging.LogRecord) -> bool:
        # Third-party exception strings/debug bodies can include group text,
        # URLs containing tokens or headers. Keep library/level/exception type metadata.
        if not record.name.startswith("app."):
            record.msg = f"Library event source={record.name} level={record.levelname}"
            candidates = list(record.args) if isinstance(record.args, tuple) else []
            if record.exc_info:
                candidates.append(record.exc_info[1])
            error = next((value for value in candidates if isinstance(value, BaseException)), None)
            if error is not None:
                record.msg += f" kind={type(error).__name__}"
        else:
            message = record.getMessage()
            for secret in self.secrets:
                message = message.replace(secret, "[REDACTED]")
            message = re.sub(r"(?i)authorization\s*[:=].*", "Authorization=[REDACTED]", message)
            record.msg = message
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def configure_logging(level: str, secrets: Sequence[str] = ()) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(SafeLogFilter(secrets))
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=level, handlers=[handler], force=True)
    for name in (
        "aiogram",
        "telethon",
        "openai",
        "httpx",
        "httpx2",
        "httpcore",
        "httpcore2",
        "aiohttp",
        "aiosqlite",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)
