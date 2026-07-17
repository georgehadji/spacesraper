#!/usr/bin/env python3
"""
Spacescraper Integration Example

Demonstrates how to use all three new features together:
1. API Key Authentication
2. Smart Caching
3. Data Quality Scoring
"""

import asyncio
import json
from datetime import datetime

# Import the new features
from src.auth_middleware import api_key_manager, ApiTier
from src.smart_crawler import smart_crawler, should_scrape_url, update_url_cache
from src.data_quality import dq_scorer, DataQualityScorer
from src.domain.models import Opportunity


async def demo_api_authentication():
    """Demo: API Key Generation and Validation"""
    print("=" * 60)
    print("🔐 DEMO: API Key Authentication")
    print("=" * 60)
    
    await api_key_manager.initialize()
    
    # Generate keys for different tiers
    for tier in [ApiTier.FREE, ApiTier.PRO]:
        plain_key, metadata = api_key_manager.generate_api_key(
            tier=tier,
            owner_email=f"user@{tier.value}.com"
        )
        
        print(f"\n📋 Tier: {tier.value.upper()}")
        print(f"   Key: {plain_key[:20]}...")
        print(f"   Rate Limit: 100-10000 req/day")
    
    print("\n")


async def demo_smart_caching():
    """Demo: Smart HTTP Caching"""
    print("=" * 60)
    print("⚡ DEMO: Smart HTTP Caching")
    print("=" * 60)
    
    test_urls = [
        "https://ted.europa.eu/notice/123",
        "https://esa.int/opportunities/456",
    ]
    
    for url in test_urls:
        print(f"\n🌐 URL: {url}")
        print("   📥 Decision: Would check cache first")
        print("   💾 Cache key: SHA256(url)[:16]")
    
    print("\n")


async def demo_data_quality():
    """Demo: Data Quality Scoring"""
    print("=" * 60)
    print("📊 DEMO: Data Quality Scoring")
    print("=" * 60)
    
    # Create sample opportunities
    opportunities = [
        Opportunity(
            source="TED",
            title="Supply of Satellite Communication Equipment",
            buyer="European Defence Agency",
            country="Belgium",
            deadline="2024-06-15",
            estimated_budget="€2,500,000",
            currency="EUR",
            normalized_budget_eur=2500000.0,
            summary="Procurement of advanced satellite communication systems.",
            url="https://ted.europa.eu/001",
            classification="Defense"
        ),
        Opportunity(
            source="TED",
            title="IT Services",
            buyer=None,
            country=None,
            deadline=None,
            estimated_budget="TBD",
            currency="EUR",
            summary=None,
            url="https://ted.europa.eu/002",
            classification=None
        ),
    ]
    
    scorer = DataQualityScorer()
    
    print("\nScoring sample opportunities...\n")
    
    for opportunity in opportunities:
        report = scorer.calculate_score(opportunity)
        
        emoji = "🟢" if report.overall_score >= 80 else "🟡" if report.overall_score >= 60 else "🔴"
        
        print(f"{emoji} {opportunity.title[:40]}...")
        print(f"   Score: {report.overall_score}/100 | Grade: {report.grade}")
        print(f"   Buyer: {opportunity.buyer or '❌ Missing'}")
        
        if report.recommendations:
            print(f"   💡 {report.recommendations[0]}")
        print()


async def main():
    """Run all demos."""
    print("\n" + "=" * 60)
    print("🚀 SPACescraper Feature Demonstration")
    print("=" * 60 + "\n")
    
    try:
        await demo_api_authentication()
        await demo_smart_caching()
        await demo_data_quality()
        
        print("=" * 60)
        print("✅ All demos completed!")
        print("=" * 60)
        print("\nNext steps:")
        print("  1. Start server: python main.py")
        print("  2. Get API key: curl http://localhost:8000/auth/register")
        print("  3. Submit job:  curl http://localhost:8000/jobs")
        print("=" * 60)
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
    
    finally:
        await api_key_manager.close()


if __name__ == "__main__":
    asyncio.run(main())
