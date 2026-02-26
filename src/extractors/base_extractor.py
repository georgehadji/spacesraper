# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Extraction Framework)
# Role: Abstract base class for all site-specific and generic extraction strategies.

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from src.domain.models import BaseEntity

class BaseExtractionStrategy(ABC):
    """
    Spacescraper Strategy Interface.
    This abstract class defines the contract for all data extraction modules. 
    By decoupled the parsing logic from the crawler, Spacescraper can 
    easily adapt to different website architectures (Server-Side Rendered, 
    Single Page Apps, or JSON APIs) without modifying the core pipeline.
    """
    
    @abstractmethod
    async def extract(
        self, 
        html_content: str, 
        json_payloads: List[Dict[str, Any]],
        current_url: str = "",
        overlay: Optional[Dict[str, Any]] = None
    ) -> List[BaseEntity]:
        """
        Spacescraper Parsing Contract.
        Must be implemented by all derived strategies.
        
        Args:
            html_content: The raw source code (DOM) of the webpage.
            json_payloads: Any intercepted network traffic (XHR/Fetch) captured during the session.
            current_url: The canonical URL being processed, for data lineage.
            
        Returns:
            A list of domain entities (Products, Tenders, Leads, etc.) 
            ready for enrichment and persistence.
        """
        pass
