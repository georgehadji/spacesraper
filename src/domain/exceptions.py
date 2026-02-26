# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Domain Exceptions)
# Role: Centralized error hierarchy for structured feedback and logging.

class SpacescraperError(Exception):
    """Base exception for all domain-specific errors."""
    def __init__(self, message: str, code: str = "INTERNAL_ERROR", details: dict = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}

class ScrapeFailure(SpacescraperError):
    """Raised when the browser engine fails to capture a payload."""
    pass

class ExtractionError(SpacescraperError):
    """Raised when parsing heuristics or overlays fail to resolve entities."""
    pass

class StorageError(SpacescraperError):
    """Raised when the persistence layer (SQLite) or Queue is unreachable."""
    pass

class StealthViolation(SpacescraperError):
    """Raised when anti-bot challenges are detected and cannot be bypassed."""
    pass

class SSRFGuardError(SpacescraperError):
    """Raised when an outbound URL targets a private or reserved address."""
    pass

class InputValidationError(SpacescraperError):
    """Raised when user input fails size or content validation."""
    pass
