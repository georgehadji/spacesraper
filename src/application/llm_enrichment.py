# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (AI Enrichment Layer)
# Role: Integrates Large Language Models (LLMs) for data polishing and recovery.

import logging
import asyncio
import os
import json
from typing import List, Union
from src.domain.models import BaseEntity

# Load environment variables (e.g., OPENAI_API_KEY) for secure API access
from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger("Spacescraper.AIEnricher")

class AIEnricher:
    """
    Spacescraper AI Integration Node.
    Uses OpenAI's GPT models to transform raw scraped data into high-value 
    marketing and SEO content. Also serves as a 'Self-Healing' mechanism 
    to extract data from complex HTML structures when typical selectors fail.
    """
    
    def __init__(self, api_key: str = None):
        """
        Initializes the enricher with optional API key override.
        """
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        
    async def enrich(self, entities: List[BaseEntity]) -> List[BaseEntity]:
        """
        Product Enrichment Workflow:
        Iterates through entities and uses GPT-4o-mini to generate 
        optimized titles and descriptions.
        """
        logger.info("Spacescraper AI: Starting enrichment cycle...")
        
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=self.api_key)
        except ImportError:
            logger.error("Spacescraper AI: 'openai' package not found. Skipping enrichment.")
            return entities

        enriched_entities = []
        for entity in entities:
            # Type Check: We primarily enrich E-Commerce Products for now
            from src.domain.models import Product
            if isinstance(entity, Product) and getattr(entity, 'name', None):
                try:
                    # Construct a semantic prompt for the SEO expert agent
                    prompt = f"""
                    System Role: E-commerce SEO Specialist.
                    Task: Optimize the following product data for a premium web store.
                    
                    Input:
                    - Title: {entity.name}
                    - Description: {entity.description or 'N/A'}
                    - Current Category: {entity.category or 'Uncategorized'}
                    
                    Output Requirements (JSON):
                    - "seo_title": Catchy, SEO-optimized title (max 60 chars).
                    - "seo_description": Persuasive, multi-paragraph description.
                    - "seo_tags": 5-8 comma-separated keyword tags.
                    - "category": Standard taxonomy category.
                    """
                    
                    response = await client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[{"role": "user", "content": prompt}],
                        response_format={"type": "json_object"}
                    )
                    
                    # Parse the model response
                    result_json = json.loads(response.choices[0].message.content)
                    
                    # Map GPT output back to the Pydantic Product model
                    entity.seo_title = result_json.get("seo_title", entity.name)
                    entity.seo_description = result_json.get("seo_description", entity.description)
                    entity.seo_tags = result_json.get("seo_tags", "")
                    entity.woo_category = result_json.get("woo_category", entity.category)
                    
                    # Dynamic Pricing Heuristic:
                    # Automatically adjust prices based on predefined margins
                    if entity.price:
                        if entity.price < 50: margin = 1.40
                        elif entity.price < 200: margin = 1.25
                        else: margin = 1.15
                        entity.updated_price = round(entity.price * margin, 2)
                        
                    logger.info(f"Spacescraper AI: Enriched product '{entity.name}' successfully.")
                    
                except Exception as e:
                    logger.error(f"Spacescraper AI: Failed to process product '{entity.name}': {e}")
                    
            elif hasattr(entity, 'lead_score') and entity.lead_score is None:
                # Basic lead qualification logic (mocked for now)
                entity.lead_score = 7

            enriched_entities.append(entity)
            
        logger.info(f"Spacescraper AI: Enrichment cycle closed for {len(enriched_entities)} items.")
        return enriched_entities

    async def fix_schema_fallback(self, raw_html: str, target_site: str) -> List[BaseEntity]:
        """
        Spacescraper Self-Healing Logic.
        When specialized extractors fail to find elements, this method passes 
        minified HTML to the LLM to extract data directly from the DOM structure.
        """
        logger.warning(f"Spacescraper: Initiating LLM-based parsing for [{target_site}]")
        
        # Note: In production, large HTML should be truncated to fit context windows.
        # Mocking recovery for architectural demonstration.
        from src.domain.models import Product
        mock_recovered_id = f"RECOVERED_{hash(raw_html[:100]) % 10000}"
        
        mock_recovered_product = Product(
            id=mock_recovered_id,
            name="Spacescraper Recovered Item",
            price=29.99,
            currency="USD",
            url="https://recovery.spacescraper.ai",
            source_url=target_site
        )
        await asyncio.sleep(0.5) # Simulate inference latency
        return [mock_recovered_product]
