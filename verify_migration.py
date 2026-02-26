#!/usr/bin/env python3
"""
Author: Georgios-Chrysovalantis Chatzivantsidis
Project: Spacescraper (Migration Verification)
Role: Verify data integrity after SQLite → PostgreSQL migration.
"""

import asyncio
import sys
from typing import Dict, Any, List
from dataclasses import dataclass

from sqlalchemy import text
from src.database_models import async_session_maker, init_db


@dataclass
class VerificationResult:
    table: str
    check: str
    passed: bool
    details: str


async def verify_migration() -> List[VerificationResult]:
    """Run verification checks on migrated data."""
    results = []
    
    print("🔍 Verifying PostgreSQL migration...\n")
    
    await init_db()
    
    async with async_session_maker() as session:
        # Check 1: Tables exist
        for table in ['tenders', 'runs', 'dead_letters']:
            try:
                result = await session.execute(text(f"SELECT COUNT(*) FROM {table}"))
                count = result.scalar()
                results.append(VerificationResult(
                    table=table,
                    check="table_exists",
                    passed=True,
                    details=f"Table exists with {count:,} rows"
                ))
            except Exception as e:
                results.append(VerificationResult(
                    table=table,
                    check="table_exists",
                    passed=False,
                    details=str(e)
                ))
        
        # Check 2: Tender data integrity
        result = await session.execute(text("""
            SELECT 
                COUNT(*) as total,
                COUNT(content_hash) as with_hash,
                COUNT(embedding) as with_embedding,
                SUM(CASE WHEN status = 'UNCERTAIN' THEN 1 ELSE 0 END) as uncertain
            FROM tenders
        """))
        row = result.mappings().first()
        
        if row:
            results.append(VerificationResult(
                table="tenders",
                check="data_integrity",
                passed=row['with_hash'] == row['total'],
                details=f"Total: {row['total']:,}, With hash: {row['with_hash']:,}, "
                       f"With embedding: {row['with_embedding']:,}, Uncertain: {row['uncertain']:,}"
            ))
        
        # Check 3: No duplicate URLs (primary key constraint)
        result = await session.execute(text("""
            SELECT url, COUNT(*) as cnt 
            FROM tenders 
            GROUP BY url 
            HAVING COUNT(*) > 1
        """))
        duplicates = result.fetchall()
        results.append(VerificationResult(
            table="tenders",
            check="no_duplicates",
            passed=len(duplicates) == 0,
            details=f"Found {len(duplicates)} duplicate URLs" if duplicates else "No duplicates found"
        ))
        
        # Check 4: Index health
        result = await session.execute(text("""
            SELECT indexname, indexdef 
            FROM pg_indexes 
            WHERE tablename IN ('tenders', 'runs', 'dead_letters')
            ORDER BY tablename, indexname
        """))
        indexes = result.fetchall()
        results.append(VerificationResult(
            table="all",
            check="indexes",
            passed=len(indexes) >= 5,
            details=f"Found {len(indexes)} indexes"
        ))
        
        # Check 5: Recent tenders have timestamps
        result = await session.execute(text("""
            SELECT COUNT(*) 
            FROM tenders 
            WHERE first_seen IS NULL OR last_seen IS NULL
        """))
        null_timestamps = result.scalar()
        results.append(VerificationResult(
            table="tenders",
            check="timestamps",
            passed=null_timestamps == 0,
            details=f"Tenders with null timestamps: {null_timestamps}"
        ))
    
    return results


def print_results(results: List[VerificationResult]):
    """Print verification results."""
    print("=" * 60)
    print("VERIFICATION RESULTS")
    print("=" * 60)
    
    passed = 0
    failed = 0
    
    for r in results:
        status = "✅ PASS" if r.passed else "❌ FAIL"
        print(f"\n{status} | {r.table}.{r.check}")
        print(f"       {r.details}")
        
        if r.passed:
            passed += 1
        else:
            failed += 1
    
    print("\n" + "=" * 60)
    print(f"Total: {len(results)} | Passed: {passed} | Failed: {failed}")
    print("=" * 60)
    
    if failed > 0:
        print("\n⚠️  Some checks failed. Review the migration.")
        return 1
    else:
        print("\n✅ All checks passed! Migration successful.")
        return 0


async def main():
    """Main entry point."""
    try:
        results = await verify_migration()
        return print_results(results)
    except Exception as e:
        print(f"❌ Verification failed: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
