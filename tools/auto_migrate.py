"""等待 DSH 完全退出后，自动把指定会话迁移到目标工作区。

给无人值守场景用：由计划任务拉起（不是 DSH 的子进程，所以 DSH 退出后它仍然活着），
它先等待 DSH 进程全部消失并稳定若干秒，再执行迁移 + 校验，最后把自己的计划任务删掉。

用法：
    pythonw auto_migrate.py --config auto-migrate-config.json
    python auto_migrate.py --config ... --now          # 跳过等待（调试用）
    python auto_migrate.py --config ... --now --dry-run # 只报告，不写任何东西
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dshmigrator import engine  # noqa: E402

# 无人值守时 stdout 可能被重定向到 GBK 控制台，或根本不存在（pythonw）。
# 统一成 UTF-8 并容错，绝不让“打印日志”这件事把迁移本身弄挂。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
POLL_SECONDS = 2
STABLE_TICKS = 3           # 连续 3 次（≈6 秒）检测不到 DSH 才认为它真的退出了
DEFAULT_TIMEOUT = 8 * 3600  # 最多等 8 小时


def make_logger(log_path: Path):
    def emit(text: str, echo: bool = True) -> None:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}"
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
        if echo:
            try:
                print(line, flush=True)
            except (UnicodeEncodeError, ValueError, OSError):
                pass

    return emit


def wait_for_dsh_exit(emit, timeout: int) -> bool:
    """等 DSH 进程消失并稳定下来。返回 True 表示可以开始迁移。"""
    deadline = time.time() + timeout
    stable = 0
    reported = False
    while time.time() < deadline:
        procs = engine.dsh_processes()
        if procs:
            if not reported:
                emit(f"检测到 {len(procs)} 个 DSH 进程，继续等待…")
                reported = True
            stable = 0
        else:
            stable += 1
            if stable >= STABLE_TICKS:
                return True
        time.sleep(POLL_SECONDS)
    emit(f"等待超过 {timeout // 3600} 小时仍未等到 DSH 退出，放弃（未做任何改动）")
    return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="DSH 会话自动迁移器")
    parser.add_argument("--config", required=True, help="JSON 配置路径")
    parser.add_argument("--home", help="覆盖配置里的 DSH 目录")
    parser.add_argument("--to", help="覆盖配置里的目标工作区")
    parser.add_argument("--now", action="store_true", help="不等待 DSH，立即执行（调试用）")
    parser.add_argument("--dry-run", action="store_true", help="只建立计划并打印，不写入")
    parser.add_argument("--force", action="store_true",
                        help="跳过 DSH 运行检查（仅供测试；正常路径绝不要用）")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="等待秒数上限")
    args = parser.parse_args(argv)

    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        # 先按当前目录解释，其次按脚本所在目录的文件名解释
        from_cwd = Path.cwd() / cfg_path
        cfg_path = from_cwd if from_cwd.exists() else (HERE / cfg_path.name)
    if not cfg_path.exists():
        print(f"找不到配置文件：{cfg_path}")
        return 4
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    home = Path(args.home or cfg["home"])
    target = args.to or cfg["to"]
    ids = list(cfg["sessionIds"])
    log_path = HERE / cfg.get("log", "auto-migrate.log")
    emit = make_logger(log_path)

    emit("=" * 70)
    emit("DSH 会话自动迁移 —— 启动")
    emit(f"DSH 目录 : {home}")
    emit(f"目标工作区: {target}")
    emit(f"待迁移会话: {len(ids)} 个")
    if args.dry_run:
        emit("模式: 预演（不写任何文件）")

    # 开工前先确认自己写得进去：放在工作区里的程序会被降到 Low 完整性而写不了 .dsh
    blocked = engine.write_preflight(home)
    if blocked:
        for line in blocked.splitlines():
            emit(line)
        emit("已停止：不做任何改动。请把本工具移到 DSH 工作区之外再运行。")
        return 5

    try:
        if args.dry_run:
            plan = engine.build_plan(home, ids, target)
            emit(f"计划: 将移动 {len(plan.moves)} 个会话")
            for change in plan.db_changes:
                emit(f"  · {change}")
            for note in plan.notes:
                emit(f"  · {note}")
            return 0

        if not args.now:
            emit("等待 DeepSeek Harness 完全退出…")
            if not wait_for_dsh_exit(emit, args.timeout):
                return 1
            emit("DSH 已退出且稳定，开始迁移")

        # DSH 有可能在等待期间又被打开；被拒绝就回到等待状态重试
        report = None
        for attempt in range(1, 5):
            try:
                plan = engine.build_plan(home, ids, target)
                emit(f"计划: 移动 {len(plan.moves)} 个会话到 {plan.target}")
                for change in plan.db_changes:
                    emit(f"  · {change}")
                for note in plan.notes:
                    emit(f"  · {note}")
                report = engine.apply_plan(plan, progress=lambda t: emit(f"  {t}"),
                                           allow_running=args.force)
                break
            except engine.MigrationError as exc:
                message = str(exc)
                # DSH 可能在等待期间又被打开：只有正常（非 --now）路径才回到等待重试
                if "正在运行" in message and not args.now and attempt < 4:
                    emit(f"DSH 又启动了（{message}），回到等待状态（第 {attempt} 次）")
                    if not wait_for_dsh_exit(emit, args.timeout):
                        return 1
                    continue
                emit(f"迁移失败: {message}")
                return 1

        if report is None:
            emit("迁移未完成")
            return 1

        emit(f"备份目录: {report['backup']}")
        for move in report["moved"]:
            emit(f"  已迁移 {move['id']}")
            emit(f"      {move['from']}  ->  {move['to']}   ({move['frames']} 帧, "
                 f"{move['bytes_before']} -> {move['bytes_after']} 字节)")
        for note in report["notes"]:
            emit(f"  {note}")

        emit("复核:")
        bad = 0
        for ok, label, extra in engine.verify(home, ids, target):
            emit(f"  {'✔' if ok else '✖'} {label} {extra}")
            if not ok:
                bad += 1

        result = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "target": target,
                  "moved": len(report["moved"]), "backup": report["backup"],
                  "verify_failures": bad}
        (HERE / cfg.get("result", "auto-migrate-result.json")).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        emit(f"完成：迁移 {len(report['moved'])} 个会话，复核未通过 {bad} 项")
        emit("重新打开 DeepSeek Harness 即可看到新工作区里的这些会话。")
        return 0 if bad == 0 else 2

    except Exception as exc:                             # noqa: BLE001
        emit("发生未预期错误：")
        for line in engine.humanize_error(exc).splitlines():
            emit(f"  {line}")
        emit("原始调用栈（排查用）：")
        for line in traceback.format_exc().splitlines():
            emit(f"  {line}")
        return 3
    finally:
        task = cfg.get("taskName")
        if task and not args.now:
            try:
                subprocess.run(["schtasks", "/delete", "/tn", task, "/f"],
                               capture_output=True, timeout=20)
                emit(f"已清理计划任务 {task}")
            except (OSError, subprocess.SubprocessError):
                pass


if __name__ == "__main__":
    sys.exit(main())
