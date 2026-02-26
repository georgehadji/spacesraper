# Author: Georgios-Chrysovalantis Chatzivantsidis
# Project: Spacescraper (Persistence & Intelligence)
# Role: SQLite-backed repository for tracking procurement entity lifecycles.

import aiosqlite
import logging
import json
import hashlib
from datetime import datetime
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager
from src.domain.models import Tender

logger = logging.getLogger("Spacescraper.SqliteTracker")

class SqliteTracker:
    """
    Spacescraper State Auditor.
    Maintains a persistent record of all discovered tenders to enable 
    accurate change detection, fuzzy deduplication, and historical reporting.
    Uses connection pooling for better performance.
    """
    
    def __init__(self, db_path: str = "spacescraper_intel.db", pool_size: int = 5):
        self.db_path = db_path
        self._pool: List[aiosqlite.Connection] = []
        self._pool_size = pool_size
        self._lock_pool = []
        self._initialized = False

    async def initialize(self):
        """Provision the intelligence schema and connection pool."""
        if self._initialized:
            return
            
        # Create initial connection to set up schema
        async with aiosqlite.connect(self.db_path) as db:
            # Performance: WAL mode allows concurrent reads during a write operation
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.execute("PRAGMA temp_store=MEMORY")
            await db.execute("PRAGMA mmap_size=30000000")  # 30MB memory map
            
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tenders (
                    id TEXT PRIMARY KEY,
                    source TEXT,
                    external_id TEXT,
                    title TEXT,
                    buyer TEXT,
                    country TEXT,
                    publication_date TEXT,
                    deadline TEXT,
                    estimated_budget TEXT,
                    currency TEXT,
                    status TEXT,
                    url TEXT,
                    summary TEXT,
                    normalized_budget_eur REAL,
                    embedding TEXT,
                    content_hash TEXT,
                    first_seen TIMESTAMP,
                    last_seen TIMESTAMP,
                    classification TEXT,
                    duplicate_group_id TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    timestamp TIMESTAMP,
                    source TEXT,
                    new_count INTEGER,
                    updated_count INTEGER,
                    total_count INTEGER
                )
            """)
            
            # Optimization: Strategic indexes to speed up lookup and grouping
            await db.execute("CREATE INDEX IF NOT EXISTS idx_tenders_source ON tenders(source)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_tenders_last_seen ON tenders(last_seen)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_tenders_dup_id ON tenders(duplicate_group_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_tenders_title ON tenders(title)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_tenders_buyer ON tenders(buyer)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_tenders_external_id ON tenders(external_id)")
            
            await db.commit()

            # Migration: add identity_hash column if not present (safe to run multiple times)
            try:
                await db.execute("ALTER TABLE tenders ADD COLUMN identity_hash TEXT")
                await db.commit()
            except Exception:
                pass  # Column already exists

        # Initialize connection pool
        for _ in range(self._pool_size):
            conn = await aiosqlite.connect(self.db_path)
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            self._pool.append(conn)
        
        self._initialized = True
        logger.info(f"Spacescraper Intelligence DB initialized at {self.db_path} (pool: {self._pool_size})")

    @asynccontextmanager
    async def _get_connection(self):
        """Get a connection from the pool."""
        if not self._pool:
            # Fallback: create temporary connection if pool exhausted
            conn = await aiosqlite.connect(self.db_path)
            conn.row_factory = aiosqlite.Row
            try:
                yield conn
            finally:
                await conn.close()
        else:
            conn = self._pool.pop()
            try:
                yield conn
            finally:
                self._pool.append(conn)

    async def get_tender_by_id(self, tender_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a specific tender snapshot for comparison."""
        async with self._get_connection() as db:
            async with db.execute("SELECT * FROM tenders WHERE id = ?", (tender_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def get_tender_by_external_id(self, external_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a tender by its external ID."""
        if not external_id:
            return None
        async with self._get_connection() as db:
            async with db.execute("SELECT * FROM tenders WHERE external_id = ?", (external_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def find_similar_tenders(self, title: str, buyer: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Query for potential duplicates based on title similarity heuristics.
        Uses optimized query with buyer filtering.
        """
        async with self._get_connection() as db:
            if buyer:
                # Query with buyer filter for better precision
                query = """
                    SELECT * FROM tenders 
                    WHERE buyer = ? 
                    AND (title LIKE ? OR ABS(LENGTH(title) - ?) < 20)
                    ORDER BY last_seen DESC
                    LIMIT ?
                """
                pattern = f"%{title[:20]}%"  # Prefix matching
                async with db.execute(query, (buyer, pattern, len(title), limit)) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(r) for r in rows]
            else:
                # Query without buyer filter
                query = """
                    SELECT * FROM tenders 
                    WHERE title LIKE ?
                    ORDER BY last_seen DESC
                    LIMIT ?
                """
                pattern = f"%{title[:20]}%"
                async with db.execute(query, (pattern, limit)) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(r) for r in rows]

    async def upsert_tender(self, tender: Tender) -> bool:
        """
        Persists or updates a tender state.
        Returns True if inserted (new), False if updated.
        """
        tender_id = tender.url
        
        async with self._get_connection() as db:
            # Check if exists
            async with db.execute("SELECT 1 FROM tenders WHERE id = ?", (tender_id,)) as cursor:
                exists = await cursor.fetchone() is not None
            
            # Prepare embedding
            embedding_json = json.dumps(tender.embedding) if tender.embedding else None
            
            await db.execute("""
                INSERT INTO tenders (
                    id, source, external_id, title, buyer, country,
                    publication_date, deadline, estimated_budget, currency,
                    status, url, summary, normalized_budget_eur, embedding, content_hash,
                    identity_hash,
                    first_seen, last_seen, classification, duplicate_group_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    deadline = excluded.deadline,
                    estimated_budget = excluded.estimated_budget,
                    summary = excluded.summary,
                    normalized_budget_eur = excluded.normalized_budget_eur,
                    embedding = excluded.embedding,
                    content_hash = excluded.content_hash,
                    identity_hash = excluded.identity_hash,
                    last_seen = excluded.last_seen,
                    classification = excluded.classification,
                    duplicate_group_id = excluded.duplicate_group_id
            """, (
                tender_id, tender.source, tender.external_id, tender.title,
                tender.buyer, tender.country, tender.publication_date,
                tender.deadline, tender.estimated_budget, tender.currency,
                tender.status, tender.url, tender.summary, tender.normalized_budget_eur,
                embedding_json, tender.content_hash,
                tender.identity_hash,
                tender.first_seen.isoformat(), tender.last_seen.isoformat(),
                tender.classification, tender.duplicate_group_id
            ))
            await db.commit()
            return not exists

    async def upsert_tenders_batch(self, tenders: List[Tender]) -> Dict[str, int]:
        """
        Batch upsert for improved performance.
        Returns counts of new vs updated records.
        """
        counts = {"new": 0, "updated": 0}
        
        async with self._get_connection() as db:
            for tender in tenders:
                tender_id = tender.url
                
                # Check if exists
                async with db.execute("SELECT 1 FROM tenders WHERE id = ?", (tender_id,)) as cursor:
                    exists = await cursor.fetchone() is not None
                
                if exists:
                    counts["updated"] += 1
                else:
                    counts["new"] += 1
                
                embedding_json = json.dumps(tender.embedding) if tender.embedding else None
                
                await db.execute("""
                    INSERT INTO tenders (
                        id, source, external_id, title, buyer, country,
                        publication_date, deadline, estimated_budget, currency,
                        status, url, summary, normalized_budget_eur, embedding, content_hash,
                        identity_hash,
                        first_seen, last_seen, classification, duplicate_group_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        status = excluded.status,
                        deadline = excluded.deadline,
                        estimated_budget = excluded.estimated_budget,
                        summary = excluded.summary,
                        normalized_budget_eur = excluded.normalized_budget_eur,
                        embedding = excluded.embedding,
                        content_hash = excluded.content_hash,
                        identity_hash = excluded.identity_hash,
                        last_seen = excluded.last_seen,
                        classification = excluded.classification,
                        duplicate_group_id = excluded.duplicate_group_id
                """, (
                    tender_id, tender.source, tender.external_id, tender.title,
                    tender.buyer, tender.country, tender.publication_date,
                    tender.deadline, tender.estimated_budget, tender.currency,
                    tender.status, tender.url, tender.summary, tender.normalized_budget_eur,
                    embedding_json, tender.content_hash,
                    tender.identity_hash,
                    tender.first_seen.isoformat(), tender.last_seen.isoformat(),
                    tender.classification, tender.duplicate_group_id
                ))
            
            await db.commit()
        
        return counts

    async def log_run(self, run_id: str, source: str, counts: Dict[str, int]):
        """Records a scraper execution session."""
        async with self._get_connection() as db:
            await db.execute("""
                INSERT INTO runs (run_id, timestamp, source, new_count, updated_count, total_count)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                run_id, datetime.now().isoformat(), source,
                counts.get('NEW', 0), counts.get('UPDATED', 0), counts.get('TOTAL', 0)
            ))
            await db.commit()

    async def get_recent_tenders(self, source: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """Get most recent tenders, optionally filtered by source."""
        async with self._get_connection() as db:
            if source:
                query = """
                    SELECT * FROM tenders 
                    WHERE source = ?
                    ORDER BY last_seen DESC
                    LIMIT ?
                """
                async with db.execute(query, (source, limit)) as cursor:
                    rows = await cursor.fetchall()
            else:
                query = """
                    SELECT * FROM tenders 
                    ORDER BY last_seen DESC
                    LIMIT ?
                """
                async with db.execute(query, (limit,)) as cursor:
                    rows = await cursor.fetchall()
            
            return [dict(r) for r in rows]

    async def close(self):
        """Close all pooled connections."""
        for conn in self._pool:
            try:
                await conn.close()
            except Exception:
                pass
        self._pool.clear()
        self._initialized = False

# Global tracker instance
intel_tracker = SqliteTracker()
