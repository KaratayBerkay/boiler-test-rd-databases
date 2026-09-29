# Poolers and proxies used in the lab (working configs live in `stacks/*/`)

## PgBouncer (`stacks/postgres/compose.yaml`, image edoburu/pgbouncer)
```
DB_HOST=primary DB_USER=lab DB_PASSWORD=labpass DB_NAME=lab
AUTH_TYPE=scram-sha-256 POOL_MODE=transaction
MAX_CLIENT_CONN=5000 DEFAULT_POOL_SIZE=20 MIN_POOL_SIZE=5 RESERVE_POOL_SIZE=5
MAX_PREPARED_STATEMENTS=200 SERVER_RESET_QUERY="DISCARD ALL"
IGNORE_STARTUP_PARAMETERS=extra_float_digits,options
```
- Admin console: `psql -p 16432 -U lab pgbouncer -c 'SHOW POOLS'` / `SHOW STATS` / `SHOW CLIENTS` / `PAUSE` / `RESUME`.
- Transaction mode = one server connection per *transaction*; session state is not preserved. Statement mode is for autocommit-only apps.
- Single-threaded: run N instances on the same port with `so_reuseport = 1` for more than ~20-30k QPS.

## HAProxy (PostgreSQL & CockroachDB & Galera stacks)
```
listen pg_read
    bind *:5001
    balance roundrobin
    option pgsql-check user lab          # protocol-level check; does NOT know primary vs replica
    server primary primary:5432 check
    server replica replica:5432 check
```
- Round-robin TCP only. For primary detection use an HTTP check against Patroni (`/primary`, `/replica`) or CockroachDB `/health?ready=1` (used in the lab).

## ProxySQL 3 (`stacks/mysql/proxysql/proxysql.cnf`)
- `mysql_servers`: hostgroup 10 = writer, 20 = readers; `mysql_replication_hostgroups` with `check_type="read_only"` moves servers between groups automatically based on `@@read_only` (the replica has `super_read_only=ON`).
- `mysql_query_rules`: `^SELECT .* FOR UPDATE` -> 10, `^SELECT` -> 20; everything else -> user's `default_hostgroup` (10). `transaction_persistent=1` keeps a transaction on one backend.
- Needs a `monitor` user with `REPLICATION CLIENT, PROCESS, SELECT`; MySQL 9 removed `mysql_native_password`, ProxySQL 3 handles `caching_sha2_password` (set `mysql-default_authentication_plugin`).
- Admin: `mysql -h127.0.0.1 -P16032 -uadmin -padmin` -> `SELECT * FROM stats_mysql_connection_pool; SELECT * FROM stats_mysql_query_digest ORDER BY sum_time DESC LIMIT 10;`
- Raise `RLIMIT_NOFILE` (`ulimits: nofile: 65536`) or it logs a warning and caps connections.

## MaxScale 24.02 (`stacks/mariadb/maxscale/maxscale.cnf`)
- `mariadbmon` monitor with `auto_failover=true`, `auto_rejoin=true`, `failcount=3`, `monitor_interval=1000ms` promoted the replica 2.5-3.0 s after the primary was killed (measured 2471 ms and 3038 ms to first successful write through the new primary).
- `readwritesplit` router: reads go to slaves, writes and transactions to the master; `transaction_replay=true` and `master_reconnection=true` hide failovers from clients; `causal_reads=local` if you need read-your-writes on replicas.
- REST API: `curl -u admin:mariadb http://127.0.0.1:18989/v1/servers`; `maxctrl list servers`.

## pgcat / PgPool-II / Supavisor / Odyssey (not exercised, notes)
- pgcat: `pgcat.toml` pools with `primary`/`replica` roles, `query_parser_enabled = true` routes SELECT to replicas, sharding by hash; Rust, multi-threaded.
- PgPool-II: `backend_hostname0/1`, `load_balance_mode = on`, `sr_check_user`; heavier, watch for `statement_level_load_balance`.
- Supavisor: Elixir, tenant-aware, transaction mode, used by Supabase.
- Odyssey: Yandex, multi-threaded PgBouncer alternative.

## Client pools (Python)
```python
from psycopg_pool import ConnectionPool
pool = ConnectionPool(conninfo, min_size=4, max_size=16, max_waiting=64, max_lifetime=1800, max_idle=300, timeout=5)
with pool.connection() as conn: ...

import asyncpg
pool = await asyncpg.create_pool(dsn, min_size=4, max_size=16, max_inactive_connection_lifetime=300, command_timeout=30)

from sqlalchemy import create_engine
engine = create_engine(url, pool_size=10, max_overflow=5, pool_timeout=5, pool_recycle=1800, pool_pre_ping=True)
```
Sizing rule of thumb: `pool_size = (db_cores * 2) / app_instances`, `max_overflow` small, `pool_timeout` short (fail fast and shed load rather than pile up).
