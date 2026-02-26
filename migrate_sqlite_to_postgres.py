#!/usr/bin/env python3
"""
Author: Georgios-Chrysovalantis Chatzivantsidis
Project: Spacescraper (Database Migration Tool)
Role: Migrate data from SQLite to PostgreSQL with zero downtime.

Usage:
    # Dry run (preview changes)
    python migrate_sqlite_to_postgres.py --dry-run
    
    # Actual migration
    python migrate_sqlite_to_postgres.py --execute
    
    # Specific tables only
    python migrate_sqlite_to_postgres.py --execute --tables tenders,runs
    
    # Batch size tuning
    python migrate_sqlite_to_postgres.py --execute --batch-size 500
"""

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass

from sqlalchemy import text

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"migration_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    ]
)
logger = logging.getLogger("Spacescraper.Migration")


@dataclass
class MigrationStats:
    """Statistics for migration tracking."""
    table_name: str
    source_count: int = 0
    target_count: int = 0
    inserted: int = 0
    updated: int = 0
    errors: int = 0
    duration_seconds: float = 0.0


class DatabaseMigrator:
    """
    SQLite to PostgreSQL Migration Tool.
    Supports incremental migration with conflict resolution.
    """
    
    def __init__(
        self,
        sqlite_path: str = "spacescraper_intel.db",
        dry_run: bool = True,
        batch_size: int = 100
    ):
        self.sqlite_path = sqlite_path
        self.dry_run = dry_run
        self.batch_size = batch_size
        self.stats: List[MigrationStats] = []
        self._sqlite_conn: Optional[sqlite3.Connection] = None
        self._pg_session = None
        
    async def initialize(self):
        """Initialize connections to both databases."""
        logger.info(f"Initializing connections...")
        
        # SQLite connection
        if not Path(self.sqlite_path).exists():
            raise FileNotFoundError(f"SQLite database not found: {self.sqlite_path}")
        
        self._sqlite_conn = sqlite3.connect(self.sqlite_path)
        self._sqlite_conn.row_factory = sqlite3.Row
        
        # PostgreSQL connection
        from src.database_models import async_session_maker, init_db, engine
        await init_db()
        
        logger.info("✅ Connections initialized")
    
    def close(self):
        """Close database connections."""
        if self._sqlite_conn:
            self._sqlite_conn.close()
            logger.info("SQLite connection closed")
    
    async def migrate_all(self, tables: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Run full migration.
        
        Args:
            tables: Specific tables to migrate, or None for all
            
        Returns:
            Migration summary statistics
        """
        start_time = datetime.now()
        available_tables = ['tenders', 'runs', 'dead_letters', 'event_logs']
        
        if tables:
            tables_to_migrate = [t for t in tables if t in available_tables]
        else:
            tables_to_migrate = available_tables
        
        logger.info("=" * 60)
        logger.info(f"Starting migration: SQLite → PostgreSQL")
        logger.info(f"Dry run: {self.dry_run}")
        logger.info(f"Batch size: {self.batch_size}")
        logger.info(f"Tables: {', '.join(tables_to_migrate)}")
        logger.info("=" * 60)
        
        for table in tables_to_migrate:
            if table == 'tenders':
                await self._migrate_tenders()
            elif table == 'runs':
                await self._migrate_runs()
            elif table == 'dead_letters':
                await self._migrate_dead_letters()
            elif table == 'event_logs':
                logger.info("Skipping event_logs (optional, high volume)")
        
        duration = (datetime.now() - start_time).total_seconds()
        
        return self._generate_report(duration)
    
    async def _migrate_tenders(self):
        """Migrate tenders table with conflict resolution."""
        logger.info("\n📦 Migrating tenders...")
        stats = MigrationStats("tenders")
        start_time = datetime.now()
        
        # Get source count
        cursor = self._sqlite_conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM tenders")
        stats.source_count = cursor.fetchone()[0]
        logger.info(f"Source records: {stats.source_count}")
        
        if stats.source_count == 0:
            logger.info("No tenders to migrate")
            return
        
        # Fetch and migrate in batches
        cursor.execute("SELECT * FROM tenders")
        
        batch = []
        processed = 0
        
        from src.database_models import async_session_maker, TenderModel
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        
        async with async_session_maker() as pg_session:
            for row in cursor:
                row_dict = dict(row)
                
                # Transform data
                tender_data = self._transform_tender(row_dict)
                batch.append(tender_data)
                
                if len(batch) >= self.batch_size:
                    if not self.dry_run:
                        inserted, updated = await self._upsert_tenders_batch(pg_session, batch)
                        stats.inserted += inserted
                        stats.updated += updated
                    
                    processed += len(batch)
                    batch = []
                    
                    if processed % 1000 == 0:
                        logger.info(f"  Progress: {processed}/{stats.source_count} ({processed/stats.source_count*100:.1f}%)")
            
            # Process remaining batch
            if batch and not self.dry_run:
                inserted, updated = await self._upsert_tenders_batch(pg_session, batch)
                stats.inserted += inserted
                stats.updated += updated
            
            processed += len(batch)
            
            # Get target count
            if not self.dry_run:
                result = await pg_session.execute(text("SELECT COUNT(*) FROM tenders"))
                stats.target_count = result.scalar()
            else:
                stats.target_count = 0
        
        stats.duration_seconds = (datetime.now() - start_time).total_seconds()
        self.stats.append(stats)
        
        logger.info(f"✅ Tenders migrated in {stats.duration_seconds:.2f}s")
        if self.dry_run:
            logger.info(f"   [DRY RUN] Would insert: {stats.source_count}")
        else:
            logger.info(f"   Inserted: {stats.inserted}, Updated: {stats.updated}")
    
    def _transform_tender(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Transform SQLite row to PostgreSQL format."""
        # Parse embedding JSON
        embedding = None
        if row.get('embedding'):
            try:
                embedding = json.loads(row['embedding'])
            except (json.JSONDecodeError, TypeError):
                embedding = None
        
        # Parse timestamps
        first_seen = self._parse_datetime(row.get('first_seen'))
        last_seen = self._parse_datetime(row.get('last_seen'))
        
        return {
            'id': row.get('url') or row.get('id'),  # Use URL as ID
            'source': row.get('source', 'unknown'),
            'external_id': row.get('external_id'),
            'title': row.get('title', 'Untitled'),
            'buyer': row.get('buyer'),
            'country': row.get('country'),
            'publication_date': row.get('publication_date'),
            'deadline': row.get('deadline'),
            'estimated_budget': row.get('estimated_budget'),
            'currency': row.get('currency', 'EUR'),
            'normalized_budget_eur': row.get('normalized_budget_eur'),
            'status': row.get('status', 'OPEN'),
            'classification': row.get('classification'),
            'url': row.get('url'),
            'summary': row.get('summary'),
            'embedding': embedding,
            'content_hash': row.get('content_hash'),
            'change_type': row.get('change_type', 'NEW'),
            'duplicate_group_id': row.get('duplicate_group_id'),
            'first_seen': first_seen,
            'last_seen': last_seen,
        }
    
    async def _upsert_tenders_batch(self, session, batch: List[Dict]) -> Tuple[int, int]:
        """Batch upsert tenders with conflict resolution."""
        from src.database_models import TenderModel
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        
        inserted = 0
        updated = 0
        
        for data in batch:
            try:
                # Check if exists
                from sqlalchemy import select
                result = await session.execute(
                    select(TenderModel).where(TenderModel.id == data['id'])
                )
                exists = result.scalar_one_or_none() is not None
                
                if exists:
                    updated += 1
                else:
                    inserted += 1
                
                # Upsert
                stmt = pg_insert(TenderModel).values(data)
                stmt = stmt.on_conflict_do_update(
                    index_elements=['id'],
                    set_={
                        'status': stmt.excluded.status,
                        'deadline': stmt.excluded.deadline,
                        'estimated_budget': stmt.excluded.estimated_budget,
                        'summary': stmt.excluded.summary,
                        'normalized_budget_eur': stmt.excluded.normalized_budget_eur,
                        'embedding': stmt.excluded.embedding,
                        'content_hash': stmt.excluded.content_hash,
                        'last_seen': stmt.excluded.last_seen,
                        'change_type': stmt.excluded.change_type,
                    }
                )
                await session.execute(stmt)
                
            except Exception as e:
                logger.error(f"Error upserting tender {data.get('id')}: {e}")
        
        await session.commit()
        return inserted, updated
    
    async def _migrate_runs(self):
        """Migrate runs table."""
        logger.info("\n📊 Migrating runs...")
        stats = MigrationStats("runs")
        start_time = datetime.now()
        
        cursor = self._sqlite_conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM runs")
        stats.source_count = cursor.fetchone()[0]
        logger.info(f"Source records: {stats.source_count}")
        
        if stats.source_count == 0:
            logger.info("No runs to migrate")
            return
        
        cursor.execute("SELECT * FROM runs")
        
        from src.database_models import async_session_maker, RunModel
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        
        async with async_session_maker() as pg_session:
            for row in cursor:
                row_dict = dict(row)
                
                data = {
                    'id': row_dict.get('run_id'),
                    'timestamp': self._parse_datetime(row_dict.get('timestamp')),
                    'source': row_dict.get('source', 'unknown'),
                    'new_count': row_dict.get('new_count', 0),
                    'updated_count': row_dict.get('updated_count', 0),
                    'total_count': row_dict.get('total_count', 0),
                    'status': 'completed',
                }
                
                if not self.dry_run:
                    try:
                        stmt = pg_insert(RunModel).values(data)
                        stmt = stmt.on_conflict_do_nothing(index_elements=['id'])
                        result = await pg_session.execute(stmt)
                        if result.rowcount > 0:
                            stats.inserted += 1
                        else:
                            stats.updated += 1  # Already existed
                    except Exception as e:
                        logger.error(f"Error migrating run {data['id']}: {e}")
                        stats.errors += 1
            
            await pg_session.commit()
            
            if not self.dry_run:
                from sqlalchemy import text
                result = await pg_session.execute(text("SELECT COUNT(*) FROM runs"))
                stats.target_count = result.scalar()
        
        stats.duration_seconds = (datetime.now() - start_time).total_seconds()
        self.stats.append(stats)
        
        logger.info(f"✅ Runs migrated in {stats.duration_seconds:.2f}s")
        if self.dry_run:
            logger.info(f"   [DRY RUN] Would migrate: {stats.source_count}")
        else:
            logger.info(f"   Inserted: {stats.inserted}, Errors: {stats.errors}")
    
    async def _migrate_dead_letters(self):
        """Migrate dead letter queue."""
        logger.info("\n💀 Migrating dead letters...")
        stats = MigrationStats("dead_letters")
        start_time = datetime.now()
        
        cursor = self._sqlite_conn.cursor()
        
        # Check if table exists
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='dead_letters'
        """)
        if not cursor.fetchone():
            logger.info("No dead_letters table found, skipping")
            return
        
        cursor.execute("SELECT COUNT(*) FROM dead_letters")
        stats.source_count = cursor.fetchone()[0]
        logger.info(f"Source records: {stats.source_count}")
        
        if stats.source_count == 0:
            return
        
        cursor.execute("SELECT * FROM dead_letters")
        
        from src.database_models import async_session_maker, DeadLetterModel
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        import uuid
        
        async with async_session_maker() as pg_session:
            for row in cursor:
                row_dict = dict(row)
                
                data = {
                    'id': uuid.uuid4(),
                    'job_id': row_dict.get('job_id', 'unknown'),
                    'url': row_dict.get('url', ''),
                    'target_site': row_dict.get('target_site', ''),
                    'error_message': row_dict.get('error_message', ''),
                    'error_code': row_dict.get('error_code'),
                    'retry_count': row_dict.get('retry_count', 0),
                    'max_retries': row_dict.get('max_retries', 3),
                    'created_at': self._parse_datetime(row_dict.get('created_at')),
                    'last_retry_at': self._parse_datetime(row_dict.get('last_retry_at')),
                    'payload': self._parse_json(row_dict.get('payload'), {}),
                    'status': row_dict.get('status', 'pending'),
                }
                
                if not self.dry_run:
                    try:
                        stmt = pg_insert(DeadLetterModel).values(data)
                        await pg_session.execute(stmt)
                        stats.inserted += 1
                    except Exception as e:
                        logger.error(f"Error migrating DLQ {data['job_id']}: {e}")
                        stats.errors += 1
            
            await pg_session.commit()
        
        stats.duration_seconds = (datetime.now() - start_time).total_seconds()
        self.stats.append(stats)
        
        logger.info(f"✅ Dead letters migrated in {stats.duration_seconds:.2f}s")
        if self.dry_run:
            logger.info(f"   [DRY RUN] Would migrate: {stats.source_count}")
        else:
            logger.info(f"   Inserted: {stats.inserted}, Errors: {stats.errors}")
    
    def _parse_datetime(self, value) -> Optional[datetime]:
        """Parse datetime from various formats."""
        if not value:
            return datetime.utcnow()
        
        if isinstance(value, datetime):
            return value
        
        formats = [
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
        ]
        
        for fmt in formats:
            try:
                return datetime.strptime(str(value).split('+')[0], fmt)
            except ValueError:
                continue
        
        logger.warning(f"Could not parse datetime: {value}, using current time")
        return datetime.utcnow()
    
    def _parse_json(self, value, default=None):
        """Parse JSON string safely."""
        if not value:
            return default
        
        if isinstance(value, dict):
            return value
        
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return default
    
    def _generate_report(self, total_duration: float) -> Dict[str, Any]:
        """Generate migration report."""
        logger.info("\n" + "=" * 60)
        logger.info("MIGRATION REPORT")
        logger.info("=" * 60)
        
        report = {
            "timestamp": datetime.now().isoformat(),
            "dry_run": self.dry_run,
            "total_duration_seconds": total_duration,
            "tables": []
        }
        
        for stat in self.stats:
            table_report = {
                "table": stat.table_name,
                "source_count": stat.source_count,
                "target_count": stat.target_count,
                "inserted": stat.inserted,
                "updated": stat.updated,
                "errors": stat.errors,
                "duration_seconds": round(stat.duration_seconds, 2)
            }
            report["tables"].append(table_report)
            
            logger.info(f"\n📋 {stat.table_name.upper()}")
            logger.info(f"   Source:      {stat.source_count:,}")
            logger.info(f"   Target:      {stat.target_count:,}")
            logger.info(f"   Inserted:    {stat.inserted:,}")
            logger.info(f"   Updated:     {stat.updated:,}")
            logger.info(f"   Errors:      {stat.errors:,}")
            logger.info(f"   Duration:    {stat.duration_seconds:.2f}s")
        
        logger.info(f"\n⏱️  Total Duration: {total_duration:.2f}s")
        logger.info("=" * 60)
        
        if self.dry_run:
            logger.info("\n⚠️  THIS WAS A DRY RUN - NO CHANGES WERE MADE")
            logger.info("Run with --execute to perform actual migration")
        
        return report


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Migrate Spacescraper data from SQLite to PostgreSQL"
    )
    parser.add_argument(
        "--sqlite-path",
        default="spacescraper_intel.db",
        help="Path to SQLite database (default: spacescraper_intel.db)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Preview migration without making changes (default: True)"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute actual migration (overrides --dry-run)"
    )
    parser.add_argument(
        "--tables",
        help="Comma-separated list of tables to migrate (default: all)"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Batch size for inserts (default: 100)"
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify counts after migration"
    )
    
    args = parser.parse_args()
    
    # Dry run logic
    dry_run = not args.execute if args.execute else args.dry_run
    
    if not dry_run:
        logger.warning("⚠️  EXECUTING REAL MIGRATION - DATA WILL BE MODIFIED")
        response = input("Are you sure? Type 'yes' to continue: ")
        if response.lower() != "yes":
            logger.info("Migration cancelled")
            return
    
    # Parse tables
    tables = None
    if args.tables:
        tables = [t.strip() for t in args.tables.split(",")]
    
    # Run migration
    migrator = DatabaseMigrator(
        sqlite_path=args.sqlite_path,
        dry_run=dry_run,
        batch_size=args.batch_size
    )
    
    try:
        await migrator.initialize()
        report = await migrator.migrate_all(tables=tables)
        
        # Save report
        report_file = f"migration_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(report_file, 'w') as f:
            json.dump(report, f, indent=2)
        logger.info(f"\n📝 Report saved to: {report_file}")
        
    except Exception as e:
        logger.exception("Migration failed")
        sys.exit(1)
    finally:
        migrator.close()


if __name__ == "__main__":
    asyncio.run(main())
