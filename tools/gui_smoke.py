"""界面级冒烟测试：真的把窗口建起来，驱动「勾选 → 迁移 → 刷新」整条链路。

在临时副本上跑，真实数据只读；所有弹窗自动回答「是」。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import tkinter as tk  # noqa: E402
from tkinter import messagebox  # noqa: E402

from dshmigrator import engine, gui  # noqa: E402
from tools import demo_home  # noqa: E402
from tools.rehearse import check, _failures  # noqa: E402


def pump(root: tk.Tk, seconds: float, until=None) -> bool:
    """转动事件循环，直到条件成立或超时。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        root.update()
        if until is not None and until():
            return True
        time.sleep(0.05)
    return until() if until else True


def run() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="dsh-migrate-guitest-"))
    home = scratch / ".dsh"
    try:
        print("[1] 生成合成数据并把它设为 DSH_HOME")
        summary = demo_home.build(home, base_dir=scratch / "projects")
        os.environ["DSH_HOME"] = str(home)
        check(engine.default_home() == home, f"引擎已指向合成数据：{home}")
        check(len(summary["sessions"]) >= 5, f"合成会话 {len(summary['sessions'])} 个")

        print("\n[2] 建窗口")
        root = tk.Tk()
        root.withdraw()                      # 测试时不真的显示窗口
        app = gui.MigratorApp(root)
        pump(root, 2)
        check(len(app.workspaces) > 0, f"工作区载入：{len(app.workspaces)} 个")
        check(len(app.sessions) > 0, f"会话载入：{len(app.sessions)} 个")
        check(app.ws_tree.get_children() != (), "工作区树有内容")

        # 选一个会话数 >= 2 的工作区
        target_ws = None
        for ws in app.workspaces:
            items = [s for s in app.sessions if s.workspace_id == ws.id]
            if len(items) >= 2:
                target_ws = ws
                break
        if target_ws is None:
            print("找不到合适的源工作区，跳过")
            return 1
        app.ws_tree.selection_set(target_ws.id)
        pump(root, 1)
        rows = list(app.rows.values())
        check(len(rows) >= 1, f"选中工作区「{target_ws.display_title}」列出 {len(rows)} 个会话")
        check(app.sess_tree.get_children() != (), "会话树有内容")

        # 子代理会话默认隐藏：先验证这一点，再打开以便有足够会话可勾选
        total_items = len(items)
        app.show_subagents.set(False)
        app.on_filter_change()
        pump(root, 0.5)
        hidden_rows = len(app.rows)
        check(hidden_rows <= total_items,
              f"默认隐藏子代理会话：显示 {hidden_rows}/{total_items}")
        check(hidden_rows == len([s for s in items if not s.is_subagent]),
              "隐藏数量与 is_subagent 判定一致")
        app.show_subagents.set(True)
        app.on_filter_change()
        pump(root, 0.5)
        check(len(app.rows) == total_items,
              f"勾选「显示子代理会话」后全部可见：{len(app.rows)}")
        if len(app.rows) < 2:
            print("该工作区可见会话不足 2 个，跳过")
            return 1

        print("\n[3] 勾选两个会话（走真实的点击逻辑）")
        items = app.sess_tree.get_children()[:2]
        for item in items:
            app.toggle(item)
        pump(root, 0.5)
        check(len(app.checked) == 2, f"已勾选 {len(app.checked)} 个")
        check(all(app.sess_tree.set(i, "sel") == gui.CHECK_ON for i in items), "勾选标记已更新")
        check(app.sel_label.cget("text") == "已选 2 个", "已选计数已更新")
        app.toggle(items[0])
        check(len(app.checked) == 1, "再次点击可取消勾选")
        app.toggle(items[0])
        app.set_all(True)
        pump(root, 0.5)
        check(len(app.checked) == len(app.rows), f"全选 = {len(app.checked)}")
        app.set_all(False)
        for item in items:
            app.toggle(item)

        print("\n[4] 设定目标并迁移")
        target_dir = scratch / "新工作区"
        target_dir.mkdir()
        app.target_var.set(str(target_dir))
        app.on_target_change()
        pump(root, 0.5)
        check("✔" in app.target_hint.cget("text"), "目标提示为有效", app.target_hint.cget("text")[:40])

        asked = []

        def fake_ask(title, message, **kw):
            asked.append(message)
            return True

        with mock.patch.object(engine, "dsh_processes", lambda: []), \
             mock.patch.object(messagebox, "askyesno", fake_ask), \
             mock.patch.object(messagebox, "showinfo", lambda *a, **k: None), \
             mock.patch.object(messagebox, "showwarning", lambda *a, **k: None), \
             mock.patch.object(messagebox, "showerror", lambda *a, **k: print("      UI ERROR:", a)):
            app.do_migrate()
            ok = pump(root, 60, until=lambda: not app.busy)
            check(ok, "迁移在 60 秒内完成（异步线程 + 事件循环）")
        check(len(asked) >= 1, "弹出了确认对话框")
        check(not app.busy, "界面已回到空闲")

        print("\n[5] 迁移后界面状态")
        moved_ids = [s.id for s in app.sessions if s.id in app.checked] or []
        check(app.checked == set(), "迁移后勾选被清空")
        app.refresh_all()
        pump(root, 3)
        fresh = {s.id: s for s in app.sessions}
        migrated = [s for s in fresh.values() if engine.same_path(s.cwd, str(target_dir))]
        check(len(migrated) == 2, f"新工作区下有 {len(migrated)} 个会话")
        check(any(w.path and engine.same_path(w.path, str(target_dir)) for w in app.workspaces),
              "工作区列表出现新目标")
        check(engine.latest_backup(home) is not None, "备份存在，可回滚")

        print("\n[6] 界面回滚")
        with mock.patch.object(engine, "dsh_processes", lambda: []), \
             mock.patch.object(messagebox, "askyesno", lambda *a, **k: True), \
             mock.patch.object(messagebox, "showinfo", lambda *a, **k: None), \
             mock.patch.object(messagebox, "showerror", lambda *a, **k: print("      UI ERROR:", a)):
            app.do_rollback()
            ok = pump(root, 60, until=lambda: not app.busy)
            check(ok, "回滚完成")
        after = {s.id: s for s in engine.list_sessions(home)}
        restored = [s for s in after.values() if engine.same_path(s.cwd, target_ws.path)]
        check(len(restored) >= 2, f"回滚后源工作区恢复 {len(restored)} 个会话")

        print("\n[7] DSH 运行时的拦截")
        with mock.patch.object(engine, "dsh_processes", lambda: [(1234, "DeepSeek Harness.exe")]), \
             mock.patch.object(messagebox, "showinfo", lambda *a, **k: None), \
             mock.patch.object(messagebox, "showerror", lambda *a, **k: asked.append(("err", a))):
            app.checked = {list(after)[0]}
            before_len = len(asked)
            app.do_migrate()
            pump(root, 2)
            check(len(asked) > before_len, "DSH 在运行时迁移被拒绝并提示")
            check(not app.busy, "被拒绝后界面仍可用")

        root.destroy()
    finally:
        engine.rmtree(scratch)

    print()
    if _failures:
        print(f"界面冒烟测试失败：{len(_failures)} 项 ❌")
        for name in _failures:
            print(f"  - {name}")
        return 1
    print("界面冒烟测试全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(run())
