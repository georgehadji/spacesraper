# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Storage & Assets Node)
# Role: Manages local acquisition and caching of remote media assets.

import hashlib
import logging
from pathlib import Path

from src.infrastructure.http_client import target_http

# Localized logger for media management telemetry
logger = logging.getLogger("Spacescraper.ImageDownloader")

class ImageDownloader:
    """
    Spacescraper Asset Node.
    Responsible for downloading product images and media to the local filesystem. 
    This is essential for local caching, image analysis, or storefronts 
    (like WooCommerce) that benefit from local file references via relative paths.
    """
    
    def __init__(self, base_dir: str = "downloads/images"):
        # Ensure the destination directory exists within the container/host
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        
    async def download(self, url: str) -> str | None:
        """
        Retrieves a remote asset and stores it logically using a hash-based filename.
        Implements basic caching by checking for file existence before downloading.
        
        Returns:
            The absolute or relative path to the local asset, or None if download failed.
        """
        if not url or not url.startswith("http"):
            logger.debug(f"Spacescraper: Skipping invalid image URL: {url}")
            return None
            
        try:
            # Hash-based Filename: Ensures 1:1 mapping and avoids filesystem collisions
            url_hash = hashlib.md5(url.encode(), usedforsecurity=False).hexdigest()
            # Heuristic Extension Capture: Extract extension while ignoring URL parameters
            extension_match = url.split('.')[-1].split('?')[0].lower()
            extension = extension_match if len(extension_match) <= 4 and extension_match != 'com' else "jpg"
            
            filename = f"{url_hash}.{extension}"
            file_path = self.base_dir / filename
            
            # Caching Layer: Don't re-download what we already have
            if file_path.exists():
                return str(file_path)
                
            # Perform Async GET request via the shared pipeline client
            client = await target_http.get_client()
            response = await client.get(url, timeout=15.0)
            
            if response.status_code == 200:
                # Atomically write to the storage node
                with open(file_path, "wb") as f:
                    f.write(response.content)
                logger.info(f"Spacescraper: Asset cached: {url_hash}.{extension}")
                return str(file_path)
            else:
                logger.warning(f"Spacescraper: Asset fetch failure [{response.status_code}] for {url}")
                    
        except Exception as e:
            logger.error(f"Spacescraper: Critical downloader error for {url}: {e}")
            
        return None

# Global Singleton for use across the processor node
image_downloader = ImageDownloader()
