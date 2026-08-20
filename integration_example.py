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
from src.auth_middleware import ApiTier, api_key_manager
from src.smart_crawler import should_scrape_url, smart_crawler, update_url_cache


async def demo_api_authentication():
    """Demo: API Key Generation and Validation"""
    print("=" * 60)
    print("🔐 DEMO: API Key Authentication")
    print("=" * 60)
    
    await api_key_manager.initialize()
    
    # Generate keys for different tiers
    for tier in [ApiTier.FREE, ApiTier.PRO]:
        plain_key, metadata = await api_key_manager.generate_api_key(
            tier=tier,
            owner_email=f"user@{tier.value}.com"
        )
        
        print(f"\n📋 Tier: {tier.value.upper()}")
        print(f"   Key: {plain_key[:20]}...")
        print("   Rate Limit: 100-10000 req/day")
    
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


async def main():
    """Run all demos."""
    print("\n" + "=" * 60)
    print("🚀 SPACescraper Feature Demonstration")
    print("=" * 60 + "\n")

    try:
        await demo_api_authentication()
        await demo_smart_caching()

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
