# Spacescraper Migration Guide

SQLite → PostgreSQL Migration για Enterprise Architecture

## 📋 Prerequisites

```bash
# 1. Start PostgreSQL (με Docker)
docker run -d \
  --name ss-postgres \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=spacescraper \
  -p 5432:5432 \
  postgres:16-alpine

# 2. Εγκατάσταση dependencies
pip install -r requirements.txt

# 3. Επιβεβαίωση SQLite database υπάρχει
ls -la spacescraper_intel.db
```

## 🧪 Step 1: Dry Run (Preview)

```bash
python migrate_sqlite_to_postgres.py --dry-run
```

Αυτό θα δείξει:
- Πόσα records υπάρχουν σε κάθε table
- Τι θα γίνει migrate
- Χωρίς να κάνει πραγματικές αλλαγές

## ✅ Step 2: Execute Migration

```bash
python migrate_sqlite_to_postgres.py --execute
```

Θα σου ζητήσει confirmation:
```
⚠️  EXECUTING REAL MIGRATION - DATA WILL BE MODIFIED
Are you sure? Type 'yes' to continue: yes
```

## 🔍 Step 3: Verify

```bash
python verify_migration.py
```

Ελέγχει:
- ✅ Tables exist
- ✅ No duplicate URLs
- ✅ Data integrity (hashes, embeddings)
- ✅ Indexes created
- ✅ Timestamps valid

## 📊 Migration Options

### Specific Tables Only
```bash
python migrate_sqlite_to_postgres.py --execute --tables opportunities,runs
```

### Custom Batch Size (για μεγάλα datasets)
```bash
python migrate_sqlite_to_postgres.py --execute --batch-size 500
```

### Custom SQLite Path
```bash
python migrate_sqlite_to_postgres.py --execute --sqlite-path /path/to/my.db
```

## 🐛 Troubleshooting

### Error: "SQLite database not found"
```bash
# Έλεγξε το path
ls -la spacescraper_intel.db

# Ή χρησιμοποίησε custom path
python migrate_sqlite_to_postgres.py --sqlite-path ./exports/spacescraper_intel.db
```

### Error: "PostgreSQL connection failed"
```bash
# Έλεγξε ότι τρέχει
pg_isready -h localhost -p 5432

# Ή χρησιμοποίησε το Docker Compose
docker-compose -f docker-compose.enterprise.yml up -d postgres
```

### Error: "Duplicate key value"
Το migration χρησιμοποιεί UPSERT (ON CONFLICT UPDATE), οπότε:
- Τα duplicates θα γίνουν UPDATE αντί για error
- Το πρώτο opportunity κρατάει το ID
- Τα υπόλοιπα ενημερώνουν τα existing records

## 📁 Output Files

Μετά το migration θα έχεις:
```
├── migration_20240115_143022.log       # Detailed logs
├── migration_report_20240115_143022.json  # Statistics
└── spacescraper_intel.db                # Original (διατηρείται)
```

## 🔄 Rollback

Το migration είναι **non-destructive**:
- Το SQLite αρχείο διατηρείται
- Μπορείς να το ξανατρέξεις (UPSERT behavior)

Για rollback:
```bash
# 1. Καθάρισε PostgreSQL
docker exec ss-postgres psql -U postgres -c "DROP DATABASE spacescraper;"
docker exec ss-postgres psql -U postgres -c "CREATE DATABASE spacescraper;"

# 2. Τρέξε ξανά migration αν χρειάζεται
python migrate_sqlite_to_postgres.py --execute
```

## 🚀 Post-Migration: Start Enterprise Stack

```bash
# 1. Environment variables
export DB_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/spacescraper
export FEATURE_POSTGRES_DB=true

# 2. Verify connection
python -c "import asyncio; from src.postgres_tracker import postgres_tracker; asyncio.run(postgres_tracker.initialize()); print('✅ PostgreSQL connected')"

# 3. Run with new stack
python worker_scraper.py &
python worker_processor.py &
```

## 📈 Performance Comparison

| Operation | SQLite | PostgreSQL | Improvement |
|-----------|--------|------------|-------------|
| Concurrent writes | 1 | 20+ (pool) | 20x |
| Query complex aggregations | Slow | Fast | 5-10x |
| Concurrent reads | Shared lock | MVCC | Unlimited |
| Connection overhead | File I/O | Connection pool | - |

## 🎯 Migration Checklist

- [ ] Backup SQLite database
- [ ] Start PostgreSQL
- [ ] Run dry run migration
- [ ] Review dry run output
- [ ] Execute real migration
- [ ] Run verification script
- [ ] Update environment variables
- [ ] Test workers with PostgreSQL
- [ ] Monitor logs for errors
- [ ] ✅ Done!
