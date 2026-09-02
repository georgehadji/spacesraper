# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Export System)
# Role: Base interface for data delivery plugins.

from abc import ABC, abstractmethod

from src.domain.models import ExtractedRecord


class BaseExportPlugin(ABC):
    """Protocol for delivering discovered intelligence to external channels."""

    @abstractmethod
    async def deliver(self, records: list[ExtractedRecord]):
        pass
