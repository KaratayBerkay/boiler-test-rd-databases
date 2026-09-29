#!/usr/bin/env bash
# allow streaming replication connections from the replica containers (trust auth, lab only)
set -e
echo "host replication all all trust" >> "$PGDATA/pg_hba.conf"
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements" || true
