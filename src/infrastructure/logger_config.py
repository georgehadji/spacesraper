# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Logging Architecture)
# Role: Centralized logging configuration for dual-stream output.

import logging
import os
import sys

from src.infrastructure.middleware.correlation import get_request_id
from src.security.input_sanitizer import sanitize_for_log


class CorrelationFilter(logging.Filter):
    """Injects correlation_id into every log record from the request context."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_request_id() or "-"
        return True


class RedactionFilter(logging.Filter):
    """
    Masks secrets (API keys, Bearer tokens, credential query params, emails,
    DSNs) in every log record before it reaches a handler (SEC-2).
    sanitize_for_log existed but was never installed in production logging —
    only tests called it directly, so nothing masked what actually landed in
    logs/trace.log or the console.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = sanitize_for_log(record.getMessage())
        record.args = ()
        return True

class Colors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'

def setup_production_logging():
    """
    Configures Dual-Stream Logging:
    1. Console: High-level feedback for Operators (Sanitized).
    2. File (trace.log): Detailed stack traces and debug data for Developers.
    """
    os.makedirs("logs", exist_ok=True)
    
    # Root logger configuration
    formatter = logging.Formatter('%(asctime)s - [%(name)s] - %(levelname)s - [corr=%(correlation_id)s] - %(message)s')

    correlation_filter = CorrelationFilter()
    redaction_filter = RedactionFilter()

    # File Handler (Production Debugging - Maximum detail)
    file_handler = logging.FileHandler("logs/trace.log")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(redaction_filter)
    file_handler.addFilter(correlation_filter)

    # Console Handler (Operational Feedback - Clean metrics)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(f'{Colors.OKCYAN}%(asctime)s{Colors.ENDC} [%(name)s] [%(correlation_id)s] %(message)s'))
    console_handler.addFilter(redaction_filter)
    console_handler.addFilter(correlation_filter)
    
    # Global Root Control
    root_logger = logging.getLogger()
    # Remove existing handlers to avoid duplicates
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
        
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    # Suppress noise from third-party libs
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING)
