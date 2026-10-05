"""DSH 工作区迁移器 —— 程序入口。

默认启动图形界面；也提供命令行模式，便于脚本化和自检：

    DSH工作区迁移器.exe                     启动界面
    DSH工作区迁移器.exe --list              列出工作区和会话
    DSH工作区迁移器.exe --migrate <会话id...> --to <目录> [--yes]
    DSH工作区迁移器.exe --rollback [备份目录]
    DSH工作区迁移器.exe --selftest          在副本上跑一遍完整迁移（不碰真实数据）
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

from dshmigrator import engine

# 命令行模式在 GBK 控制台下也能输出中文与符号
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _cmd_list(home: Path) -> int:
    workspaces, _ = engine.list_workspaces(home)
    sessions = engine.list_sessions(home)
    print(f"DSH_HOME: {home}")
    print(f"工作区 {len(workspaces)} 个，会话 {len(sessions)} 个\n")
    for ws in workspaces:
        items = [s for s in sessions if s.workspace_id == ws.id]
        flag = "" if ws.path_ok else "  ⚠ 目录不存在"
        print(f"[{ws.display_title}]{flag}  {ws.path}   ({len(items)} 个会话)")
        for s in items:
            print(f"    {s.created_text}  {s.size_text:>8}  {s.display_title}")
            print(f"        {s.id}")
    orphans = [s for s in sessions if s.workspace_id is None]
    if orphans:
        print(f"[未登记 / 路径失效]  ({len(orphans)} 个会话)")
        for s in orphans:
            print(f"    {s.display_title}  cwd={s.cwd}")
            print(f"        {s.id}")
    return 0


def _cmd_migrate(home: Path, ids: list[str], target: str, assume_yes: bool) -> int:
    try:
        plan = engine.build_plan(home, ids, target)
    except Exception as exc:                           # noqa: BLE001
        print("无法建立迁移计划：\n" + engine.humanize_error(exc))
        return 1
    print(f"把 {len(plan.moves)} 个会话迁移到 {plan.target}")
    for change in plan.db_changes:
        print(f"  · {change}")
    if not assume_yes:
        answer = input("继续？[y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("已取消")
            return 1
    try:
        report = engine.apply_plan(plan, progress=lambda t: print(f"  {t}"))
    except Exception as exc:                           # noqa: BLE001
        print("\n" + engine.humanize_error(exc))
        return 1
    print(f"\n完成。备份：{report['backup']}")
    for move in report["moved"]:
        print(f"  {move['from']}  ->  {move['to']}  ({move['frames']} 帧)")
    print("\n复核：")
    for ok, label, extra in engine.verify(home, [m.session_id for m in plan.moves], plan.target):
        print(f"  {'✔' if ok else '✖'} {label} {extra}")
    return 0


def _cmd_rollback(home: Path, backup: str | None) -> int:
    path = Path(backup) if backup else engine.latest_backup(home)
    if path is None:
        print("没有可回滚的备份")
        return 1
    result = engine.rollback(path, progress=lambda t: print(f"  {t}"))
    print(f"已回滚 {len(result['restored'])} 个会话，来自 {result['backup']}")
    return 0


def _cmd_selftest() -> int:
    """端到端跑一遍：迁移 → 独立校验 → 回滚。

    默认在合成数据上跑（不需要真实 DSH 数据）；设 DSH_MIGRATOR_SELFTEST_REAL=1
    可以改为把你的真实数据只读复制到临时目录再跑，原件不会被写入。
    """
    from tools.rehearse import run

    use_real = os.environ.get("DSH_MIGRATOR_SELFTEST_REAL") == "1"
    return run(use_real=use_real)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="DSH工作区迁移器", description="把 DSH 会话迁移到另一个工作区")
    parser.add_argument("--home", help="DSH 目录（默认取 DSH_HOME 或安装目录）")
    parser.add_argument("--list", action="store_true", help="列出工作区与会话")
    parser.add_argument("--migrate", nargs="+", metavar="SESSION_ID", help="迁移这些会话")
    parser.add_argument("--to", help="目标工作区目录")
    parser.add_argument("--yes", "-y", action="store_true", help="跳过确认")
    parser.add_argument("--rollback", nargs="?", const="", metavar="BACKUP_DIR", help="回滚（默认最近一次）")
    parser.add_argument("--selftest", action="store_true", help="在副本上自检")
    args = parser.parse_args(argv)

    home = Path(args.home) if args.home else engine.default_home()

    if args.selftest:
        return _cmd_selftest()
    if args.list:
        return _cmd_list(home)
    if args.migrate:
        if not args.to:
            parser.error("--migrate 需要同时给出 --to <目标目录>")
        return _cmd_migrate(home, args.migrate, args.to, args.yes)
    if args.rollback is not None:
        return _cmd_rollback(home, args.rollback or None)

    from dshmigrator.gui import main as gui_main

    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
