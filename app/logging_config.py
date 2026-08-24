"""結構化 JSON Logging(spec 一)。"""
import json
import logging
import sys
from datetime import datetime, timezone

from app.services.secret_sanitizer import sanitize, sanitize_text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": sanitize_text(record.getMessage()),
        }
        if record.exc_info:
            payload["exc"] = sanitize_text(self.formatException(record.exc_info))
        extra = getattr(record, "extra_data", None)
        if extra:
            payload.update(sanitize(extra))
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
