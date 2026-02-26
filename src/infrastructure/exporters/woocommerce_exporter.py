# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Persistence & Export Layer)
# Role: Formats extracted product data into WooCommerce-compatible CSV shipments.

import csv
import os
import logging
from typing import List
from datetime import datetime
from src.domain.models import Product

# Initialize localized logger for export auditing
logger = logging.getLogger("Spacescraper.WooCommerceExporter")

class WooCommerceExporter:
    """
    Spacescraper E-Commerce Bridge.
    This component serializes enriched Product entities into a CSV format 
    that is natively understood by the WooCommerce Product Importer. 
    It ensures that AI-generated SEO content is prioritized and that technical 
    specifications are formatted as HTML for professional presentation.
    """
    
    # Authoritative WooCommerce Metadata Headers
    HEADERS = [
        "Type", "SKU", "Name", "Published", "Is featured?", "Visibility in catalog", 
        "Short description", "Description", "Sale price", "Regular price", 
        "Categories", "Tags", "Images", "External URL"
    ]
    
    @staticmethod
    def export(products: List[Product], target_site: str, export_dir: str = "exports") -> str:
        """
        Generates a WooCommerce-ready CSV snapshot from a batch of products.
        
        Args:
            products: List of Product entities to serialize.
            target_site: Identifier for the source (used in naming).
            export_dir: Destination folder for the generated file.
        """
        os.makedirs(export_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(export_dir, f"{target_site}_woo_export_{timestamp}.csv")
        
        # Filtering Layer: Ensure we only process valid Product domain models
        woo_products = [p for p in products if isinstance(p, Product)]
        
        if not woo_products:
            logger.warning("Spacescraper Export: No valid Product entities available for WooCommerce sync.")
            return None

        try:
            with open(filename, mode="w", newline="", encoding="utf-8") as file:
                writer = csv.DictWriter(file, fieldnames=WooCommerceExporter.HEADERS)
                writer.writeheader()
                
                for prod in woo_products:
                    # Logic: Prioritize AI-enriched SEO fields over raw scraped data
                    display_title = prod.seo_title if prod.seo_title else prod.name
                    main_description = prod.seo_description if prod.seo_description else (prod.description or "")
                    
                    # Representation Transformation: 
                    # Convert raw technical spec dictionaries into elegant HTML lists for the storefront.
                    if prod.technical_specs:
                        specs_html = "<div class='ss-specs'><h3>Technical Specifications</h3><ul>"
                        for k, v in prod.technical_specs.items():
                            specs_html += f"<li><strong>{k}:</strong> {v}</li>"
                        specs_html += "</ul></div>"
                        main_description += f"<br><br>{specs_html}"
                    
                    # Meta-data alignment
                    product_category = prod.woo_category if prod.woo_category else (prod.category or "")
                    product_tags = prod.seo_tags if prod.seo_tags else ""
                    
                    # Media Handling: 
                    # WooCommerce consumes absolute image URLs. Spacescraper provides direct CDN links.
                    primary_image = prod.image_url if prod.image_url else ""
                    
                    # Pricing Strategy:
                    # Map the algorithmically calculated 'updated_price' to the shop's regular price.
                    display_price = prod.updated_price if prod.updated_price else prod.price
                    
                    # Construct the standard WooCommerce ingestion row
                    row = {
                        "Type": "simple",
                        "SKU": str(prod.id) if prod.id and prod.id != "unknown" else f"ss_{hash(prod.name)}",
                        "Name": display_title,
                        "Published": "1" if not prod.is_out_of_stock else "0",
                        "Is featured?": "0",
                        "Visibility in catalog": "visible",
                        "Short description": display_title, 
                        "Description": main_description,
                        "Sale price": "", # Placeholder for promotional logic
                        "Regular price": f"{display_price:.2f}" if display_price else "",
                        "Categories": product_category,
                        "Tags": product_tags,
                        "Images": primary_image,
                        "External URL": prod.url
                    }
                    writer.writerow(row)
                    
            logger.info(f"Spacescraper Export: WooCommerce shipment authorized at: {filename}")
            return filename
        except Exception as e:
            logger.error(f"Spacescraper Export Fault: Failed to write WooCommerce CSV: {e}")
            return None
