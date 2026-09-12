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
    python migrate_sqlite_to_postgres.py --execute --tables opportunities,runs
    
    # Batch size tuning
    python migrate_sqlite_to_postgres.py --execute --batch-size 500
"""

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
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


# The tables this tool knows how to move. migrate_all dispatches only to
# entries here, so a table with a _migrate_* method and no entry is dead code.
# It is a module constant so a test can assert membership directly -- the
# first attempt grepped migrate_all's source, and both substrings it looked
# for live in the dispatch branch, so it stayed green with the entry removed.
AVAILABLE_TABLES = [
    'opportunities',
    'runs',
    'dead_letters',
    'event_logs',
    'domain_profiles',
]

# Fixed namespace for deriving a dead letter's target id from its source row.
# It must never change: the whole point is that the same source row produces
# the same UUID on a re-run, so a resumed migration updates rather than
# duplicates. Deriving it from a literal keeps it stable without a magic
# constant nobody can trace back to anything.
DEAD_LETTER_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "spacescraper/dead_letters")


@dataclass
class MigrationStats:
    """Statistics for migration tracking."""
    table_name: str
    source_count: int = 0
    target_count: int = 0
    inserted: int = 0
    updated: int = 0
    # Rows the target already had. Counting these as `updated` overstated the
    # work done: ON CONFLICT DO NOTHING writes nothing at all.
    skipped: int = 0
    errors: int = 0
    duration_seconds: float = 0.0


class DatabaseMigrator:
    """
    SQLite to PostgreSQL Migration Tool.
    Supports incremental migration with conflict resolution.
    """
    
    def __init__(
        self,
        sqlite_path: str = "spacescraper_jobs.db",
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
        tables_to_migrate = self._resolve_tables(tables)

        logger.info("=" * 60)
        logger.info(f"Starting migration: SQLite → PostgreSQL")
        logger.info(f"Dry run: {self.dry_run}")
        logger.info(f"Batch size: {self.batch_size}")
        logger.info(f"Tables: {', '.join(tables_to_migrate)}")
        logger.info("=" * 60)
        
        for table in tables_to_migrate:
            if table == 'opportunities':
                await self._migrate_opportunities()
            elif table == 'runs':
                await self._migrate_runs()
            elif table == 'dead_letters':
                await self._migrate_dead_letters()
            elif table == 'event_logs':
                logger.info("Skipping event_logs (optional, high volume)")
            elif table == 'domain_profiles':
                await self._migrate_domain_profiles()

        # Say what is not carried rather than let the absence read as
        # "there was nothing to carry". strategy_observations is the raw
        # evidence behind domain_profiles; losing it costs history, not
        # learned behaviour, because the evaluator leaves an axis alone when
        # it has no observations for it. feedback_items and
        # evaluation_results are likewise not migrated.
        logger.info(
            "Not migrated: strategy_observations, evaluation_results, feedback_items. "
            "Learned domain_profiles are carried; the observation history behind "
            "them is not."
        )
        
        duration = (datetime.now() - start_time).total_seconds()
        
        return self._generate_report(duration)

    # New annotations here use the modern spellings rather than this file's
    # legacy typing.List/Dict/Optional, which ruff's UP rules already flag
    # throughout. Matching the old style would add findings to the gate; the
    # rest of the file is left alone because rewriting it is not this change.
    @staticmethod
    def _resolve_tables(tables: "list[str] | None") -> "list[str]":
        """Decide which tables to migrate, refusing names this tool cannot move.

        An unknown name used to be filtered out silently, so `--tables
        opportunitys` migrated nothing and still printed a success report --
        and that report was the operator's evidence for decommissioning the
        source database.

        The result follows AVAILABLE_TABLES order rather than the caller's:
        the order encodes nothing today, but reading it from a command line
        would make it look as though it did.
        """
        if not tables:
            return list(AVAILABLE_TABLES)

        unknown = [t for t in tables if t not in AVAILABLE_TABLES]
        if unknown:
            raise ValueError(
                f"Unknown table(s): {', '.join(sorted(unknown))}. "
                f"This tool can migrate: {', '.join(AVAILABLE_TABLES)}"
            )
        return [t for t in AVAILABLE_TABLES if t in tables]

    async def _migrate_opportunities(self):
        """Migrate opportunities table with conflict resolution."""
        logger.info("\n📦 Migrating opportunities...")
        stats = MigrationStats("opportunities")
        start_time = datetime.now()
        
        # Get source count
        cursor = self._sqlite_conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM opportunities")
        stats.source_count = cursor.fetchone()[0]
        logger.info(f"Source records: {stats.source_count}")
        
        if stats.source_count == 0:
            logger.info("No opportunities to migrate")
            return
        
        # Fetch and migrate in batches
        cursor.execute("SELECT * FROM opportunities")
        
        batch = []
        processed = 0
        
        from src.database_models import async_session_maker, OpportunityModel
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        
        async with async_session_maker() as pg_session:
            for row in cursor:
                row_dict = dict(row)
                
                # Transform data
                opportunity_data = self._transform_opportunity(row_dict)
                batch.append(opportunity_data)
                
                if len(batch) >= self.batch_size:
                    if not self.dry_run:
                        inserted, updated = await self._upsert_opportunities_batch(pg_session, batch)
                        stats.inserted += inserted
                        stats.updated += updated
                    
                    processed += len(batch)
                    batch = []
                    
                    if processed % 1000 == 0:
                        logger.info(f"  Progress: {processed}/{stats.source_count} ({processed/stats.source_count*100:.1f}%)")
            
            # Process remaining batch
            if batch and not self.dry_run:
                inserted, updated = await self._upsert_opportunities_batch(pg_session, batch)
                stats.inserted += inserted
                stats.updated += updated
            
            processed += len(batch)
            
            # Get target count
            if not self.dry_run:
                result = await pg_session.execute(text("SELECT COUNT(*) FROM opportunities"))
                stats.target_count = result.scalar()
            else:
                stats.target_count = 0
        
        stats.duration_seconds = (datetime.now() - start_time).total_seconds()
        self.stats.append(stats)
        
        logger.info(f"✅ Opportunities migrated in {stats.duration_seconds:.2f}s")
        if self.dry_run:
            logger.info(f"   [DRY RUN] Would insert: {stats.source_count}")
        else:
            logger.info(f"   Inserted: {stats.inserted}, Updated: {stats.updated}")
    
    def _transform_opportunity(self, row: Dict[str, Any]) -> Dict[str, Any]:
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
    
    async def _upsert_opportunities_batch(self, session, batch: List[Dict]) -> Tuple[int, int]:
        """Batch upsert opportunities with conflict resolution."""
        from src.database_models import OpportunityModel
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        
        inserted = 0
        updated = 0
        
        for data in batch:
            try:
                # Check if exists
                from sqlalchemy import select
                result = await session.execute(
                    select(OpportunityModel).where(OpportunityModel.id == data['id'])
                )
                exists = result.scalar_one_or_none() is not None

                # Upsert
                stmt = pg_insert(OpportunityModel).values(data)
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

                # Counted only once the statement has actually run. Crediting
                # the row before the write meant a failure below was reported
                # as a migrated row.
                if exists:
                    updated += 1
                else:
                    inserted += 1

            except Exception as e:
                logger.error(f"Error upserting opportunity {data.get('id')}: {e}")
                # Postgres aborts the whole transaction on a failed statement,
                # so every remaining row in this batch would fail too. The old
                # loop logged each one and committed anyway, turning one bad
                # row into a silently truncated table under a success report.
                # Failing here costs a re-run; continuing cost the data.
                raise

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
                            # ON CONFLICT DO NOTHING wrote nothing. Calling
                            # that an update claimed work the migration did
                            # not do -- the same overstatement as D3, one
                            # table over.
                            stats.skipped += 1
                    except Exception as e:
                        logger.error(f"Error migrating run {data['id']}: {e}")
                        stats.errors += 1
                        # Same aborted-transaction reasoning as the
                        # opportunities batch: nothing after this can land.
                        raise
            
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

        async with async_session_maker() as pg_session:
            for row in cursor:
                row_dict = dict(row)

                data = {
                    'id': self._dead_letter_id(row_dict),
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
                        # Paired with the derived id: together they make a
                        # re-run a no-op instead of a second copy.
                        stmt = stmt.on_conflict_do_nothing(index_elements=['id'])
                        result = await pg_session.execute(stmt)
                        if result.rowcount > 0:
                            stats.inserted += 1
                        else:
                            stats.skipped += 1
                    except Exception as e:
                        logger.error(f"Error migrating DLQ {data['job_id']}: {e}")
                        stats.errors += 1
                        raise
            
            await pg_session.commit()
        
        stats.duration_seconds = (datetime.now() - start_time).total_seconds()
        self.stats.append(stats)
        
        logger.info(f"✅ Dead letters migrated in {stats.duration_seconds:.2f}s")
        if self.dry_run:
            logger.info(f"   [DRY RUN] Would migrate: {stats.source_count}")
        else:
            logger.info(f"   Inserted: {stats.inserted}, Errors: {stats.errors}")
    
    async def _migrate_domain_profiles(self):
        """Carry learned per-domain behaviour across the cutover.

        domain_profiles is not one of the SQLAlchemy models in
        database_models.py -- it belongs to the observation repositories, which
        own their own DDL -- so it is read here as raw SQLite and written
        through PostgresObservationRepository, the thing that creates and
        migrates the target table. Restating its schema in this script would
        make a second place for the column set to drift.

        Without this the cutover silently reset every domain to
        preferred_fetch_tier='http', so every domain already learned to need a
        browser paid for a wasted tier-1 attempt all over again.
        """
        logger.info("\n🎯 Migrating domain profiles...")
        stats = MigrationStats("domain_profiles")
        start_time = datetime.now()

        cursor = self._sqlite_conn.cursor()
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='domain_profiles'
        """)
        if not cursor.fetchone():
            logger.info("No domain_profiles table found, skipping")
            return

        cursor.execute("PRAGMA table_info(domain_profiles)")
        columns = {row[1] for row in cursor.fetchall()}

        cursor.execute("SELECT COUNT(*) FROM domain_profiles")
        stats.source_count = cursor.fetchone()[0]
        logger.info(f"Source records: {stats.source_count}")

        if stats.source_count == 0:
            return

        cursor.execute("SELECT * FROM domain_profiles")
        rows = [dict(row) for row in cursor.fetchall()]

        if self.dry_run:
            stats.duration_seconds = (datetime.now() - start_time).total_seconds()
            self.stats.append(stats)
            logger.info(f"   [DRY RUN] Would migrate: {stats.source_count}")
            return

        from src.config_settings import settings
        from src.domain.models import DomainProfile
        from src.infrastructure.repositories.postgres_observation_repository import (
            PostgresObservationRepository,
        )

        repo = PostgresObservationRepository(str(settings.database.url))
        await repo.initialize()
        try:
            for row in rows:
                domain = row["domain"]
                try:
                    tier, extractor = self._profile_axes(row, columns)
                    # get_or_create first so the row exists, then overwrite it
                    # with the source values -- update_profile has no upsert.
                    await repo.get_or_create_profile(domain)
                    await repo.update_profile(DomainProfile(
                        domain=domain,
                        preferred_fetch_tier=tier,
                        preferred_extraction_strategy=extractor,
                        overlay_id=row.get("overlay_id"),
                        success_rate=row.get("success_rate") or 0.0,
                        total_observations=row.get("total_observations") or 0,
                        avg_latency_ms=row.get("avg_latency_ms") or 0.0,
                        block_rate=row.get("block_rate") or 0.0,
                        last_observed=self._parse_optional_datetime(row.get("last_observed")),
                        profile_version=row.get("profile_version") or 1,
                        throttle_delay_ms=row.get("throttle_delay_ms") or 0.0,
                    ))
                    stats.inserted += 1
                except Exception as e:
                    logger.error(f"Error migrating profile {domain}: {e}")
                    stats.errors += 1
        finally:
            await repo.close()

        stats.duration_seconds = (datetime.now() - start_time).total_seconds()
        self.stats.append(stats)

        logger.info(f"✅ Domain profiles migrated in {stats.duration_seconds:.2f}s")
        logger.info(f"   Inserted: {stats.inserted}, Errors: {stats.errors}")

    @staticmethod
    def _dead_letter_id(row: "dict[str, Any]") -> uuid.UUID:
        """Derive a stable target id from the source row.

        The target's primary key is a UUID the source table does not have, and
        a fresh uuid4() per row made every re-run append the entire dead letter
        table again. Re-running after a partial failure is ordinary operator
        behaviour -- it has to resume, not duplicate.

        The source's own primary key is the natural key when it has one. The
        composite fallback deliberately leaves out mutable columns like
        retry_count and status: those change between runs, and keying off them
        would reintroduce the duplication this exists to prevent.
        """
        source_id = row.get("id")
        if source_id:
            key = f"id:{source_id}"
        else:
            key = "|".join(
                str(row.get(field, "")) for field in ("job_id", "url", "created_at", "error_message")
            )
        return uuid.uuid5(DEAD_LETTER_NAMESPACE, key)

    @staticmethod
    def _profile_axes(row: Dict[str, Any], columns: set) -> Tuple[str, Optional[str]]:
        """Read the two learned axes from either generation of the schema.

        A source database predating the preferred_strategy split holds one
        value from either vocabulary in that single column. The vocabularies
        are imported from the evaluator rather than restated here so this
        cannot drift from what the application believes they are.
        """
        if "preferred_fetch_tier" in columns:
            return row["preferred_fetch_tier"], row.get("preferred_extraction_strategy")

        from src.application.evaluator import EXTRACTION_STRATEGIES, FETCH_TIERS

        legacy = row.get("preferred_strategy")
        return (
            legacy if legacy in FETCH_TIERS else "http",
            legacy if legacy in EXTRACTION_STRATEGIES else None,
        )

    @staticmethod
    def _parse_optional_datetime(value) -> Optional[datetime]:
        """Unlike _parse_datetime, absent means absent.

        _parse_datetime substitutes now() for a missing value, which is right
        for a created_at column and wrong for last_observed -- it would claim
        a domain was just seen when it has never been observed at all.
        """
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None

    def _parse_datetime(self, value) -> Optional[datetime]:
        """Parse datetime from various formats."""
        if not value:
            return datetime.now(tz=timezone.utc)
        
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
        return datetime.now(tz=timezone.utc)
    
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
                "skipped": stat.skipped,
                "errors": stat.errors,
                "duration_seconds": round(stat.duration_seconds, 2)
            }
            report["tables"].append(table_report)

            logger.info(f"\n📋 {stat.table_name.upper()}")
            logger.info(f"   Source:      {stat.source_count:,}")
            logger.info(f"   Target:      {stat.target_count:,}")
            logger.info(f"   Inserted:    {stat.inserted:,}")
            logger.info(f"   Updated:     {stat.updated:,}")
            logger.info(f"   Skipped:     {stat.skipped:,}")
            logger.info(f"   Errors:      {stat.errors:,}")
            logger.info(f"   Duration:    {stat.duration_seconds:.2f}s")
        
        logger.info(f"\n⏱️  Total Duration: {total_duration:.2f}s")
        logger.info("=" * 60)
        
        if self.dry_run:
            logger.info("\n⚠️  THIS WAS A DRY RUN - NO CHANGES WERE MADE")
            logger.info("Run with --execute to perform actual migration")
        
        return report


async def _run_verification() -> int:
    """Run the post-migration integrity checks and return a shell exit code.

    --verify was parsed and never read: the operator asked for a check, got no
    check, and got a success exit code regardless. verify_migration.py already
    held the checks; nothing called it.

    Imported as a module rather than by name so the checks stay patchable in
    tests and so the import cost lands only when the flag is used.
    """
    import verify_migration

    results = await verify_migration.verify_migration()
    return verify_migration.print_results(results)


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Migrate Spacescraper data from SQLite to PostgreSQL"
    )
    parser.add_argument(
        "--sqlite-path",
        default="spacescraper_jobs.db",
        help="Path to SQLite database (default: spacescraper_jobs.db)"
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

        if args.verify:
            if dry_run:
                logger.info("Skipping --verify: a dry run wrote nothing to check.")
            else:
                exit_code = await _run_verification()
                if exit_code != 0:
                    sys.exit(exit_code)

    except Exception as e:
        logger.exception("Migration failed")
        sys.exit(1)
    finally:
        migrator.close()


if __name__ == "__main__":
    asyncio.run(main())
