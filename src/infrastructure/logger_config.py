# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Logging Architecture)
# Role: Centralized logging configuration for dual-stream output.

import logging
import sys
import os

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
    formatter = logging.Formatter('%(asctime)s - [%(name)s] - %(levelname)s - %(message)s')
    
    # File Handler (Production Debugging - Maximum detail)
    file_handler = logging.FileHandler("logs/trace.log")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    
    # Console Handler (Operational Feedback - Clean metrics)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(f'{Colors.OKCYAN}%(asctime)s{Colors.ENDC} [%(name)s] %(message)s'))
    
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
