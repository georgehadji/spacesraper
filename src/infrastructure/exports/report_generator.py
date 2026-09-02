# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Reporting Engine)
# Role: Generates multi-format intelligence shipments and UX dashboards.

import os
import logging
import pandas as pd
from datetime import datetime
from typing import List
from src.domain.models import ExtractedRecord

logger = logging.getLogger("Spacescraper.ReportGenerator")

class ReportGenerator:
    """
    Generates CSV/JSON export artifacts for extracted records.
    """

    def __init__(self, export_dir: str = "exports"):
        self.export_dir = export_dir
        os.makedirs(self.export_dir, exist_ok=True)

    def generate_excel_csv(self, records: List[ExtractedRecord], target_site: str):
        """Generates structured intelligence files."""
        if not records: return

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        df = pd.DataFrame([t.model_dump() for t in records])

        excel_path = f"{self.export_dir}/{target_site}_intel_{timestamp}.xlsx"
        csv_path = f"{self.export_dir}/{target_site}_intel_{timestamp}.csv"

        try:
            with pd.ExcelWriter(excel_path, engine='openpyxl') as writer:
                df.to_excel(writer, sheet_name='All Records', index=False)
                if 'change_type' in df.columns:
                    df[df['change_type'] == 'NEW'].to_excel(writer, sheet_name='New', index=False)
                    df[df['change_type'] == 'UPDATED'].to_excel(writer, sheet_name='Updated', index=False)
            
            df.to_csv(csv_path, index=False, encoding='utf-8')
            logger.info(f"Spacescraper: Generated shipments at {excel_path}")
        except Exception as e:
            logger.error(f"Report generation failure: {e}")
