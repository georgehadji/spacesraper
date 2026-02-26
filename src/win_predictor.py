# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Win Probability Engine)
# Role: Predict tender win probability based on user capabilities.

import logging
import re
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
from collections import Counter
import math

from src.domain.models import Tender

logger = logging.getLogger("Spacescraper.WinPredictor")


@dataclass
class UserCapabilityProfile:
    """
    User's company capabilities and preferences.
    This creates the data moat - accumulated over time.
    """
    user_id: str
    organization: str
    
    # Core capabilities
    keywords: List[str] = field(default_factory=list)  # ["satellite", "optical", "AI"]
    industries: List[str] = field(default_factory=list)  # ["defense", "space"]
    services: List[str] = field(default_factory=list)  # ["consulting", "manufacturing"]
    
    # Financial constraints
    min_budget_eur: Optional[float] = None
    max_budget_eur: Optional[float] = None
    preferred_currencies: List[str] = field(default_factory=lambda: ["EUR", "USD"])
    
    # Geographic preferences
    geographic_focus: List[str] = field(default_factory=list)  # ["EU", "NATO"]
    excluded_countries: List[str] = field(default_factory=list)
    
    # Historical performance (the secret sauce)
    past_wins: List[Dict[str, Any]] = field(default_factory=list)
    past_bids: List[Dict[str, Any]] = field(default_factory=list)
    win_rate_by_buyer: Dict[str, float] = field(default_factory=dict)
    
    # Preferences
    min_quality_score: int = 70  # Only show tenders with DQ >= 70
    max_deadline_days: Optional[int] = None  # Must submit within X days
    
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "organization": self.organization,
            "keywords": self.keywords,
            "industries": self.industries,
            "services": self.services,
            "budget_range": {
                "min": self.min_budget_eur,
                "max": self.max_budget_eur
            },
            "geographic_focus": self.geographic_focus,
            "past_wins_count": len(self.past_wins),
            "win_rate": self.calculate_overall_win_rate()
        }
    
    def calculate_overall_win_rate(self) -> float:
        """Calculate overall win rate from history."""
        total = len(self.past_wins) + len(self.past_bids)
        if total == 0:
            return 0.0
        return len(self.past_wins) / total


@dataclass
class TenderMatch:
    """A tender with its match score and explanation."""
    tender: Tender
    match_score: float  # 0.0 - 1.0
    win_probability: float  # 0.0 - 1.0
    confidence: str  # "high", "medium", "low"
    
    # Breakdown
    keyword_match_score: float
    budget_match_score: float
    geographic_match_score: float
    historical_performance_score: float
    quality_score: float
    
    # Human-readable explanation
    why: str
    recommendations: List[str]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "tender": {
                "title": self.tender.title,
                "buyer": self.tender.buyer,
                "budget": self.tender.estimated_budget,
                "deadline": self.tender.deadline,
                "url": self.tender.url
            },
            "scores": {
                "overall_match": round(self.match_score, 3),
                "win_probability": round(self.win_probability, 3),
                "confidence": self.confidence
            },
            "breakdown": {
                "keywords": round(self.keyword_match_score, 3),
                "budget": round(self.budget_match_score, 3),
                "geographic": round(self.geographic_match_score, 3),
                "historical": round(self.historical_performance_score, 3),
                "quality": round(self.quality_score, 3)
            },
            "why": self.why,
            "recommendations": self.recommendations
        }


class TenderMatcher:
    """
    Matches tenders to user capabilities using multiple signals.
    
    Algorithm:
    1. Keyword matching (TF-IDF style)
    2. Budget compatibility
    3. Geographic alignment
    4. Historical win patterns
    5. Data quality boost
    """
    
    def __init__(self):
        self.weights = {
            "keywords": 0.30,
            "budget": 0.25,
            "geographic": 0.15,
            "historical": 0.20,
            "quality": 0.10
        }
    
    def calculate_match(
        self, 
        tender: Tender, 
        profile: UserCapabilityProfile
    ) -> TenderMatch:
        """
        Calculate comprehensive match between tender and user profile.
        """
        # Individual scores
        kw_score = self._score_keywords(tender, profile)
        budget_score = self._score_budget(tender, profile)
        geo_score = self._score_geographic(tender, profile)
        hist_score = self._score_historical(tender, profile)
        qual_score = self._score_quality(tender)
        
        # Weighted match score
        match_score = (
            kw_score * self.weights["keywords"] +
            budget_score * self.weights["budget"] +
            geo_score * self.weights["geographic"] +
            hist_score * self.weights["historical"] +
            qual_score * self.weights["quality"]
        )
        
        # Win probability adjusts based on confidence
        confidence = self._determine_confidence(profile, match_score)
        win_prob = self._calculate_win_probability(
            match_score, hist_score, profile, confidence
        )
        
        # Generate explanation
        why = self._generate_why(match_score, kw_score, budget_score, geo_score, hist_score)
        recommendations = self._generate_recommendations(tender, profile, kw_score, budget_score)
        
        return TenderMatch(
            tender=tender,
            match_score=match_score,
            win_probability=win_prob,
            confidence=confidence,
            keyword_match_score=kw_score,
            budget_match_score=budget_score,
            geographic_match_score=geo_score,
            historical_performance_score=hist_score,
            quality_score=qual_score,
            why=why,
            recommendations=recommendations
        )
    
    def _score_keywords(self, tender: Tender, profile: UserCapabilityProfile) -> float:
        """
        Score based on keyword overlap.
        Uses TF-IDF inspired weighting.
        """
        if not profile.keywords:
            return 0.5  # Neutral if no keywords defined
        
        tender_text = f"{tender.title} {tender.summary or ''} {tender.buyer or ''}".lower()
        tender_words = set(re.findall(r'\b\w+\b', tender_text))
        
        matches = 0
        total_weight = 0
        
        for keyword in profile.keywords:
            keyword_lower = keyword.lower()
            weight = 1.0
            
            # Exact match in title (higher weight)
            if keyword_lower in tender.title.lower():
                weight = 2.0
                matches += weight
            # Partial match
            elif any(keyword_lower in word for word in tender_words):
                matches += weight * 0.5
            
            total_weight += 2.0  # Max possible weight
        
        score = matches / total_weight if total_weight > 0 else 0
        return min(score, 1.0)
    
    def _score_budget(self, tender: Tender, profile: UserCapabilityProfile) -> float:
        """
        Score based on budget compatibility.
        """
        if not tender.normalized_budget_eur:
            return 0.6  # Slightly positive if budget unknown
        
        budget = tender.normalized_budget_eur
        
        # Check if within range
        if profile.min_budget_eur and budget < profile.min_budget_eur:
            return 0.2  # Too small
        
        if profile.max_budget_eur and budget > profile.max_budget_eur:
            return 0.1  # Too large
        
        # Sweet spot: middle of range
        if profile.min_budget_eur and profile.max_budget_eur:
            mid = (profile.min_budget_eur + profile.max_budget_eur) / 2
            diff = abs(budget - mid) / mid
            return max(0.8, 1.0 - diff * 0.4)
        
        return 0.8  # In range, good
    
    def _score_geographic(self, tender: Tender, profile: UserCapabilityProfile) -> float:
        """
        Score based on geographic alignment.
        """
        if not tender.country:
            return 0.5
        
        # Check excluded countries first
        if tender.country in profile.excluded_countries:
            return 0.0
        
        # Direct country match
        if profile.geographic_focus and tender.country in profile.geographic_focus:
            return 1.0
        
        # Region matching (simplified)
        eu_countries = {"DE", "FR", "IT", "ES", "NL", "BE", "AT", "PL", "SE", "FI"}
        nato_countries = {"US", "CA", "GB", "DE", "FR", "IT", "ES", "NL", "BE", "PL"}
        
        if "EU" in profile.geographic_focus and tender.country in eu_countries:
            return 0.9
        
        if "NATO" in profile.geographic_focus and tender.country in nato_countries:
            return 0.85
        
        # Neutral if no specific preference
        if not profile.geographic_focus:
            return 0.7
        
        return 0.3  # Outside preferred areas
    
    def _score_historical(self, tender: Tender, profile: UserCapabilityProfile) -> float:
        """
        Score based on historical win patterns.
        This is the "secret sauce" - improves over time.
        """
        if not profile.past_wins:
            return 0.5  # Neutral for new users
        
        scores = []
        
        # Win rate with this buyer
        if tender.buyer and tender.buyer in profile.win_rate_by_buyer:
            scores.append(profile.win_rate_by_buyer[tender.buyer])
        
        # Similar tender wins
        for win in profile.past_wins[-10:]:  # Last 10 wins
            similarity = self._calculate_similarity(tender, win)
            scores.append(similarity)
        
        if not scores:
            return 0.5
        
        # Weight recent wins higher
        return sum(scores) / len(scores)
    
    def _calculate_similarity(self, tender: Tender, past_win: Dict) -> float:
        """Calculate similarity between tender and past win."""
        score = 0.0
        
        # Same buyer
        if tender.buyer == past_win.get("buyer"):
            score += 0.4
        
        # Similar budget (within 50%)
        if tender.normalized_budget_eur and past_win.get("budget"):
            ratio = min(tender.normalized_budget_eur, past_win["budget"]) / max(tender.normalized_budget_eur, past_win["budget"])
            if ratio > 0.5:
                score += 0.3 * ratio
        
        # Keyword overlap
        tender_words = set(tender.title.lower().split())
        past_words = set(past_win.get("title", "").lower().split())
        overlap = len(tender_words & past_words) / len(tender_words | past_words) if tender_words or past_words else 0
        score += 0.3 * overlap
        
        return min(score, 1.0)
    
    def _score_quality(self, tender: Tender) -> float:
        """Boost score for high-quality tender data."""
        from src.data_quality import dq_scorer
        
        try:
            report = dq_scorer.calculate_score(tender)
            return report.overall_score / 100
        except Exception:
            return 0.5
    
    def _determine_confidence(self, profile: UserCapabilityProfile, match_score: float) -> str:
        """Determine confidence level based on data quality."""
        data_points = (
            len(profile.keywords) +
            len(profile.past_wins) +
            (1 if profile.min_budget_eur else 0) +
            (1 if profile.max_budget_eur else 0)
        )
        
        if data_points >= 10 and match_score > 0.7:
            return "high"
        elif data_points >= 5 and match_score > 0.5:
            return "medium"
        else:
            return "low"
    
    def _calculate_win_probability(
        self, 
        match_score: float, 
        hist_score: float,
        profile: UserCapabilityProfile,
        confidence: str
    ) -> float:
        """
        Calculate final win probability.
        
        Formula: Base match + historical boost + confidence adjustment
        """
        base = match_score
        
        # Historical performance boost
        if hist_score > 0.5:
            base += 0.1
        
        # Overall win rate factor
        win_rate = profile.calculate_overall_win_rate()
        if win_rate > 0:
            base = (base + win_rate) / 2
        
        # Confidence adjustment
        confidence_mult = {"high": 1.0, "medium": 0.85, "low": 0.7}
        base *= confidence_mult.get(confidence, 0.7)
        
        return min(base, 0.99)  # Never 100%
    
    def _generate_why(
        self, 
        match: float, 
        kw: float, 
        budget: float, 
        geo: float, 
        hist: float
    ) -> str:
        """Generate human-readable explanation."""
        reasons = []
        
        if kw > 0.7:
            reasons.append("Strong keyword match")
        elif kw > 0.4:
            reasons.append("Good keyword overlap")
        
        if budget > 0.8:
            reasons.append("Budget in sweet spot")
        elif budget > 0.5:
            reasons.append("Budget acceptable")
        
        if geo > 0.8:
            reasons.append("Preferred geography")
        
        if hist > 0.7:
            reasons.append("Strong historical performance")
        
        if not reasons:
            return "Moderate match across dimensions"
        
        return ", ".join(reasons)
    
    def _generate_recommendations(
        self, 
        tender: Tender, 
        profile: UserCapabilityProfile,
        kw_score: float,
        budget_score: float
    ) -> List[str]:
        """Generate actionable recommendations."""
        recs = []
        
        if kw_score < 0.5:
            recs.append("Highlight relevant experience in your proposal")
        
        if not tender.normalized_budget_eur:
            recs.append("Request budget clarification from buyer")
        
        if budget_score < 0.5 and tender.normalized_budget_eur:
            if profile.max_budget_eur and tender.normalized_budget_eur > profile.max_budget_eur:
                recs.append("Consider consortium partnership for large budget")
        
        if tender.deadline:
            recs.append(f"Submit before deadline: {tender.deadline}")
        
        if not recs:
            recs.append("Strong match - prioritize this tender")
        
        return recs


class WinPredictor:
    """
    Main interface for win prediction.
    """
    
    def __init__(self):
        self.matcher = TenderMatcher()
    
    def find_matches(
        self,
        tenders: List[Tender],
        profile: UserCapabilityProfile,
        min_score: float = 0.5,
        top_k: int = 10
    ) -> List[TenderMatch]:
        """
        Find best matching tenders for a user profile.
        
        Args:
            tenders: List of tenders to evaluate
            profile: User's capability profile
            min_score: Minimum match score (0.0-1.0)
            top_k: Return top K matches
        
        Returns:
            List of TenderMatch, sorted by win probability
        """
        matches = []
        
        for tender in tenders:
            match = self.matcher.calculate_match(tender, profile)
            
            if match.match_score >= min_score:
                matches.append(match)
        
        # Sort by win probability (descending)
        matches.sort(key=lambda m: m.win_probability, reverse=True)
        
        return matches[:top_k]
    
    def update_profile_from_outcome(
        self,
        profile: UserCapabilityProfile,
        tender: Tender,
        bid_submitted: bool,
        won: bool
    ):
        """
        Update user profile based on bid outcome.
        This is the learning loop that improves predictions over time.
        """
        profile.updated_at = datetime.utcnow()
        
        bid_record = {
            "tender_url": tender.url,
            "title": tender.title,
            "buyer": tender.buyer,
            "budget": tender.normalized_budget_eur,
            "bid_submitted": bid_submitted,
            "won": won,
            "timestamp": datetime.utcnow().isoformat()
        }
        
        profile.past_bids.append(bid_record)
        
        if won:
            profile.past_wins.append(bid_record)
            
            # Update win rate by buyer
            if tender.buyer:
                buyer_bids = [b for b in profile.past_bids if b.get("buyer") == tender.buyer]
                buyer_wins = [b for b in buyer_bids if b.get("won")]
                profile.win_rate_by_buyer[tender.buyer] = len(buyer_wins) / len(buyer_bids)
        
        logger.info(f"Updated profile {profile.user_id} with outcome: won={won}")


# Global instance
win_predictor = WinPredictor()
