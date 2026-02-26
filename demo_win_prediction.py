#!/usr/bin/env python3
"""
Spacescraper Win Prediction Engine - Demo

Demonstrates the Nash-stable feature: capability-based tender matching.
"""

import asyncio
from datetime import datetime

from src.win_predictor import (
    UserCapabilityProfile, win_predictor, TenderMatcher
)
from src.domain.models import Tender


def print_header(text):
    print("\n" + "=" * 70)
    print(f"  {text}")
    print("=" * 70)


def print_match(match, index):
    """Pretty print a tender match."""
    emoji = "🟢" if match.win_probability >= 0.7 else "🟡" if match.win_probability >= 0.5 else "🔴"
    
    print(f"\n{emoji} Match #{index + 1}: {match.tender.title[:50]}...")
    print(f"   Buyer: {match.tender.buyer or 'Unknown'}")
    print(f"   Budget: {match.tender.estimated_budget or 'Not specified'}")
    print(f"   ")
    print(f"   📊 Win Probability: {match.win_probability:.1%} (confidence: {match.confidence})")
    print(f"   📈 Match Score: {match.match_score:.2f}")
    print(f"   ")
    print(f"   Breakdown:")
    print(f"      Keywords:   {match.keyword_match_score:.2f}")
    print(f"      Budget:     {match.budget_match_score:.2f}")
    print(f"      Geography:  {match.geographic_match_score:.2f}")
    print(f"      Historical: {match.historical_performance_score:.2f}")
    print(f"   ")
    print(f"   💡 Why: {match.why}")
    print(f"   📝 Recommendations:")
    for rec in match.recommendations[:2]:
        print(f"      • {rec}")


async def demo_basic_matching():
    """Demo basic tender matching."""
    print_header("🎯 DEMO: Basic Tender Matching")
    
    # Create user profile
    profile = UserCapabilityProfile(
        user_id="user_001",
        organization="SpaceTech Solutions GmbH",
        keywords=["satellite", "communication", "ground station", "RF", "antenna"],
        industries=["space", "defense", "telecommunications"],
        services=["manufacturing", "system integration", "consulting"],
        min_budget_eur=500_000,
        max_budget_eur=15_000_000,
        geographic_focus=["EU", "NATO", "DE", "FR"],
        excluded_countries=["CN", "RU"],
        min_quality_score=70
    )
    
    print(f"\n👤 User Profile:")
    print(f"   Organization: {profile.organization}")
    print(f"   Keywords: {', '.join(profile.keywords)}")
    print(f"   Budget Range: €{profile.min_budget_eur/1e6:.1f}M - €{profile.max_budget_eur/1e6:.1f}M")
    print(f"   Geography: {', '.join(profile.geographic_focus)}")
    
    # Create sample tenders
    tenders = [
        Tender(
            source="TED",
            title="Supply of Advanced Satellite Communication Terminals for Defense Applications",
            buyer="European Defence Agency",
            country="BE",
            deadline="2024-06-15",
            estimated_budget="€2,500,000",
            normalized_budget_eur=2_500_000,
            summary="Procurement of mobile satellite communication terminals with encryption for military field operations.",
            url="https://ted.europa.eu/notice/001",
            classification="Defense"
        ),
        Tender(
            source="ESA",
            title="Earth Observation Satellite Constellation - Ground Segment",
            buyer="European Space Agency",
            country="FR",
            deadline="2024-08-20",
            estimated_budget="€12,000,000",
            normalized_budget_eur=12_000_000,
            summary="Design and deployment of ground station network for new EO constellation. Includes antennas, RF equipment, and data processing.",
            url="https://esa.int/tenders/002",
            classification="Space"
        ),
        Tender(
            source="TED",
            title="Office Furniture and Stationery Supply",
            buyer="Local Municipality Office",
            country="ES",
            deadline="2024-05-01",
            estimated_budget="€25,000",
            normalized_budget_eur=25_000,
            summary="Annual supply of office chairs, desks, and stationery items.",
            url="https://ted.europa.eu/notice/003",
            classification=None
        ),
        Tender(
            source="NATO",
            title="Secure Military Satellite Communications Upgrade",
            buyer="NATO Communications Agency",
            country="NL",
            deadline="2024-07-30",
            estimated_budget="€45,000,000",
            normalized_budget_eur=45_000_000,
            summary="Next-generation secure satellite communication system for NATO operations. High-frequency bands, anti-jamming.",
            url="https://nato.int/procurement/004",
            classification="Defense"
        ),
        Tender(
            source="TED",
            title="Smart City IoT Sensors Deployment",
            buyer="Barcelona City Council",
            country="ES",
            deadline="2024-09-15",
            estimated_budget="€800,000",
            normalized_budget_eur=800_000,
            summary="Deployment of IoT sensors for traffic and environmental monitoring.",
            url="https://ted.europa.eu/notice/005",
            classification=None
        ),
    ]
    
    print(f"\n📋 Evaluating {len(tenders)} tenders...\n")
    
    # Find matches
    matches = win_predictor.find_matches(
        tenders=tenders,
        profile=profile,
        min_score=0.3,
        top_k=5
    )
    
    print(f"✅ Found {len(matches)} matching tenders:\n")
    
    for i, match in enumerate(matches):
        print_match(match, i)
    
    return profile, matches


async def demo_learning_loop():
    """Demo how the system learns from outcomes."""
    print_header("🧠 DEMO: Learning Loop (The Data Moat)")
    
    # Start with empty profile
    profile = UserCapabilityProfile(
        user_id="user_002",
        organization="New Space Company",
        keywords=["satellite", "optical"],
        past_wins=[],
        past_bids=[]
    )
    
    print(f"\n📊 Initial State:")
    print(f"   Past Wins: {len(profile.past_wins)}")
    print(f"   Past Bids: {len(profile.past_bids)}")
    print(f"   Win Rate: {profile.calculate_overall_win_rate():.1%}")
    print(f"   Confidence: LOW (not enough data)")
    
    # Simulate bidding on several tenders
    outcomes = [
        ("ESA Earth Observation Contract", "ESA", 5_000_000, True, True),    # Won
        ("Defense Communication System", "EDA", 2_000_000, True, True),      # Won
        ("Small Satellite Platform", "NASA", 8_000_000, True, False),        # Lost
        ("Ground Station Equipment", "ESA", 1_500_000, True, True),          # Won
        ("Military Radar System", "NATO", 15_000_000, False, False),         # No bid
    ]
    
    print(f"\n📝 Simulating {len(outcomes)} bid outcomes...\n")
    
    for title, buyer, budget, bid, won in outcomes:
        tender = Tender(
            source="DEMO",
            title=title,
            buyer=buyer,
            estimated_budget=f"€{budget:,}",
            normalized_budget_eur=budget,
            url=f"https://demo/{title.replace(' ', '-')}"
        )
        
        win_predictor.update_profile_from_outcome(
            profile=profile,
            tender=tender,
            bid_submitted=bid,
            won=won
        )
        
        status = "✅ WON" if won else "❌ LOST" if bid else "🚫 NO BID"
        print(f"   {title[:40]}... {status}")
    
    print(f"\n📊 After Learning:")
    print(f"   Past Wins: {len(profile.past_wins)}")
    print(f"   Past Bids: {len(profile.past_bids)}")
    print(f"   Overall Win Rate: {profile.calculate_overall_win_rate():.1%}")
    print(f"   ESA Win Rate: {profile.win_rate_by_buyer.get('ESA', 0):.1%}")
    print(f"   EDA Win Rate: {profile.win_rate_by_buyer.get('EDA', 0):.1%}")
    print(f"   ")
    print(f"   💡 The system now knows you're strong with ESA/EDA!")
    print(f"   Future ESA tenders will get higher win probability scores.")


async def demo_competitive_advantage():
    """Explain why this creates a moat."""
    print_header("🏰 Why This Creates a Nash-Stable Moat")
    
    print("""
    🎯 THE FLYWHEEL EFFECT:
    
    1. User joins platform
       ↓
    2. Gets personalized tender matches
       ↓
    3. Submits bids on high-probability tenders
       ↓
    4. Reports outcomes (wins/losses)
       ↓
    5. Algorithm learns their strengths
       ↓
    6. Future matches become MORE accurate
       ↓
    7. User wins more contracts
       ↓
    8. User becomes loyal (high switching cost)
       ↓
    9. Data moat deepens
       ↓
    (Repeat)
    
    💪 COMPETITIVE DEFENSE:
    
    Competitor can copy:
    ✅ The scraper
    ✅ The UI
    ✅ Basic search
    
    Competitor CANNOT copy:
    ❌ Your user's 3 years of bid history
    ❌ Your user's win patterns per buyer
    ❌ Your user's preference evolution
    ❌ The accumulated training data
    
    📈 NETWORK EFFECTS:
    
    More users → More bid outcomes → Better predictions → More wins → More users
    
    This is the same moat that makes:
    • Netflix's recommendations hard to copy
    • TikTok's algorithm unbeatable
    • Amazon's purchase predictions accurate
    """)


async def main():
    """Run all demos."""
    print("\n" + "=" * 70)
    print("  🚀 SPACescraper Win Prediction Engine")
    print("  The Nash-Stable Feature")
    print("=" * 70)
    
    try:
        await demo_basic_matching()
        await demo_learning_loop()
        await demo_competitive_advantage()
        
        print_header("✅ Demo Complete!")
        print("""
    Next Steps:
    1. Start API: python main.py
    2. Create profile: POST /profile
    3. Get matches: POST /tenders/match
    4. Report outcomes: POST /tenders/outcome
    
    API Documentation: http://localhost:8000/docs
        """)
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
