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
from src.domain.models import Opportunity

logger = logging.getLogger("Spacescraper.SqliteTracker")

class SqliteTracker:
    """
    Spacescraper State Auditor.
    Maintains a persistent record of all discovered opportunities to enable 
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
                CREATE TABLE IF NOT EXISTS opportunities (
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
            await db.execute("CREATE INDEX IF NOT EXISTS idx_opportunities_source ON opportunities(source)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_opportunities_last_seen ON opportunities(last_seen)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_opportunities_dup_id ON opportunities(duplicate_group_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_opportunities_title ON opportunities(title)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_opportunities_buyer ON opportunities(buyer)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_opportunities_external_id ON opportunities(external_id)")
            
            await db.commit()

            # Migration: add identity_hash column if not present (safe to run multiple times)
            try:
                await db.execute("ALTER TABLE opportunities ADD COLUMN identity_hash TEXT")
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

    async def get_opportunity_by_id(self, opportunity_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a specific opportunity snapshot for comparison."""
        async with self._get_connection() as db:
            async with db.execute("SELECT * FROM opportunities WHERE id = ?", (opportunity_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def get_opportunity_by_external_id(self, external_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a opportunity by its external ID."""
        if not external_id:
            return None
        async with self._get_connection() as db:
            async with db.execute("SELECT * FROM opportunities WHERE external_id = ?", (external_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def find_similar_opportunities(self, title: str, buyer: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Query for potential duplicates based on title similarity heuristics.
        Uses optimized query with buyer filtering.
        """
        async with self._get_connection() as db:
            if buyer:
                # Query with buyer filter for better precision
                query = """
                    SELECT * FROM opportunities 
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
                    SELECT * FROM opportunities 
                    WHERE title LIKE ?
                    ORDER BY last_seen DESC
                    LIMIT ?
                """
                pattern = f"%{title[:20]}%"
                async with db.execute(query, (pattern, limit)) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(r) for r in rows]

    async def upsert_opportunity(self, opportunity: Opportunity) -> bool:
        """
        Persists or updates a opportunity state.
        Returns True if inserted (new), False if updated.
        """
        opportunity_id = opportunity.url
        
        async with self._get_connection() as db:
            # Check if exists
            async with db.execute("SELECT 1 FROM opportunities WHERE id = ?", (opportunity_id,)) as cursor:
                exists = await cursor.fetchone() is not None
            
            # Prepare embedding
            embedding_json = json.dumps(opportunity.embedding) if opportunity.embedding else None
            
            await db.execute("""
                INSERT INTO opportunities (
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
                opportunity_id, opportunity.source, opportunity.external_id, opportunity.title,
                opportunity.buyer, opportunity.country, opportunity.publication_date,
                opportunity.deadline, opportunity.estimated_budget, opportunity.currency,
                opportunity.status, opportunity.url, opportunity.summary, opportunity.normalized_budget_eur,
                embedding_json, opportunity.content_hash,
                opportunity.identity_hash,
                opportunity.first_seen.isoformat(), opportunity.last_seen.isoformat(),
                opportunity.classification, opportunity.duplicate_group_id
            ))
            await db.commit()
            return not exists

    async def upsert_opportunities_batch(self, opportunities: List[Opportunity]) -> Dict[str, int]:
        """
        Batch upsert for improved performance.
        Returns counts of new vs updated records.
        """
        counts = {"new": 0, "updated": 0}
        
        async with self._get_connection() as db:
            for opportunity in opportunities:
                opportunity_id = opportunity.url
                
                # Check if exists
                async with db.execute("SELECT 1 FROM opportunities WHERE id = ?", (opportunity_id,)) as cursor:
                    exists = await cursor.fetchone() is not None
                
                if exists:
                    counts["updated"] += 1
                else:
                    counts["new"] += 1
                
                embedding_json = json.dumps(opportunity.embedding) if opportunity.embedding else None
                
                await db.execute("""
                    INSERT INTO opportunities (
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
                    opportunity_id, opportunity.source, opportunity.external_id, opportunity.title,
                    opportunity.buyer, opportunity.country, opportunity.publication_date,
                    opportunity.deadline, opportunity.estimated_budget, opportunity.currency,
                    opportunity.status, opportunity.url, opportunity.summary, opportunity.normalized_budget_eur,
                    embedding_json, opportunity.content_hash,
                    opportunity.identity_hash,
                    opportunity.first_seen.isoformat(), opportunity.last_seen.isoformat(),
                    opportunity.classification, opportunity.duplicate_group_id
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

    async def get_recent_opportunities(self, source: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """Get most recent opportunities, optionally filtered by source."""
        async with self._get_connection() as db:
            if source:
                query = """
                    SELECT * FROM opportunities 
                    WHERE source = ?
                    ORDER BY last_seen DESC
                    LIMIT ?
                """
                async with db.execute(query, (source, limit)) as cursor:
                    rows = await cursor.fetchall()
            else:
                query = """
                    SELECT * FROM opportunities 
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
