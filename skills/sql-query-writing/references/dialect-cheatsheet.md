# Dialect cheat-sheet (verified Sept 2026 against the rd-databases lab stacks)

Engines: PG = PostgreSQL 18, CR = CockroachDB 26.2, YB = YugabyteDB 2026.1, MY = MySQL 9.7, MA = MariaDB 11.8, TI = TiDB 8.5,
MS = SQL Server 2025, OR = Oracle 26ai Free (23.26), D2 = Db2 12.1 CE, SL = SQLite 3.45, DU = DuckDB 1.5, CH = ClickHouse 26.8,
FB = Firebird 5, H2 = H2 2.3 (PostgreSQL mode), MO = MonetDB, CT = CrateDB, QD = QuestDB.

## Row limiting
| engine | syntax |
|---|---|
| PG, CR, YB, MY, MA, TI, SL, DU, CH, MO, CT, H2 | `LIMIT n OFFSET m` (MySQL family also `LIMIT m, n`) |
| MS | `ORDER BY ... OFFSET m ROWS FETCH NEXT n ROWS ONLY` (ORDER BY mandatory), `SELECT TOP (n)` |
| OR, D2, FB | `OFFSET m ROWS FETCH NEXT n ROWS ONLY` / `FETCH FIRST n ROWS ONLY` (Firebird also `ROWS m TO n`) |
| QD | `LIMIT n`, `LIMIT m,n` (second number is an absolute upper bound, not a count) |

## JSON value extraction (key `color` of column `attrs`)
| engine | text | number |
|---|---|---|
| PG/CR/YB | `attrs->>'color'` | `(attrs->>'weight')::numeric` |
| MY/TI | `JSON_UNQUOTE(JSON_EXTRACT(attrs,'$.color'))` or `attrs->>'$.color'` | `CAST(JSON_EXTRACT(attrs,'$.weight') AS DECIMAL(12,2))` |
| MA | same as MySQL (JSON is an alias for LONGTEXT with a CHECK) | same |
| MS | `JSON_VALUE(attrs,'$.color')` | `TRY_CAST(JSON_VALUE(attrs,'$.weight') AS DECIMAL(12,2))` |
| OR | `JSON_VALUE(attrs,'$.color')` | `JSON_VALUE(attrs,'$.weight' RETURNING NUMBER)` |
| D2 | `JSON_VALUE(attrs,'$.color')` | `JSON_VALUE(attrs,'$.weight' RETURNING DECIMAL(12,2))` |
| SL | `json_extract(attrs,'$.color')` / `attrs->>'$.color'` | `CAST(json_extract(attrs,'$.weight') AS REAL)` |
| DU | `attrs->>'$.color'` | `CAST(attrs->>'$.weight' AS DOUBLE)` |
| CH | `JSONExtractString(attrs,'color')` (or native JSON type: `attrs.color`) | `JSONExtractFloat(attrs,'weight')` |
| MO | `json.text(json.filter(attrs,'$.color'))` | `json.number(json.filter(attrs,'$.weight'))` |
| CT | `attrs['color']` (OBJECT columns) | `attrs['weight']` |
| FB, H2, QD | no JSON functions (store as text, extract client-side) | -- |

## Date truncation
| engine | day | month |
|---|---|---|
| PG/CR/YB/DU/CT | `DATE_TRUNC('day', ts)` | `DATE_TRUNC('month', ts)` |
| MY/MA/TI | `DATE(ts)` | `DATE_FORMAT(ts, '%Y-%m-01')` |
| MS | `CAST(ts AS DATE)` | `DATEFROMPARTS(YEAR(ts), MONTH(ts), 1)` (2022+: `DATETRUNC(month, ts)`) |
| OR | `TRUNC(ts)` | `TRUNC(ts, 'MM')` |
| D2 | `DATE(ts)` | `DATE_TRUNC('MONTH', ts)` |
| SL | `date(ts)` | `strftime('%Y-%m-01', ts)` |
| CH | `toDate(ts)` | `toStartOfMonth(ts)` |
| FB | `CAST(ts AS DATE)` | build from `EXTRACT(YEAR ...)`/`EXTRACT(MONTH ...)` |
| H2 | `DATE_TRUNC('DAY', ts)` | `DATE_TRUNC('MONTH', ts)` |
| MO | `CAST(ts AS DATE)` | `date_trunc('month', ts)` |
| QD | `timestamp_floor('d', ts)` | `timestamp_floor('M', ts)` (or `SAMPLE BY 1M`) |

## Interval arithmetic (subtract 7 days)
PG/OR/D2/FB-style `ts - INTERVAL '7' DAY` (PG accepts `INTERVAL '7 days'`), MySQL `ts - INTERVAL 7 DAY`, SQL Server `DATEADD(day,-7,ts)`,
SQLite `datetime(ts,'-7 days')`, DuckDB `ts - INTERVAL 7 DAY`, ClickHouse `ts - INTERVAL 7 DAY`, Firebird `DATEADD(-7 DAY TO ts)`,
H2 `DATEADD('DAY',-7,ts)`, QuestDB `dateadd('d',-7,ts)`.

## String aggregation ordered
PG `STRING_AGG(x, ',' ORDER BY x)`; MySQL/MariaDB `GROUP_CONCAT(x ORDER BY x SEPARATOR ',')`; SQL Server `STRING_AGG(x, ',') WITHIN GROUP (ORDER BY x)`;
Oracle/Db2/H2 `LISTAGG(x, ',') WITHIN GROUP (ORDER BY x)`; SQLite `GROUP_CONCAT(x, ',' ORDER BY x)` (3.44+); DuckDB `STRING_AGG(x, ',' ORDER BY x)`;
ClickHouse `arrayStringConcat(arraySort(groupArray(x)), ',')`; Firebird `LIST(x, ',')` (no ORDER BY); MonetDB `GROUP_CONCAT(x, ',')`; CrateDB `ARRAY_TO_STRING(ARRAY_AGG(x), ',')`; QuestDB `string_agg(x, ',')`.

## Median / percentile
PG/CR(`::FLOAT8` only)/OR/D2/MS(window form only) `PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY v)`; MariaDB window form only; MySQL/TiDB: none (use window trick);
DuckDB `quantile_cont(v, 0.5)`; ClickHouse `quantileExact(0.5)(v)`; MonetDB `quantile(v, 0.5)`; CrateDB `PERCENTILE(v, 0.5)`; QuestDB `approx_percentile(v, 0.5, 5)`; SQLite/Firebird/H2: none in core.

## Full-text search
PG `to_tsvector('english', d) @@ plainto_tsquery('english', 'w1 w2')` + GIN index; MySQL/MariaDB/TiDB(no) `MATCH(d) AGAINST('w1 w2')` + FULLTEXT index;
SQL Server `CONTAINS(d, 'w1 AND w2')` + full-text catalog; Oracle `CONTAINS(d, 'w1 AND w2') > 0` + CTXSYS.CONTEXT index; SQLite FTS5 virtual table `MATCH`;
DuckDB `fts` extension `match_bm25`; ClickHouse `hasToken(d,'w1')` (+ text index); CrateDB `MATCH(idx, 'w1 w2')`; CockroachDB `to_tsvector @@ plainto_tsquery` (24.1+); Firebird/H2/MonetDB/QuestDB/Db2 CE: LIKE fallback.

## Booleans
Native BOOLEAN: PG family, MySQL (TINYINT(1)), MariaDB, SQLite (0/1 integers), DuckDB, ClickHouse (Bool), Firebird 3+, H2, MonetDB, CrateDB, QuestDB, Oracle 23ai (new), Db2.
SQL Server: `BIT` (compare with `= 1`). Bind Python `bool` as int for MySQL/SQL Server drivers.

## Row-value comparison `(a, b) > (x, y)`
Supported: PG, CR, YB, MY, MA, TI, SL, DU, CH, H2. Not supported: MS, OR (only `=`/`IN`), D2 (limited), FB, MO, CT, QD.

## Isolation levels (what `SET TRANSACTION ISOLATION LEVEL` accepts)
PG: all four keywords accepted, RU behaves as RC. MySQL/MariaDB/TiDB: all four (TiDB: RC + "REPEATABLE READ"=snapshot). SQL Server: RU/RC/RR/SNAPSHOT/SERIALIZABLE.
Oracle: RC/SERIALIZABLE (+READ ONLY). Db2: UR/CS/RS/RR via `SET CURRENT ISOLATION`. SQLite: serializable always. DuckDB: snapshot (no statement). ClickHouse/CrateDB/QuestDB: no transactions.
CockroachDB: SERIALIZABLE default, READ COMMITTED (23.2+), REPEATABLE READ (24.3+). Firebird: `SNAPSHOT`, `SNAPSHOT TABLE STABILITY`, `READ COMMITTED`.
