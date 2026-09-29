"""QuestDB backup strategy (QuestDB 8.x): CHECKPOINT CREATE -> copy the server root -> CHECKPOINT RELEASE.
Restore = copy into a fresh root, `touch _restore`, start QuestDB (the same major version). The image has no tar,
so the copy is a plain `cp -a` into the shared backup volume."""
from __future__ import annotations

import time

from .. import dockerctl
from ..config import StackConfig
from ..engines import Engine
from .common import Ctx, Strategy, StrategyResult


def s_checkpoint(ctx: Ctx) -> StrategyResult:
    r = ctx.res
    root = ctx.bconf.get("root", "/var/lib/questdb")
    tarball = f"{ctx.backup_dir}/questdb-full"
    ctx.sh("prepare", f"rm -rf {tarball} && mkdir -p {tarball}")
    t0 = time.perf_counter()
    ctx.sql_step("CHECKPOINT CREATE", ["CHECKPOINT CREATE"])
    try:
        rows = ctx.sql_step("checkpoint_status()", ["SELECT * FROM checkpoint_status()"], fetch_last=True)
        r.extra["checkpoint_status"] = str(rows[:2])[:200] if rows else None
        ctx.sh("cp -a server root (during checkpoint)", f"cd {root} && for d in .checkpoint conf db snapshot public; do [ -e $d ] && cp -a $d {tarball}/; done; true")
    finally:
        ctx.sql_step("CHECKPOINT RELEASE", ["CHECKPOINT RELEASE"])
    r.backup_seconds = round(time.perf_counter() - t0, 2)
    r.backup_bytes = ctx.size(tarball)
    project, image = ctx.cfg.project, ctx.bconf.get("image") or ctx.cfg.image
    port = int(ctx.bconf.get("restore_port", 18813))
    name = f"{project}-restore"
    script = f"""set -e
cp -a {tarball}/. /var/lib/questdb/
touch /var/lib/questdb/_restore
chown -R questdb:questdb /var/lib/questdb
exec /docker-entrypoint.sh
"""
    dockerctl.scratch_run(name, image, project=project, volumes=[f"{dockerctl.volume_name(project, ctx.bconf.get('volume', 'questdb-backups'))}:{ctx.backup_dir}"],
                          ports=[f"{port}:8812"], env={"QDB_PG_USER": ctx.primary.user, "QDB_PG_PASSWORD": ctx.primary.password},
                          command=["bash", "-c", script])
    tgt = ctx.alt_target(port=port, host="127.0.0.1", name="restore")
    try:
        t0 = time.perf_counter()
        deadline = t0 + 240
        last = None
        while time.perf_counter() < deadline:
            if not dockerctl.container_running(name):
                raise RuntimeError(f"scratch container exited: {dockerctl.container_logs(name, 30)[-1500:]}")
            try:
                c = ctx.engine.connect(tgt, timeout=3)
                try:
                    c.execute("SELECT count() FROM customers", rendered=True)
                    break
                finally:
                    c.close()
            except Exception as e:  # noqa: BLE001
                last = str(e)[:120]
                time.sleep(1.0)
        else:
            raise TimeoutError(f"restored QuestDB not ready: {last}")
        r.restore_seconds = round(time.perf_counter() - t0, 2)
        r.verify = ctx.verify(tgt)
        r.extra["restore_log"] = [l for l in dockerctl.container_logs(name, 200).splitlines() if "restore" in l.lower() or "checkpoint" in l.lower()][:5]
    finally:
        dockerctl.rm_container(name)
    r.notes = "consistent copy (cp -a of db/, conf/, .checkpoint/) while CHECKPOINT holds the table versions; the _restore trigger file makes the new instance finalize the checkpoint on start"
    return r


def strategies(engine: Engine, cfg: StackConfig) -> list[Strategy]:
    return [Strategy("checkpoint", "physical", "CHECKPOINT CREATE + tar + _restore", "checkpoint-consistent file copy restored in a scratch container", s_checkpoint)]
