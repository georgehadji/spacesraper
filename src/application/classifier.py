# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Heuristic Intelligence)
# Role: Automated classification of procurement opportunities.

import re
from typing import Optional

class ProcurementClassifier:
    """
    Spacescraper Semantic Analyst.
    Classifies opportunities into high-level categories based on industry 
    taxonomies and semantic markers.
    """
    
    SPACE_KEYWORDS = [
        r'satellite', r'launcher', r'spacecraft', r'orbital', r'esa', r'nasa',
        r'lunar', r'mars', r'astronom', r'earth observation', r'telescope',
        r'microgravity', r'constellation', r'ground station'
    ]
    
    DEFENSE_KEYWORDS = [
        r'military', r'weapon', r'ammunition', r'nato', r'tactical', r'soldier',
        r'warfare', r'defense', r'defence', r'ballistic', r'missile', r'combat',
        r'c4isr', r'surveillance', r'armored', r'naval', r'air force'
    ]

    def classify(self, title: str, description: Optional[str] = "") -> str:
        """
        Heuristic classification based on keyword density and overlaps.
        """
        text = (title + " " + (description or "")).lower()
        
        is_space = any(re.search(kw, text) for kw in self.SPACE_KEYWORDS)
        is_defense = any(re.search(kw, text) for kw in self.DEFENSE_KEYWORDS)
        
        if is_space and is_defense:
            return "Dual-use"
        elif is_space:
            return "Space"
        elif is_defense:
            return "Defense"
        else:
            return "General Utility"

# Global classifier instance
opportunity_classifier = ProcurementClassifier()
