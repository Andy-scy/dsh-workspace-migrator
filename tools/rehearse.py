"""端到端自检：把真实 DSH 数据整份复制到临时目录，在副本上跑完整流程。

覆盖：列出 → 建立计划 → 迁移 → 复核 → 回滚 → 回滚后字节比对。
真实数据全程只读。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from dshmigrator import engine  # noqa: E402
from tools import demo_home  # noqa: E402

PASS, FAIL = "  ✔", "  ✖"
_failures: list[str] = []


def check(cond: bool, label: str, extra: str = "") -> bool:
    print(f"{PASS if cond else FAIL} {label}" + (f"  {extra}" if extra else ""))
    if not cond:
        _failures.append(label)
    return cond


def copy_subset(src_home: Path, dst_home: Path) -> None:
    dst_home.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src_home / "sessions", dst_home / "sessions")
    (dst_home / "storages").mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_home / "storages" / "workspace.json", dst_home / "storages" / "workspace.json")
    pc_src = src_home / "storages" / "session_projcache"
    if pc_src.is_dir():
        shutil.copytree(pc_src, dst_home / "storages" / "session_projcache")
    if (src_home / "usage-stats" / "index-v1.json").exists():
        (dst_home / "usage-stats").mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_home / "usage-stats" / "index-v1.json", dst_home / "usage-stats" / "index-v1.json")


def tail_after_first_frame(data: bytes) -> tuple[bytes, bytes]:
    frames, _ = engine.scan_zstd_frames(data)
    return data[frames[0][0]:frames[0][1]], data[frames[0][1]:]


def run(use_real: bool = False) -> int:
    scratch = Path(tempfile.mkdtemp(prefix="dsh-migrate-selftest-"))
    home = scratch / ".dsh"
    try:
        print("[1] 准备数据")
        if use_real:
            real_home = engine.default_home()
            print(f"  使用真实 DSH 目录（只读复制，绝不在原件上写）：{real_home}")
            copy_subset(real_home, home)
            check((home / "sessions").is_dir(), "副本 sessions 就绪")
        else:
            summary = demo_home.build(home, base_dir=scratch / "projects")
            print(f"  使用合成数据（不含任何真实信息）：{home}")
            check((home / "sessions").is_dir(), "合成数据 sessions 就绪")
            check(len(summary["sessions"]) >= 5,
                  f"合成会话 {len(summary['sessions'])} 个（含子代理 {len(summary['subagents'])} 个）")

        sessions = engine.list_sessions(home)
        check(len(sessions) > 0, f"读到 {len(sessions)} 个会话")
        workspaces, _ = engine.list_workspaces(home)
        check(len(workspaces) > 0, f"读到 {len(workspaces)} 个工作区")
        titled = [s for s in sessions if s.title]
        check(len(titled) > 0, f"{len(titled)} 个会话读到标题（来自 projcache）")

        # 选两个有标题、且 cwd 存在的不同会话
        picked = [s for s in sessions if s.title and s.cwd_ok][:2]
        if len(picked) < 2:
            picked = [s for s in sessions if s.cwd_ok][:2]
        if len(picked) < 2:
            print("可用会话不足，跳过")
            return 1
        ids = [s.id for s in picked]
        print("    选中：")
        for s in picked:
            print(f"      {s.display_title}   ({s.cwd})")

        print("\n[2] 建立计划")
        target = scratch / "target-workspace"
        target.mkdir()
        plan = engine.build_plan(home, ids, str(target))
        check(len(plan.moves) == len(ids), f"计划包含 {len(plan.moves)} 个移动")
        check(engine.canonical(str(target)) == plan.target, f"目标规范化：{plan.target}")
        for change in plan.db_changes:
            print(f"      · {change}")

        print("\n[3] 执行迁移（副本）")
        originals = {m.session_id: engine.read_bytes(m.from_log) for m in plan.moves}
        originals_projcache = {m.session_id: engine.projcache_file(home, m.session_id) for m in plan.moves}
        original_cwds = {s.id: s.cwd for s in picked}
        report = engine.apply_plan(plan, allow_running=True, progress=lambda t: print(f"      {t}"))
        check(len(report["moved"]) == len(ids), f"报告 {len(report['moved'])} 个已迁移")
        check(Path(report["backup"]).is_dir(), f"备份目录已生成：{Path(report['backup']).name}")

        print("\n[4] 独立校验（不依赖引擎的 verify）")
        for move in plan.moves:
            before = originals[move.session_id]
            check(not engine.exists(move.from_log), f"旧位置日志已移除：{move.from_log.name}")
            check(engine.is_file(move.to_log), f"新位置日志存在：{move.to_log}")
            after = engine.read_bytes(move.to_log)
            old_frame, old_tail = tail_after_first_frame(before)
            new_frame, new_tail = tail_after_first_frame(after)
            check(old_tail == new_tail, "第 0 帧之后的字节逐字节相同（事件流零改动）")
            check(len(engine.scan_zstd_frames(before)[0]) == len(engine.scan_zstd_frames(after)[0]),
                  "帧数一致")
            header = json.loads(engine._decompress_frame(new_frame)[:-1].decode("utf-8"))
            check(header.get("cwd") == plan.target, f"header.cwd 已改写：{header.get('cwd')}")
            check(header.get("id") == move.session_id, "header.id 未变")
            derived = engine.migrate_log_path(home, header["cwd"], header["id"])
            check(derived == move.to_log, "日志路径与 header 自洽（DSH 会做同样校验）")
            pc = originals_projcache[move.session_id]
            if engine.exists(pc):
                doc = json.loads(engine.read_text(pc))
                check(doc.get("record", {}).get("identity", {}).get("cwd") == plan.target,
                      "投影缓存已改指新目录（标题等缓存行得以保留）")
            else:
                check(False, "投影缓存不应被删除")
            check(engine.list_sessions(home) and
                  any(s.id == move.session_id and s.title for s in engine.list_sessions(home)),
                  "迁移后仍能读到该会话的标题")

        reloaded = {s.id: s for s in engine.list_sessions(home)}
        for sid in ids:
            info = reloaded.get(sid)
            check(info is not None and info.workspace_id is not None and
                  engine.canonical(info.cwd) == plan.target,
                  f"重新扫描后归属目标工作区：{sid[:26]}…")

        db = engine.load_db(home)
        try:
            engine.validate_db(db)
            check(True, "workspace.json 结构自检通过")
        except engine.MigrationError as exc:
            check(False, "workspace.json 结构自检通过", str(exc))
        holders = [wid for wid, rec in db["tables"]["workspaces"].items()
                   if any(s in rec.get("sessionIds", []) for s in ids)]
        check(len(holders) == 1, f"会话只被一个工作区登记（{len(holders)} 处）")
        check(any(canonical_match(rec["path"], plan.target) for rec in db["tables"]["workspaces"].values()),
              "目标工作区记录已建立")

        print("\n[5] 引擎自带复核")
        for ok, label, extra in engine.verify(home, ids, plan.target):
            check(ok, label, extra)

        print("\n[6] 回滚")
        result = engine.rollback(Path(report["backup"]), allow_running=True,
                                 progress=lambda t: print(f"      {t}"))
        check(len(result["restored"]) == len(ids), f"回滚 {len(result['restored'])} 个会话")
        for move in plan.moves:
            check(engine.is_file(move.from_log) and not engine.exists(move.to_log),
                  f"日志已回到原位置：{move.from_log.parent.name}")
            check(engine.read_bytes(move.from_log) == originals[move.session_id],
                  "回滚后与原日志逐字节相同")
        db2 = engine.load_db(home)
        check(json.dumps(db2, sort_keys=True) == json.dumps(engine.load_db(home), sort_keys=True),
              "工作区表可正常读取")
        back = {s.id: s for s in engine.list_sessions(home)}
        for info in back.values():
            if info.id in ids:
                check(engine.same_path(info.cwd, original_cwds[info.id]),
                      f"回滚后 header.cwd 复原：{info.cwd}")

        print("\n[7] 边界情况")
        try:
            engine.build_plan(home, ["不存在的会话"], str(target))
            check(False, "不存在的会话应当报错")
        except engine.MigrationError as exc:
            check(True, "不存在的会话被拒绝", str(exc)[:60])
        try:
            engine.build_plan(home, ids, str(scratch / "不存在"))
            check(False, "不存在的目标目录应当报错")
        except engine.MigrationError as exc:
            check(True, "不存在的目标目录被拒绝", str(exc)[:60])
        try:
            engine.build_plan(home, ids, picked[0].cwd or "")
            check(False, "目标 == 原工作区应当跳过")
        except engine.MigrationError as exc:
            check(True, "同工作区迁移被合理拒绝/跳过", str(exc)[:60])
    finally:
        engine.rmtree(scratch)

    print()
    if _failures:
        print(f"自检失败：{len(_failures)} 项未通过 ❌")
        for name in _failures:
            print(f"  - {name}")
        return 1
    print("自检全部通过 ✅")
    return 0


def canonical_match(a: str, b: str) -> bool:
    return engine.canonical(a) == engine.canonical(b)


if __name__ == "__main__":
    sys.exit(run())
