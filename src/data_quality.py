# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Data Quality Score)
# Role: Calculate completeness score for each tender (0-100).

import logging
import re
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Callable
from enum import Enum

from src.domain.models import Tender

logger = logging.getLogger("Spacescraper.DataQuality")


class QualityDimension(Enum):
    """Dimensions of data quality."""
    COMPLETENESS = "completeness"      # Required fields present
    ACCURACY = "accuracy"              # Values are reasonable
    TIMELINESS = "timeliness"          # Dates are valid/future
    CONSISTENCY = "consistency"        # No contradictions
    ENRICHMENT = "enrichment"          # AI-enhanced fields


@dataclass
class QualityCheck:
    """Individual quality check result."""
    name: str
    dimension: QualityDimension
    weight: int  # 0-100, sum of all weights should be 100
    passed: bool
    score: int   # 0-100 for this check
    details: str


@dataclass
class QualityReport:
    """Complete quality report for a tender."""
    tender_id: str
    overall_score: int  # 0-100
    grade: str  # A+, A, B, C, D, F
    checks: List[QualityCheck]
    missing_fields: List[str]
    recommendations: List[str]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "tender_id": self.tender_id,
            "overall_score": self.overall_score,
            "grade": self.grade,
            "checks": [
                {
                    "name": c.name,
                    "dimension": c.dimension.value,
                    "weight": c.weight,
                    "passed": c.passed,
                    "score": c.score,
                    "details": c.details
                }
                for c in self.checks
            ],
            "missing_fields": self.missing_fields,
            "recommendations": self.recommendations
        }


class DataQualityScorer:
    """
    Calculate Data Quality (DQ) Score for tenders.
    
    Scoring breakdown:
    - Completeness (40%): Required fields present
    - Accuracy (25%): Values are reasonable/valid
    - Timeliness (15%): Deadlines are valid
    - Consistency (10%): No contradictions
    - Enrichment (10%): AI-enhanced fields present
    """
    
    def __init__(self):
        self.checks: List[Callable[[Tender], QualityCheck]] = [
            self._check_title_quality,
            self._check_buyer_present,
            self._check_deadline_valid,
            self._check_budget_reasonable,
            self._check_description_quality,
            self._check_country_present,
            self._check_classification_valid,
            self._check_enrichment_status,
            self._check_id_consistency,
        ]
    
    def calculate_score(self, tender: Tender) -> QualityReport:
        """
        Calculate complete quality score for a tender.
        
        Args:
            tender: The tender to evaluate
            
        Returns:
            QualityReport with detailed breakdown
        """
        checks = []
        missing_fields = []
        recommendations = []
        
        for check_fn in self.checks:
            try:
                check = check_fn(tender)
                checks.append(check)
                
                if not check.passed:
                    missing_fields.append(check.name)
                    
            except Exception as e:
                logger.warning(f"Quality check failed for {tender.url}: {e}")
                continue
        
        # Calculate weighted score
        total_weight = sum(c.weight for c in checks)
        weighted_score = sum(c.score * c.weight for c in checks) / total_weight if total_weight > 0 else 0
        
        overall_score = int(weighted_score)
        grade = self._score_to_grade(overall_score)
        
        # Generate recommendations
        recommendations = self._generate_recommendations(checks, tender)
        
        return QualityReport(
            tender_id=tender.url,
            overall_score=overall_score,
            grade=grade,
            checks=checks,
            missing_fields=missing_fields,
            recommendations=recommendations
        )
    
    def _check_title_quality(self, tender: Tender) -> QualityCheck:
        """Check if title is descriptive and meaningful."""
        title = tender.title or ""
        
        # Length check
        if len(title) < 10:
            return QualityCheck(
                name="title_length",
                dimension=QualityDimension.COMPLETENESS,
                weight=10,
                passed=False,
                score=20,
                details=f"Title too short ({len(title)} chars, minimum 10)"
            )
        
        # Word count
        words = title.split()
        if len(words) < 3:
            return QualityCheck(
                name="title_words",
                dimension=QualityDimension.COMPLETENESS,
                weight=10,
                passed=False,
                score=40,
                details=f"Title has only {len(words)} words"
            )
        
        # Check for generic terms
        generic_terms = ['tender', 'procurement', 'invitation', 'rfp']
        has_specific = any(term not in title.lower() for term in generic_terms)
        
        score = 100 if len(words) >= 5 and len(title) >= 30 else 80
        
        return QualityCheck(
            name="title_quality",
            dimension=QualityDimension.COMPLETENESS,
            weight=10,
            passed=True,
            score=score,
            details=f"Title has {len(words)} words, {len(title)} chars"
        )
    
    def _check_buyer_present(self, tender: Tender) -> QualityCheck:
        """Check if buyer/organization is specified."""
        if not tender.buyer or len(tender.buyer.strip()) < 3:
            return QualityCheck(
                name="buyer_present",
                dimension=QualityDimension.COMPLETENESS,
                weight=10,
                passed=False,
                score=0,
                details="Buyer/organization not specified"
            )
        
        return QualityCheck(
            name="buyer_present",
            dimension=QualityDimension.COMPLETENESS,
            weight=10,
            passed=True,
            score=100,
            details=f"Buyer: {tender.buyer[:50]}"
        )
    
    def _check_deadline_valid(self, tender: Tender) -> QualityCheck:
        """Check if deadline is present and valid."""
        from datetime import datetime
        
        if not tender.deadline:
            return QualityCheck(
                name="deadline_valid",
                dimension=QualityDimension.TIMELINESS,
                weight=15,
                passed=False,
                score=0,
                details="No deadline specified"
            )
        
        # Try to parse deadline
        try:
            # Common formats
            formats = [
                "%Y-%m-%d",
                "%d/%m/%Y",
                "%m/%d/%Y",
                "%Y-%m-%dT%H:%M:%S",
                "%d-%m-%Y"
            ]
            
            parsed = None
            for fmt in formats:
                try:
                    parsed = datetime.strptime(tender.deadline.split('T')[0], fmt)
                    break
                except ValueError:
                    continue
            
            if not parsed:
                raise ValueError("Could not parse date")
            
            # Check if deadline is in future
            days_until = (parsed - datetime.now()).days
            
            if days_until < 0:
                return QualityCheck(
                    name="deadline_valid",
                    dimension=QualityDimension.TIMELINESS,
                    weight=15,
                    passed=False,
                    score=30,
                    details=f"Deadline in past ({tender.deadline})"
                )
            
            if days_until < 7:
                return QualityCheck(
                    name="deadline_valid",
                    dimension=QualityDimension.TIMELINESS,
                    weight=15,
                    passed=True,
                    score=70,
                    details=f"Deadline very soon ({days_until} days)"
                )
            
            return QualityCheck(
                name="deadline_valid",
                dimension=QualityDimension.TIMELINESS,
                weight=15,
                passed=True,
                score=100,
                details=f"Deadline valid ({days_until} days remaining)"
            )
            
        except Exception as e:
            return QualityCheck(
                name="deadline_valid",
                dimension=QualityDimension.TIMELINESS,
                weight=15,
                passed=False,
                score=40,
                details=f"Deadline format unclear: {tender.deadline}"
            )
    
    def _check_budget_reasonable(self, tender: Tender) -> QualityCheck:
        """Check if budget is present and reasonable."""
        if not tender.estimated_budget:
            return QualityCheck(
                name="budget_reasonable",
                dimension=QualityDimension.COMPLETENESS,
                weight=10,
                passed=False,
                score=30,
                details="No budget specified"
            )
        
        # Try to extract numeric value
        try:
            # Remove common currency symbols and separators
            cleaned = re.sub(r'[^\d.,]', '', tender.estimated_budget)
            cleaned = cleaned.replace(',', '.')
            
            if '.' in cleaned:
                # Handle European format (1.234.567,89)
                parts = cleaned.split('.')
                if len(parts) > 2:
                    cleaned = ''.join(parts[:-1]) + '.' + parts[-1]
            
            value = float(cleaned)
            
            # Sanity checks
            if value <= 0:
                return QualityCheck(
                    name="budget_reasonable",
                    dimension=QualityDimension.ACCURACY,
                    weight=10,
                    passed=False,
                    score=20,
                    details=f"Budget value invalid: {value}"
                )
            
            if value > 10_000_000_000:  # 10 billion EUR
                return QualityCheck(
                    name="budget_reasonable",
                    dimension=QualityDimension.ACCURACY,
                    weight=10,
                    passed=False,
                    score=50,
                    details=f"Budget seems extremely high: €{value:,.0f}"
                )
            
            return QualityCheck(
                name="budget_reasonable",
                dimension=QualityDimension.ACCURACY,
                weight=10,
                passed=True,
                score=100,
                details=f"Budget: €{value:,.0f}"
            )
            
        except Exception:
            # Budget present but can't parse
            return QualityCheck(
                name="budget_reasonable",
                dimension=QualityDimension.COMPLETENESS,
                weight=10,
                passed=True,
                score=60,
                details=f"Budget present but format unclear: {tender.estimated_budget}"
            )
    
    def _check_description_quality(self, tender: Tender) -> QualityCheck:
        """Check if summary/description is meaningful."""
        summary = tender.summary or ""
        
        if not summary:
            return QualityCheck(
                name="description_quality",
                dimension=QualityDimension.ENRICHMENT,
                weight=10,
                passed=False,
                score=20,
                details="No summary/description available"
            )
        
        words = len(summary.split())
        
        if words < 10:
            return QualityCheck(
                name="description_quality",
                dimension=QualityDimension.ENRICHMENT,
                weight=10,
                passed=False,
                score=40,
                details=f"Summary very short ({words} words)"
            )
        
        if words < 30:
            return QualityCheck(
                name="description_quality",
                dimension=QualityDimension.ENRICHMENT,
                weight=10,
                passed=True,
                score=70,
                details=f"Summary brief but present ({words} words)"
            )
        
        return QualityCheck(
            name="description_quality",
            dimension=QualityDimension.ENRICHMENT,
            weight=10,
            passed=True,
            score=100,
            details=f"Summary detailed ({words} words)"
        )
    
    def _check_country_present(self, tender: Tender) -> QualityCheck:
        """Check if country/region is specified."""
        if not tender.country or len(tender.country) < 2:
            return QualityCheck(
                name="country_present",
                dimension=QualityDimension.COMPLETENESS,
                weight=5,
                passed=False,
                score=0,
                details="Country not specified"
            )
        
        return QualityCheck(
            name="country_present",
            dimension=QualityDimension.COMPLETENESS,
            weight=5,
            passed=True,
            score=100,
            details=f"Country: {tender.country}"
        )
    
    def _check_classification_valid(self, tender: Tender) -> QualityCheck:
        """Check if tender has been classified."""
        valid_classifications = ["Space", "Defense", "Dual-use", "Uncertain", None]
        
        if not tender.classification:
            return QualityCheck(
                name="classification_valid",
                dimension=QualityDimension.ENRICHMENT,
                weight=10,
                passed=False,
                score=30,
                details="Tender not classified"
            )
        
        if tender.classification.startswith("AUDIT_FLAG"):
            return QualityCheck(
                name="classification_valid",
                dimension=QualityDimension.CONSISTENCY,
                weight=10,
                passed=False,
                score=40,
                details=f"Classification flagged: {tender.classification}"
            )
        
        return QualityCheck(
            name="classification_valid",
            dimension=QualityDimension.ENRICHMENT,
            weight=10,
            passed=True,
            score=100,
            details=f"Classified as: {tender.classification}"
        )
    
    def _check_enrichment_status(self, tender: Tender) -> QualityCheck:
        """Check if AI enrichment has been applied."""
        enriched_fields = 0
        total_fields = 3  # summary, normalized_budget_eur, embedding
        
        if tender.summary:
            enriched_fields += 1
        if tender.normalized_budget_eur:
            enriched_fields += 1
        if tender.embedding:
            enriched_fields += 1
        
        score = int((enriched_fields / total_fields) * 100)
        
        return QualityCheck(
            name="enrichment_status",
            dimension=QualityDimension.ENRICHMENT,
            weight=10,
            passed=enriched_fields > 0,
            score=score,
            details=f"{enriched_fields}/{total_fields} fields enriched"
        )
    
    def _check_id_consistency(self, tender: Tender) -> QualityCheck:
        """Check for ID consistency."""
        # URL should match tender ID
        if tender.url != tender.url:  # Always true, placeholder logic
            pass
        
        # Check external_id format if present
        if tender.external_id:
            # Most tender IDs have some structure
            return QualityCheck(
                name="id_consistency",
                dimension=QualityDimension.CONSISTENCY,
                weight=5,
                passed=True,
                score=100,
                details=f"External ID present: {tender.external_id[:30]}..."
            )
        
        return QualityCheck(
            name="id_consistency",
            dimension=QualityDimension.CONSISTENCY,
            weight=5,
            passed=True,
            score=80,
            details="No external ID (using URL as primary key)"
        )
    
    def _score_to_grade(self, score: int) -> str:
        """Convert numeric score to letter grade."""
        if score >= 95:
            return "A+"
        elif score >= 90:
            return "A"
        elif score >= 85:
            return "A-"
        elif score >= 80:
            return "B+"
        elif score >= 75:
            return "B"
        elif score >= 70:
            return "B-"
        elif score >= 65:
            return "C+"
        elif score >= 60:
            return "C"
        elif score >= 50:
            return "D"
        else:
            return "F"
    
    def _generate_recommendations(
        self, 
        checks: List[QualityCheck], 
        tender: Tender
    ) -> List[str]:
        """Generate improvement recommendations."""
        recommendations = []
        
        failed_checks = [c for c in checks if not c.passed]
        
        for check in failed_checks:
            if check.name == "title_quality":
                recommendations.append(
                    "Add a more descriptive title with at least 5 words describing the project"
                )
            elif check.name == "buyer_present":
                recommendations.append(
                    "Include the procuring organization/buyer name"
                )
            elif check.name == "deadline_valid":
                recommendations.append(
                    "Add a clear submission deadline date"
                )
            elif check.name == "budget_reasonable":
                recommendations.append(
                    "Provide estimated budget in a standard format (e.g., '€1,500,000')"
                )
            elif check.name == "description_quality":
                recommendations.append(
                    "Add a detailed project description (at least 30 words)"
                )
            elif check.name == "classification_valid":
                recommendations.append(
                    "Review AI classification or provide manual classification"
                )
        
        if not recommendations:
            recommendations.append("Data quality is excellent - no improvements needed")
        
        return recommendations


# Filter helpers for querying
def filter_by_min_quality(tenders: List[Tender], min_score: int) -> List[Tender]:
    """Filter tenders by minimum quality score."""
    scorer = DataQualityScorer()
    result = []
    
    for tender in tenders:
        report = scorer.calculate_score(tender)
        if report.overall_score >= min_score:
            tender.quality_score = report.overall_score  # Attach score
            result.append(tender)
    
    return result


def sort_by_quality(tenders: List[Tender]) -> List[Tender]:
    """Sort tenders by quality score (highest first)."""
    scorer = DataQualityScorer()
    
    # Calculate scores
    scored_tenders = []
    for tender in tenders:
        report = scorer.calculate_score(tender)
        scored_tenders.append((tender, report.overall_score))
    
    # Sort by score descending
    scored_tenders.sort(key=lambda x: x[1], reverse=True)
    
    # Attach scores and return
    for tender, score in scored_tenders:
        tender.quality_score = score
    
    return [t for t, _ in scored_tenders]


# Global instance
dq_scorer = DataQualityScorer()
