"""auto_migrate.py 的端到端测试：在临时副本上跑，真实数据只读。

覆盖：预演、正常迁移、重复执行（幂等）、DSH 在运行时拒绝、日志与结果文件。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from dshmigrator import engine  # noqa: E402
from tools import demo_home  # noqa: E402
from tools.rehearse import check, _failures  # noqa: E402

PY = sys.executable
CONFIG = HERE / "auto-migrate-config.json"   # 仅作为回退；测试会生成自己的配置


def run_auto(home: Path, target: Path, *extra: str,
             config: Path | None = None, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [PY, str(HERE / "auto_migrate.py"), "--config", str(config or CONFIG),
         "--home", str(home), "--to", str(target), *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT), env=env,
    )


def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="dsh-automig-test-"))
    home = scratch / ".dsh"
    target = scratch / "target-workspace"
    target.mkdir(parents=True)
    # 测试用独立的日志/结果文件，绝不碰生产的那两个（否则会把真实运行记录删掉）
    log = scratch / "auto-migrate-test.log"
    result_file = scratch / "auto-migrate-test-result.json"

    try:
        print("[1] 生成合成数据并据此写出临时配置")
        summary = demo_home.build(home, base_dir=scratch / "projects")
        ids = [summary["parent"]] + list(summary["subagents"])
        parent_cwd = next(s["cwd"] for s in summary["sessions"] if s["id"] == summary["parent"])
        test_cfg = {"home": str(home), "to": str(target), "parent": summary["parent"],
                    "sessionIds": ids, "log": str(log), "result": str(result_file),
                    "taskName": "DSH-Migrator-Test"}
        test_cfg_path = scratch / "auto-migrate-test-config.json"
        test_cfg_path.write_text(json.dumps(test_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        check((home / "sessions").is_dir(), "合成数据就绪")
        check(len(ids) >= 3, f"待迁移 {len(ids)} 个会话（1 个主会话 + {len(summary['subagents'])} 个子代理）")
        present = {s.id for s in engine.list_sessions(home)}
        missing = [i for i in ids if i not in present]
        check(not missing, f"配置里的 {len(ids)} 个会话都能被列出", f"缺失 {missing}")

        print("\n[2] 预演（不应写任何东西）")
        before_db = engine.read_bytes(engine.storage_file(home))
        r = run_auto(home, target, "--now", "--dry-run", config=test_cfg_path)
        check(r.returncode == 0, "预演退出码 0", r.stderr.strip()[:120])
        check("将移动" in r.stdout, "预演打印了计划")
        check(engine.read_bytes(engine.storage_file(home)) == before_db, "预演未改动工作区表")
        check(not result_file.exists(), "预演未写结果文件")

        print("\n[3] DSH 正在运行时必须拒绝（保护生效）")
        r = run_auto(home, target, "--now", config=test_cfg_path)
        check(r.returncode != 0, "带 --now 但不带 --force 时被拒绝")
        check("正在运行" in (r.stdout + r.stderr), "拒绝原因指向 DSH 正在运行")
        check(engine.latest_backup(home) is None, "被拒绝时没有产生任何备份/改动")

        print("\n[4] 立即执行（--force 跳过运行检查，仅测试用）")
        r = run_auto(home, target, "--now", "--force", config=test_cfg_path)
        check(r.returncode == 0, "迁移退出码 0", (r.stderr or r.stdout).strip()[-200:])
        moved = [s for s in engine.list_sessions(home) if engine.same_path(s.cwd, str(target))]
        check(len(moved) == len(ids), f"目标工作区下有 {len(moved)} 个会话（期望 {len(ids)}）")
        check(all(s.is_subagent or s.id == summary["parent"] for s in moved),
              "迁移的就是主会话及其子代理")
        check(engine.latest_backup(home) is not None, "备份已生成")
        check(log.exists(), "日志文件已写出")
        check(result_file.exists(), "结果文件已写出")
        if result_file.exists():
            result = json.loads(result_file.read_text(encoding="utf-8"))
            check(result["moved"] == len(ids) and result["verify_failures"] == 0,
                  "结果文件记录迁移数与复核通过", json.dumps(result, ensure_ascii=False))
        text = log.read_text(encoding="utf-8")
        check("复核" in text and "✖" not in text, "日志里复核全部通过")

        # 只迁走了指定的那些；原工作区里其余会话必须原封不动
        left = [s for s in engine.list_sessions(home) if engine.same_path(s.cwd, parent_cwd)]
        expected_left = [s for s in summary["sessions"]
                         if engine.same_path(s["cwd"], parent_cwd) and s["id"] not in ids]
        check(len(left) == len(expected_left),
              f"原工作区剩 {len(left)} 个会话（应为 {len(expected_left)}）",
              ", ".join(s.display_title[:16] for s in left))
        check(all(s.id not in ids for s in left), "留下的确实不是被迁移的那些")

        print("\n[5] 重复执行（幂等）")
        r = run_auto(home, target, "--now", "--force", config=test_cfg_path)
        check(r.returncode != 0, "再次执行会被拒绝（已经都在目标工作区）")
        check("都已经在目标工作区" in (r.stdout + r.stderr), "拒绝原因明确")

        print("\n[6] 回滚")
        result = json.loads(result_file.read_text(encoding="utf-8"))
        engine.rollback(Path(result["backup"]), allow_running=True)
        back = [s for s in engine.list_sessions(home) if engine.same_path(s.cwd, str(target))]
        check(len(back) == 0, "回滚后目标工作区已清空")
        restored = {s.id for s in engine.list_sessions(home)}
        check(set(ids) <= restored, "回滚后 14 个会话都回到原工作区")
    finally:
        engine.rmtree(scratch)          # 测试用的日志/结果都在 scratch 里，一并清掉

    print()
    if _failures:
        print(f"auto_migrate 测试失败：{len(_failures)} 项 ❌")
        for name in _failures:
            print(f"  - {name}")
        return 1
    print("auto_migrate 测试全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
