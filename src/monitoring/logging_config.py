import os
import json
import re
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from src.config import config

SENSITIVE_KEYS = {"password", "secret", "api_key", "token", "authorization", "access_token", "connection_string"}

def sanitize_log_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    """Sanitize structured logging payload to prevent PII, secrets, and raw contract leaks."""
    clean = {}
    for k, v in data.items():
        lower_k = str(k).lower()
        if any(sk in lower_k for sk in SENSITIVE_KEYS):
            clean[k] = "[REDACTED_SECRET]"
        elif lower_k in {"contract_text", "raw_text", "document_text", "full_text"} and not config.enable_sensitive_data:
            clean[k] = f"[REDACTED_DOCUMENT_TEXT ({len(str(v))} chars)]"
        elif isinstance(v, dict):
            clean[k] = sanitize_log_payload(v)
        elif isinstance(v, list):
            clean[k] = [sanitize_log_payload(item) if isinstance(item, dict) else item for item in v]
        else:
            clean[k] = v
    return clean

def get_default_log_path() -> str:
    if os.getenv("VERCEL") or not os.access(".", os.W_OK):
        return "/tmp/application.jsonl"
    return "logs/application.jsonl"

class JSONLinesHandler(logging.Handler):
    """Custom logging handler that writes structured JSON lines to a file."""
    def __init__(self, file_path: Optional[str] = None):
        super().__init__()
        self.file_path = file_path or get_default_log_path()
        try:
            dirname = os.path.dirname(self.file_path)
            if dirname:
                os.makedirs(dirname, exist_ok=True)
        except Exception:
            pass

    def emit(self, record: logging.LogRecord):
        try:
            log_entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage()
            }
            if hasattr(record, "structured_data"):
                sanitized = sanitize_log_payload(record.structured_data)
                log_entry.update(sanitized)
            
            with open(self.file_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry) + "\n")
        except Exception:
            self.handleError(record)

def get_logger(name: str = "contract_compliance") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        # Console handler
        console_handler = logging.StreamHandler()
        console_formatter = logging.Formatter("[%(levelname)s] %(asctime)s - %(name)s - %(message)s")
        console_handler.setFormatter(console_formatter)
        logger.addHandler(console_handler)
        # JSON lines file handler
        json_handler = JSONLinesHandler()
        logger.addHandler(json_handler)
    return logger

def log_event(
    logger: logging.Logger,
    level: str,
    stage: str,
    event: str,
    run_id: str,
    agent: Optional[str] = None,
    duration_ms: Optional[float] = None,
    status: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None
):
    """Convenience helper to emit compliant structured log events with execution IDs."""
    data = {
        "run_id": run_id,
        "stage": stage,
        "event": event,
        "agent": agent,
        "duration_ms": duration_ms,
        "status": status,
        "error_type": error_type,
        "error_message": error_message
    }
    if extra:
        data.update(extra)
    
    # Filter out None values
    clean_data = {k: v for k, v in data.items() if v is not None}
    
    log_func = getattr(logger, level.lower(), logger.info)
    log_func(
        f"[{stage}] {event} (run_id={run_id}, status={status or 'N/A'})",
        extra={"structured_data": clean_data}
    )
