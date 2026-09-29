#!/usr/bin/env python3
"""Kubernetes-native backup add-ons for the k3s runtime: one CronJob per stack that writes a logical/native backup
into the stack's shared backup PersistentVolumeClaim (the same volume the compose drills use), with retention.

Written as standalone kustomize directories under k8s/addons/backup/<stack>/ (namespace = the stack's namespace),
applied on top of the generated stack manifests: they reference the PVCs and Services gen.py creates but never touch
those files, so regenerating the stack manifests does not disturb them and vice versa.

  gen-backup-addons.py              write k8s/addons/backup/<stack>/ for every stack that has a backup strategy
  gen-backup-addons.py postgres ... only these
  k8s/scenarios-backup.sh schedule|backup-now|list|restore-drill|logs-sidecar ...   drives them

Each CronJob runs the engine's own client image (already mirrored in Harbor), connects through the Service DNS name of
the primary, writes /backups/cron/<timestamp>* and keeps the newest KEEP artifacts. Engines whose backup command must
run *inside* the server process (Db2 `db2 backup`, QuestDB CHECKPOINT + file copy) are handled by the scenario script
with `kubectl exec` instead of a CronJob.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
K8S = ROOT / "k8s"
sys.path.insert(0, str(K8S))
import gen  # noqa: E402  (the stack generator: harbor_ref, lab_config, compose_config)

KEEP = 5
SCHEDULE = "0 */6 * * *"
RETENTION = 'ls -1dt /backups/cron/* 2>/dev/null | tail -n +$((KEEP+1)) | xargs -r rm -rf; echo "kept:"; ls -la /backups/cron'


# stack -> (image source, backup PVC, shell script). $TS is the timestamp, /backups the shared volume.
SPECS: dict[str, dict] = {
    "postgres": {"image": "pgvector/pgvector:pg18", "pvc": "pg-backups", "env": {"PGPASSWORD": "labpass"},
                 "script": "pg_dump -h primary -U lab -d lab -Fd -j 2 --compress=zstd -f /backups/cron/$TS && pg_dumpall -h primary -U lab --globals-only -f /backups/cron/$TS/globals.sql"},
    "citus": {"image": "citusdata/citus:latest", "pvc": "citus-backups", "env": {"PGPASSWORD": "labpass"},
              "script": "pg_dump -h coordinator -U lab -d lab -Fd -j 2 --compress=zstd -f /backups/cron/$TS"},
    "mysql": {"image": "mysql:9", "pvc": "my-backups",
              "script": "mysqlsh --uri root:rootpass@source:3306 -- util dump-tables lab --all --outputUrl=/backups/cron/$TS --threads=2 --compression=zstd"},
    "mariadb": {"image": "mariadb:11", "pvc": "mdb-backups",
                "script": "mariadb-dump -h primary -uroot -prootpass --single-transaction --routines --triggers --events --gtid lab | gzip -c > /backups/cron/$TS.sql.gz"},
    "pxc": {"image": "percona/percona-xtradb-cluster:8.4", "pvc": "pxc-backups", "user": 1001,
            "script": "mysqldump -h pxc1 -uroot -prootpass --single-transaction --routines --triggers --events lab | gzip -c > /backups/cron/$TS.sql.gz"},
    "tidb": {"image": "mysql:9", "pvc": "tidb-backups",
             "script": "mysql -h tidb1 -P 4000 -uroot -e \"BACKUP DATABASE lab TO 'local:///backups/cron/$TS'\""},
    "cockroach": {"image": "cockroachdb/cockroach:latest", "pvc": None,
                  "script": "/cockroach/cockroach sql --insecure --host=crdb1:26257 -e \"BACKUP DATABASE lab INTO 'nodelocal://1/cron' WITH revision_history\" && /cockroach/cockroach sql --insecure --host=crdb1:26257 -e \"SHOW BACKUPS IN 'nodelocal://1/cron'\""},
    "yugabyte": {"image": "yugabytedb/yugabyte:latest", "pvc": "yb-backups",
                 "script": "cd /home/yugabyte && postgres/bin/ysql_dump -h yb1 -U yugabyte -d lab -f /backups/cron/$TS.sql && gzip /backups/cron/$TS.sql"},
    "mssql": {"image": "mcr.microsoft.com/mssql/server:2025-latest", "pvc": "mssql-backups", "user": 10001,
              "script": "/opt/mssql-tools18/bin/sqlcmd -C -S mssql1 -U sa -P 'LabPass_2026!' -b -Q \"BACKUP DATABASE [lab] TO DISK = N'/backups/cron/lab-$TS.bak' WITH INIT, COMPRESSION, CHECKSUM\""},
    "oracle": {"image": "gvenzl/oracle-free:23-slim-faststart", "pvc": "oracle-backups", "user": 54321,
               "script": "printf \"CREATE OR REPLACE DIRECTORY rdlab_bk AS '/backups/cron';\\nGRANT READ, WRITE ON DIRECTORY rdlab_bk TO system;\\nEXIT\\n\" | sqlplus -s system/rootpass@oracle/FREEPDB1 && expdp system/rootpass@oracle/FREEPDB1 SCHEMAS=lab DIRECTORY=rdlab_bk DUMPFILE=lab-$TS.dmp LOGFILE=exp-$TS.log"},
    "clickhouse": {"image": "clickhouse/clickhouse-server:latest", "pvc": "ch-backups", "user": 101,
                   "script": "clickhouse-client -h ch1 -u lab --password labpass -q \"BACKUP DATABASE lab TO Disk('backups', 'cron/$TS')\""},
    "crate": {"image": "crate:latest", "pvc": "crate-backups", "user": 1000,
              "script": "crash --hosts crate1:4200 -c \"CREATE REPOSITORY lab_backups TYPE fs WITH (location = '/backups/crate', compress = true)\" || true; crash --hosts crate1:4200 -c \"CREATE SNAPSHOT lab_backups.cron_$(date +%Y%m%d_%H%M%S) ALL WITH (wait_for_completion = true)\" && crash --hosts crate1:4200 -c \"SELECT name, state, finished FROM sys.snapshots ORDER BY finished DESC LIMIT 5\""},
    "monetdb": {"image": "monetdb/monetdb:latest", "pvc": "monetdb-backups",
                "script": "printf 'user=monetdb\\npassword=labpass\\n' > /tmp/.monetdb && DOTMONETDBFILE=/tmp/.monetdb msqldump -h monetdb -d lab | gzip -c > /backups/cron/$TS.sql.gz"},
    "firebird": {"image": "firebirdsql/firebird:5", "pvc": "firebird-backups",
                 "script": "/opt/firebird/bin/gbak -b -g -user lab -password labpass firebird:/var/lib/firebird/data/lab.fdb /backups/cron/lab-$TS.fbk"},
    "h2": {"image": "rdlab/h2:2.3.232", "pvc": "h2-backups",
           "script": "java -cp /opt/h2.jar org.h2.tools.Shell -url jdbc:h2:tcp://h2:9092/lab -user sa -password sa -sql \"BACKUP TO '/backups/cron/lab-$TS.zip'\""},
}
# db2: `db2 backup` only runs inside the instance (scenarios-backup.sh backup-now db2 uses kubectl exec)
# questdb: CHECKPOINT CREATE + copy of the data directory (scenarios-backup.sh backup-now questdb uses kubectl exec)


def cronjob(stack: str, spec: dict) -> dict:
    ns = f"rdlab-{stack}"
    script = f'set -euo pipefail; TS=$(date +%Y%m%d-%H%M%S); mkdir -p /backups/cron; echo "== backup {stack} $TS"; {spec["script"]}; echo "== done"; '
    if spec.get("pvc"):
        script += RETENTION
    container = {"name": "backup", "image": gen.harbor_ref(spec["image"]), "imagePullPolicy": "IfNotPresent",
                 "command": ["bash", "-c", script] if stack not in ("crate", "monetdb", "firebird", "h2") else ["sh", "-c", script],
                 "env": [{"name": "KEEP", "value": str(KEEP)}] + [{"name": k, "value": v} for k, v in (spec.get("env") or {}).items()]}
    if spec.get("user"):
        container["securityContext"] = {"runAsUser": spec["user"]}
    volumes, mounts = [], []
    if spec.get("pvc"):
        volumes.append({"name": "backups", "persistentVolumeClaim": {"claimName": spec["pvc"]}})
        mounts.append({"name": "backups", "mountPath": "/backups"})
    container["volumeMounts"] = mounts
    labels = {"app.kubernetes.io/name": f"backup-{stack}", "app.kubernetes.io/part-of": "rdlab", "rdlab.io/stack": stack, "rdlab.io/addon": "backup"}
    return {"apiVersion": "batch/v1", "kind": "CronJob",
            "metadata": {"name": f"backup-{stack}", "namespace": ns, "labels": labels},
            "spec": {"schedule": SCHEDULE, "concurrencyPolicy": "Forbid", "successfulJobsHistoryLimit": 3, "failedJobsHistoryLimit": 3,
                     "jobTemplate": {"spec": {"backoffLimit": 1, "ttlSecondsAfterFinished": 86400,
                                              "template": {"metadata": {"labels": labels},
                                                           "spec": {"restartPolicy": "Never", "containers": [container], "volumes": volumes}}}}}}


def write(stack: str) -> Path:
    d = K8S / "addons" / "backup" / stack
    d.mkdir(parents=True, exist_ok=True)
    (d / "cronjob.yaml").write_text("# generated by k8s/gen-backup-addons.py -- scheduled backup into the stack's backup PVC\n" +
                                    yaml.dump(cronjob(stack, SPECS[stack]), Dumper=gen.Dumper, sort_keys=False, width=100000))
    (d / "kustomization.yaml").write_text(yaml.dump({"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
                                                     "namespace": f"rdlab-{stack}", "resources": ["cronjob.yaml"]}, Dumper=gen.Dumper, sort_keys=False))
    return d


def main() -> None:
    keys = sys.argv[1:] or sorted(SPECS)
    for k in keys:
        if k not in SPECS:
            sys.exit(f"no backup add-on spec for {k} (db2/questdb are exec-based, sqlite/duckdb are embedded)")
        print(f"wrote {write(k).relative_to(ROOT)}")


if __name__ == "__main__":
    main()
