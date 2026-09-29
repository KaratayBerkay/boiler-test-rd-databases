#!/usr/bin/env bash
# Logging strategy, appended to postgresql.conf at initdb time (NOT passed as `postgres -c ...`: command-line options
# outrank ALTER SYSTEM / postgresql.auto.conf, so they could never be changed at runtime without a restart).
# The replica clones the primary with pg_basebackup and therefore inherits this block. (Citus: coordinator + workers)
set -e
cat >> "$PGDATA/postgresql.conf" <<'CONF'

# ---- rdlab logging strategy ---------------------------------------------------------------------
logging_collector = on                      # PG writes its own files (docker logs keeps only the collector's startup lines)
log_destination = 'stderr,jsonlog'          # text log for humans + JSON lines for log shippers (Vector/Fluent Bit/Loki)
log_directory = '/var/lib/postgresql/log'   # outside PGDATA so base backups do not carry the logs
log_filename = 'postgresql-%Y-%m-%d.log'    # jsonlog uses the same name with .json
log_rotation_age = 1d
log_rotation_size = 100MB
log_truncate_on_rotation = on
log_min_duration_statement = 100ms          # slow-query log; ALTER SYSTEM SET ... = 0 logs everything (overhead measured by the lab)
log_lock_waits = on                         # deadlock_timeout (1 s) waits are logged
log_temp_files = 0                          # every temp file with its size (work_mem too small?)
log_checkpoints = on
log_autovacuum_min_duration = 1s
log_connections = all                       # PG18: receipt, authentication, authorization, setup_durations
log_disconnections = on
log_line_prefix = '%m [%p] %q%u@%d app=%a '
log_error_verbosity = default
auto_explain.log_min_duration = 500ms       # shared_preload_libraries=pg_stat_statements,auto_explain is on the command line
auto_explain.log_format = json
auto_explain.log_nested_statements = on
CONF
echo "logging strategy appended to $PGDATA/postgresql.conf"
