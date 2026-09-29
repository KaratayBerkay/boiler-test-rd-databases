# Time to Ready

Deploy → wait for every rollout & job → tear down, one stack at a time.

**17 / 17 Ready**

## Single node

| Engine | Topology | Pods | Ready |
|---|---|---|---|
| H2 | embedded SQL over PG wire | 1/1 | 15s |
| QuestDB | time-series, single | 1/1 | 20s |
| Firebird | single | 1/1 | 22s |
| MonetDB | column-store, single | 1/1 | 26s |
| Oracle ✦ | 23ai Free, single | 1/1 | 30s |

## Primary + replica + proxy

| Engine | Topology | Pods | Ready |
|---|---|---|---|
| PostgreSQL | primary · replica · PgBouncer · HAProxy | 4 pods | 28s |
| MariaDB ✦ | primary · replica · MaxScale | 3 pods | 40s |
| ClickHouse | 2 replicas · Keeper | 3 pods | 51s |
| MySQL | source · GTID replica · ProxySQL | 4 pods | 69s |
| SQL Server | 2-replica read-scale AG | 3 pods | 69s |

## Multi-node clusters

| Engine | Topology | Pods | Ready |
|---|---|---|---|
| CockroachDB | 3 nodes · HAProxy | 5 pods | 28s |
| CrateDB | 3-node distributed | 3 pods | 30s |
| YugabyteDB | 3 nodes, RF3 | 4 pods | 65s |
| TiDB | PD · 3× TiKV · 2× TiDB | 7 pods | 86s |
| Citus | coordinator · 5×2 workers · 3 PgBouncer | 16 pods | 122s |
| Percona XtraDB | 3-node Galera · HAProxy | 4 pods | 128s |
| Db2 | 12.1 CE, privileged, single | 1/1 | 159s |

---

**Notes:**

- Single k3d node, local-path storage, all images from Harbor.
- ✦ MariaDB and Oracle needed generator fixes before they came up (see [explanation-failures.md](explanation-failures.md)).
- SQLite and DuckDB are embedded (in-process, no containers), so they have no manifests; 17 is the full containerized set.
