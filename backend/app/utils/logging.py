"""Structured JSON logging with credential redaction (NF-007, SE-006)."""
import json
import logging
import re
import sys
from datetime import datetime, timezone

# Patterns that look like secrets — redacted before any log write (SE-006).
_REDACT_PATTERNS = [
    re.compile(r"(AKIA[0-9A-Z]{16})"),  # AWS access key id
    re.compile(r"(?i)(aws_secret_access_key\s*[=:]\s*)([^\s\"']+)"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)([^\s\"']+)"),
    re.compile(r"(?i)(secret\s*[=:]\s*)([^\s\"']+)"),
    re.compile(r"(?i)(password\s*[=:]\s*)([^\s\"']+)"),
    re.compile(r"(sk-[A-Za-z0-9]{20,})"),  # generic openai-style key
    # JSON string fields, e.g. {"password":"x"}, {"access_token":"<jwt>"} — the patterns
    # above only catch key=value / key: value forms, so this covers quoted JSON keys (SE-006).
    re.compile(r'(?i)("(?:password_hash|access_token|password|token)"\s*:\s*")([^"]*)'),
]


def redact(text: str) -> str:
    if not text:
        return text
    out = text
    for pat in _REDACT_PATTERNS:
        if pat.groups >= 2:
            out = pat.sub(lambda m: f"{m.group(1)}***REDACTED***", out)
        else:
            out = pat.sub("***REDACTED***", out)
    return out


def redact_phi(text: str) -> str:
    """Credential redaction PLUS PHI/PII redaction (G6).

    Use anywhere untrusted free text (harvested forum content, request/response bodies,
    audit context) could be persisted to logs, the audit table, or APP_EVENTS. The PHI
    layer is imported lazily to avoid a circular import with ``app.compliance.phi``.
    """
    if not text:
        return text
    out = redact(text)
    try:
        from app.compliance import phi  # lazy: phi imports this module for its logger

        out = phi.redact(out)[0]
    except Exception:  # noqa: BLE001 — never let redaction break a log/audit write
        pass
    return out


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key, value in getattr(record, "context", {}).items():
            payload[key] = value
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # Quiet noisy libraries
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_with_context(logger: logging.Logger, level: int, message: str, **context) -> None:
    logger.log(level, message, extra={"context": context})
