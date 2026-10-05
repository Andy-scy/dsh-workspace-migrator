"""DSH 工作区迁移器 —— tkinter 界面。"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import engine
from .engine import MigrationError

APP_NAME = "DSH 工作区迁移器"
CHECK_ON = "☑"
CHECK_OFF = "☐"
ORPHAN_ID = "__orphan__"
FONT = ("Microsoft YaHei UI", 10)
FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")
FONT_TITLE = ("Microsoft YaHei UI", 13, "bold")
FONT_MONO = ("Consolas", 9)


def config_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "dsh-workspace-migrator" / "config.json"


def load_config() -> dict:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    try:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


class MigratorApp(ttk.Frame):
    def __init__(self, master: tk.Tk):
        super().__init__(master, padding=10)
        self.master: tk.Tk = master
        self.cfg = load_config()
        self.home = Path(self.cfg.get("home") or engine.default_home())
        self.workspaces: list[engine.WorkspaceInfo] = []
        self.sessions: list[engine.SessionInfo] = []
        self.session_by_id: dict[str, engine.SessionInfo] = {}
        self.checked: set[str] = set()
        self.rows: dict[str, str] = {}          # tree item id -> session id
        self.busy = False
        self.events: queue.Queue = queue.Queue()

        self._build()
        self.master.after(150, self._place_sash)
        self.refresh_all()

    def _place_sash(self) -> None:
        """让左侧工作区栏只占约 250px，其余留给会话列表。"""
        try:
            self.main_pane.sashpos(0, 252)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        self.grid(sticky="nsew")
        self.master.rowconfigure(0, weight=1)
        self.master.columnconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Treeview", font=FONT, rowheight=24)
        style.configure("Treeview.Heading", font=FONT_BOLD)
        style.configure("TLabel", font=FONT)
        style.configure("TButton", font=FONT)
        style.configure("TCheckbutton", font=FONT)
        style.configure("Title.TLabel", font=FONT_TITLE)
        style.configure("Hint.TLabel", font=("Microsoft YaHei UI", 9), foreground="#666")
        style.configure("Ok.TLabel", font=FONT, foreground="#0a7a2f")
        style.configure("Bad.TLabel", font=FONT, foreground="#b3261e")
        style.configure("Warn.TLabel", font=FONT, foreground="#9a6400")

        # ---- 标题行
        head = ttk.Frame(self)
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(1, weight=1)
        ttk.Label(head, text=APP_NAME, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        self.home_label = ttk.Label(head, text="", style="Hint.TLabel")
        self.home_label.grid(row=0, column=1, sticky="w", padx=(12, 0))
        ttk.Button(head, text="更改 DSH 目录…", command=self.pick_home).grid(row=0, column=2, padx=(4, 0))
        ttk.Button(head, text="刷新", command=self.refresh_all).grid(row=0, column=3, padx=(4, 0))

        # ---- 状态横幅
        self.banner = ttk.Frame(self)
        self.banner.grid(row=1, column=0, sticky="ew", pady=(8, 6))
        self.banner.columnconfigure(0, weight=1)
        self.banner_label = ttk.Label(self.banner, text="", style="Ok.TLabel")
        self.banner_label.grid(row=0, column=0, sticky="w")
        self.btn_recheck = ttk.Button(self.banner, text="重新检测", command=self.refresh_all)
        self.btn_recheck.grid(row=0, column=1, padx=4)
        self.btn_kill = ttk.Button(self.banner, text="关闭 DSH", command=self.close_dsh)
        self.btn_kill.grid(row=0, column=2, padx=4)

        # ---- 主区：左工作区 / 右会话
        main = ttk.Panedwindow(self, orient="horizontal")
        main.grid(row=2, column=0, sticky="nsew")
        self.main_pane = main

        left = ttk.Labelframe(main, text=" 工作区 ", padding=6)
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.ws_tree = ttk.Treeview(left, columns=("count",), show="tree headings", selectmode="browse")
        self.ws_tree.heading("#0", text="工作区")
        self.ws_tree.heading("count", text="会话")
        self.ws_tree.column("#0", width=198, stretch=True)
        self.ws_tree.column("count", width=48, anchor="center", stretch=False)
        self.ws_tree.grid(row=0, column=0, sticky="nsew")
        ws_scroll = ttk.Scrollbar(left, orient="vertical", command=self.ws_tree.yview)
        ws_scroll.grid(row=0, column=1, sticky="ns")
        self.ws_tree.configure(yscrollcommand=ws_scroll.set)
        self.ws_tree.bind("<<TreeviewSelect>>", lambda _e: self.on_workspace_select())
        main.add(left, weight=1)

        right = ttk.Labelframe(main, text=" 会话（点击第一列勾选） ", padding=6)
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        cols = ("sel", "title", "created", "size", "id", "cwd")
        self.sess_tree = ttk.Treeview(right, columns=cols, show="headings", selectmode="extended")
        headings = {"sel": ("选择", 44, "center"), "title": ("标题", 300, "w"),
                    "created": ("创建时间", 128, "center"), "size": ("大小", 74, "e"),
                    "id": ("会话 ID", 250, "w"), "cwd": ("当前工作目录", 240, "w")}
        for key, (text, width, anchor) in headings.items():
            self.sess_tree.heading(key, text=text)
            self.sess_tree.column(key, width=width, anchor=anchor, stretch=(key in ("title", "cwd")))
        self.sess_tree.grid(row=0, column=0, sticky="nsew")
        sess_scroll = ttk.Scrollbar(right, orient="vertical", command=self.sess_tree.yview)
        sess_scroll.grid(row=0, column=1, sticky="ns")
        sess_xscroll = ttk.Scrollbar(right, orient="horizontal", command=self.sess_tree.xview)
        sess_xscroll.grid(row=1, column=0, sticky="ew")
        self.sess_tree.configure(yscrollcommand=sess_scroll.set, xscrollcommand=sess_xscroll.set)
        self.sess_tree.bind("<Button-1>", self.on_session_click)
        self.sess_tree.bind("<space>", self.on_session_space)

        tools = ttk.Frame(right)
        tools.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(tools, text="全选", command=lambda: self.set_all(True)).grid(row=0, column=0)
        ttk.Button(tools, text="全不选", command=lambda: self.set_all(False)).grid(row=0, column=1, padx=4)
        self.show_subagents = tk.BooleanVar(value=False)
        ttk.Checkbutton(tools, text="显示子代理会话", variable=self.show_subagents,
                        command=self.on_filter_change).grid(row=0, column=2, padx=(10, 0))
        self.sel_label = ttk.Label(tools, text="已选 0 个", style="Hint.TLabel")
        self.sel_label.grid(row=0, column=3, padx=10)
        self.ws_hint = ttk.Label(tools, text="", style="Warn.TLabel")
        self.ws_hint.grid(row=0, column=4, padx=(4, 0), sticky="w")
        main.add(right, weight=3)

        # ---- 底部：目标 + 动作
        bottom = ttk.Frame(self)
        bottom.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        bottom.columnconfigure(1, weight=1)
        ttk.Label(bottom, text="目标工作区：").grid(row=0, column=0, sticky="w")
        self.target_var = tk.StringVar()
        self.target_box = ttk.Combobox(bottom, textvariable=self.target_var, font=FONT)
        self.target_box.grid(row=0, column=1, sticky="ew", padx=(0, 4))
        self.target_box.bind("<<ComboboxSelected>>", lambda _e: self.on_target_change())
        self.target_box.bind("<KeyRelease>", lambda _e: self.on_target_change())
        ttk.Button(bottom, text="浏览…", command=self.pick_target).grid(row=0, column=2)
        ttk.Button(bottom, text="新建目录…", command=self.create_target).grid(row=0, column=3, padx=4)

        self.target_hint = ttk.Label(bottom, text="", style="Hint.TLabel")
        self.target_hint.grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))

        actions = ttk.Frame(self)
        actions.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        actions.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(actions, mode="indeterminate", length=160)
        self.progress.grid(row=0, column=0, sticky="w")
        self.status = ttk.Label(actions, text="就绪", style="Hint.TLabel")
        self.status.grid(row=0, column=1, sticky="w", padx=10)
        self.btn_rollback = ttk.Button(actions, text="回滚上次迁移", command=self.do_rollback)
        self.btn_rollback.grid(row=0, column=2, padx=4)
        self.btn_migrate = ttk.Button(actions, text="迁移选中会话", command=self.do_migrate)
        self.btn_migrate.grid(row=0, column=3, padx=(4, 0))

        self.master.bind("<F5>", lambda _e: self.refresh_all())

    # ------------------------------------------------------------- 数据刷新
    def refresh_all(self) -> None:
        if self.busy:
            return
        self.home_label.configure(text=f"DSH 目录：{self.home}")
        try:
            self.workspaces, db = engine.list_workspaces(self.home)
            self.sessions = engine.list_sessions(self.home)
        except Exception as exc:                       # noqa: BLE001 - 读取失败也要让人看见
            self.workspaces, self.sessions = [], []
            first = engine.humanize_error(exc).splitlines()
            self.banner_label.configure(
                text="读取 DSH 数据失败：" + (" ".join(first[:2]) if first else str(exc)),
                style="Bad.TLabel")
            self.sess_tree.delete(*self.sess_tree.get_children())
            self.ws_tree.delete(*self.ws_tree.get_children())
            return

        self.session_by_id = {s.id: s for s in self.sessions}
        self.checked &= set(self.session_by_id)

        # 工作区树
        selected = self.current_workspace_id()
        self.ws_tree.delete(*self.ws_tree.get_children())
        for ws in self.workspaces:
            label = ws.display_title + ("" if ws.path_ok else "  ⚠ 目录不存在")
            self.ws_tree.insert("", "end", iid=ws.id, text=label, values=(self.count_for(ws.id),))
        orphans = [s for s in self.sessions if s.workspace_id is None]
        if orphans:
            self.ws_tree.insert("", "end", iid=ORPHAN_ID, text="（未登记 / 路径失效）", values=(len(self.visible(orphans)),))
        if selected and self.ws_tree.exists(selected):
            self.ws_tree.selection_set(selected)
        else:
            children = self.ws_tree.get_children()
            if children:
                self.ws_tree.selection_set(children[0])

        self.target_box.configure(values=[ws.path for ws in self.workspaces])
        self.on_workspace_select()
        self.refresh_banner()
        self.on_target_change()

    def refresh_banner(self) -> None:
        blocked = engine.write_preflight(self.home)
        if blocked:
            self.banner_label.configure(
                text="⛔ 本程序无法写入 DSH 目录 —— 很可能是因为它被放在工作区目录里运行"
                     "（工作区带 Windows 低完整性标签，写不了 .dsh）。请把工具移到工作区之外。",
                style="Bad.TLabel")
            self.btn_kill.state(["disabled"])
            return
        procs = engine.dsh_processes()
        if procs:
            names = ", ".join(sorted({n for _, n in procs}))
            self.banner_label.configure(
                text=f"⚠ 检测到 DSH 正在运行（{names}，{len(procs)} 个进程）——迁移前必须完全退出，否则会话日志会被写回旧位置。",
                style="Bad.TLabel")
            self.btn_kill.state(["!disabled"])
        else:
            self.banner_label.configure(
                text="✔ 未检测到 DSH 进程，可以安全迁移（迁移完成后重新打开 DSH 即可看到结果）。",
                style="Ok.TLabel")
            self.btn_kill.state(["disabled"])

    def current_workspace_id(self) -> str | None:
        sel = self.ws_tree.selection()
        return sel[0] if sel else None

    def visible(self, items: list[engine.SessionInfo]) -> list[engine.SessionInfo]:
        if self.show_subagents.get():
            return items
        return [s for s in items if not s.is_subagent]

    def count_for(self, workspace_id: str) -> int:
        return len(self.visible([s for s in self.sessions if s.workspace_id == workspace_id]))

    def on_filter_change(self) -> None:
        for ws in self.workspaces:
            if self.ws_tree.exists(ws.id):
                self.ws_tree.set(ws.id, "count", self.count_for(ws.id))
        if self.ws_tree.exists(ORPHAN_ID):
            self.ws_tree.set(ORPHAN_ID, "count",
                             len(self.visible([s for s in self.sessions if s.workspace_id is None])))
        self.on_workspace_select()

    def on_workspace_select(self) -> None:
        wid = self.current_workspace_id()
        self.sess_tree.delete(*self.sess_tree.get_children())
        self.rows.clear()
        self.ws_hint.configure(text="")
        if wid is None:
            self.update_sel_label()
            return
        if wid == ORPHAN_ID:
            items = [s for s in self.sessions if s.workspace_id is None]
            self.ws_hint.configure(text="ℹ DSH 无法识别这些会话的工作目录；迁到有效目录即可重新出现。")
        else:
            items = [s for s in self.sessions if s.workspace_id == wid]
            ws = next((w for w in self.workspaces if w.id == wid), None)
            if ws is not None and not ws.path_ok:
                self.ws_hint.configure(
                    text="ℹ 该目录已不存在：这些会话在 DSH 里是隐藏的；迁移到有效目录即可“救回”。")
        hidden = len(items) - len(self.visible(items))
        for s in self.visible(items):
            self.insert_session(s)
        if hidden and not self.ws_hint.cget("text"):
            self.ws_hint.configure(text=f"ℹ 另有 {hidden} 个子代理会话已隐藏（勾选上方可显示）")
        self.update_sel_label()

    def insert_session(self, s: engine.SessionInfo) -> None:
        mark = CHECK_ON if s.id in self.checked else CHECK_OFF
        cwd = s.cwd or "-"
        if not s.cwd_ok:
            cwd += "  (不存在)"
        title = ("└ " if s.is_subagent else "") + s.display_title
        item = self.sess_tree.insert("", "end", values=(mark, title, s.created_text,
                                                       s.size_text, s.id, cwd))
        self.rows[item] = s.id

    def update_sel_label(self) -> None:
        self.sel_label.configure(text=f"已选 {len(self.checked)} 个")

    # --------------------------------------------------------------- 交互
    def on_session_click(self, event) -> str | None:
        if self.sess_tree.identify_region(event.x, event.y) != "cell":
            return None
        if self.sess_tree.identify_column(event.x) != "#1":
            return None
        item = self.sess_tree.identify_row(event.y)
        if not item:
            return None
        self.toggle(item)
        return "break"

    def on_session_space(self, _event) -> str:
        for item in self.sess_tree.selection():
            self.toggle(item)
        return "break"

    def toggle(self, item: str) -> None:
        sid = self.rows.get(item)
        if sid is None:
            return
        if sid in self.checked:
            self.checked.discard(sid)
        else:
            self.checked.add(sid)
        s = self.session_by_id.get(sid)
        if s is not None:
            self.sess_tree.set(item, "sel", CHECK_ON if sid in self.checked else CHECK_OFF)
        self.update_sel_label()

    def set_all(self, value: bool) -> None:
        for item, sid in self.rows.items():
            if value:
                self.checked.add(sid)
            else:
                self.checked.discard(sid)
            self.sess_tree.set(item, "sel", CHECK_ON if value else CHECK_OFF)
        self.update_sel_label()

    def pick_home(self) -> None:
        chosen = filedialog.askdirectory(title="选择 DSH 目录（应包含 sessions 和 storages）",
                                         initialdir=str(self.home))
        if not chosen:
            return
        self.home = Path(chosen)
        self.cfg["home"] = str(self.home)
        save_config(self.cfg)
        self.refresh_all()

    def pick_target(self) -> None:
        chosen = filedialog.askdirectory(title="选择目标工作区目录", mustexist=True)
        if chosen:
            self.target_var.set(chosen)
            self.on_target_change()

    def create_target(self) -> None:
        parent = filedialog.askdirectory(title="在哪个位置新建工作区目录？", mustexist=True)
        if not parent:
            return
        name = SimplePrompt(self.master, "新建目录", "目录名：").result
        if not name:
            return
        path = Path(parent) / name
        try:
            path.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"创建失败：{exc}")
            return
        self.target_var.set(str(path))
        self.on_target_change()

    def on_target_change(self) -> None:
        path = self.target_var.get().strip().strip('"')
        if not path:
            self.target_hint.configure(text="请选择或输入目标工作区目录", style="Hint.TLabel")
            return
        canon = engine.canonical(path)
        if canon and os.path.isdir(canon):
            known = next((ws for ws in self.workspaces if engine.same_path(ws.path, canon)), None)
            if known:
                self.target_hint.configure(text=f"✔ 已存在的工作区「{known.display_title}」：{canon}", style="Ok.TLabel")
            else:
                self.target_hint.configure(text=f"✔ 目录存在，迁移时会为它新建一条工作区记录：{canon}", style="Warn.TLabel")
        else:
            self.target_hint.configure(text=f"✖ 目录不存在：{path}（可点「新建目录…」）", style="Bad.TLabel")

    def close_dsh(self) -> None:
        procs = engine.dsh_processes()
        if not procs:
            self.refresh_banner()
            return
        if not messagebox.askyesno(APP_NAME,
                                   f"将请求关闭 {len(procs)} 个 DSH 进程。\n\n"
                                   "如果 DSH 里有正在进行的对话，请先手动保存/结束。\n"
                                   "继续吗？"):
            return
        try:
            subprocess.run(["taskkill", "/IM", "DeepSeek Harness.exe"],
                           capture_output=True, timeout=20,
                           creationflags=0x08000000 if sys.platform == "win32" else 0)
        except (OSError, subprocess.SubprocessError):
            pass
        self.after(1500, self.refresh_banner)

    # --------------------------------------------------------------- 迁移
    def do_migrate(self) -> None:
        if self.busy:
            return
        ids = [s.id for s in self.sessions if s.id in self.checked]
        if not ids:
            messagebox.showinfo(APP_NAME, "请先在右侧勾选要迁移的会话。")
            return
        target = self.target_var.get().strip().strip('"')
        if not target:
            messagebox.showinfo(APP_NAME, "请先选择目标工作区目录。")
            return
        canon = engine.canonical(target)
        if canon is None or not os.path.isdir(canon):
            if messagebox.askyesno(APP_NAME, f"目录不存在：\n{target}\n\n要现在创建吗？"):
                try:
                    Path(target).mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    messagebox.showerror(APP_NAME, f"创建目录失败。\n\n{engine.humanize_error(exc)}")
                    return
                canon = engine.canonical(target)
            else:
                return
        if engine.dsh_processes():
            messagebox.showerror(APP_NAME, "检测到 DSH 正在运行。\n\n请完全退出 DeepSeek Harness 后再迁移，"
                                           "否则它会把工作区表写回、或让会话日志重新出现在旧位置。")
            self.refresh_banner()
            return
        blocked = engine.write_preflight(self.home)
        if blocked:
            messagebox.showerror(APP_NAME, blocked)
            self.refresh_banner()
            return

        try:
            plan = engine.build_plan(self.home, ids, canon)
        except Exception as exc:                      # noqa: BLE001 - 任何异常都要让人看见
            messagebox.showerror(APP_NAME, "无法建立迁移计划。\n\n" + engine.humanize_error(exc))
            return

        lines = [f"即将把 {len(plan.moves)} 个会话迁移到：", f"  {canon}", ""]
        for move in plan.moves[:12]:
            info = self.session_by_id.get(move.session_id)
            lines.append(f"  · {info.display_title if info else move.session_id}")
            lines.append(f"      {move.from_cwd}  →  {canon}")
        if len(plan.moves) > 12:
            lines.append(f"  … 其余 {len(plan.moves) - 12} 个")
        lines += ["", "会自动完整备份，可在本窗口一键回滚。继续吗？"]
        if not messagebox.askyesno(APP_NAME, "\n".join(lines)):
            return

        self.run_async(lambda: engine.apply_plan(plan, progress=self.thread_progress),
                       lambda result, error: self.after_migrate(plan, result, error))

    def after_migrate(self, plan, result, error) -> None:
        if error is not None:
            messagebox.showerror(APP_NAME, "迁移没有完成。\n\n" + engine.humanize_error(error))
            self.refresh_all()
            return
        checks = engine.verify(self.home, [m.session_id for m in plan.moves], plan.target)
        bad = [c for c in checks if not c[0]]
        lines = [f"已迁移 {len(result['moved'])} 个会话到：", f"  {plan.target}", "",
                 f"备份：{result['backup']}", ""]
        for move in result["moved"]:
            lines.append(f"· {move['from']}  →  {move['to']}")
            lines.append(f"    {move['frames']} 个 zstd 帧，事件流原样保留")
        lines += ["", "复核结果："]
        for ok, label, extra in checks:
            lines.append(f"  {'✔' if ok else '✖'} {label}" + (f"  {extra}" if extra else ""))
        lines += ["", "现在可以重新打开 DeepSeek Harness，新工作区里就能看到这些会话。"]
        (messagebox.showinfo if not bad else messagebox.showwarning)(APP_NAME, "\n".join(lines))
        self.checked.clear()
        self.refresh_all()

    def do_rollback(self) -> None:
        if self.busy:
            return
        backup = engine.latest_backup(self.home)
        if backup is None:
            messagebox.showinfo(APP_NAME, "没有找到可回滚的备份。\n"
                                          f"（备份目录：{Path(self.home) / 'migration-backups'}）")
            return
        if engine.dsh_processes():
            messagebox.showerror(APP_NAME, "检测到 DSH 正在运行，请先完全退出再回滚。")
            return
        if not messagebox.askyesno(APP_NAME, f"将回滚这次迁移：\n\n{backup}\n\n"
                                             "会话日志和工作区表都会还原到迁移前。继续吗？"):
            return
        self.run_async(lambda: engine.rollback(backup, progress=self.thread_progress),
                       self.after_rollback)

    def after_rollback(self, result, error) -> None:
        if error is not None:
            messagebox.showerror(APP_NAME, "回滚失败。\n\n" + engine.humanize_error(error))
        else:
            messagebox.showinfo(APP_NAME, f"已回滚 {len(result['restored'])} 个会话。\n\n"
                                          "重新打开 DeepSeek Harness 即可看到原状。")
            self.checked.clear()
        self.refresh_all()

    # --------------------------------------------------------- 线程与状态
    def thread_progress(self, text: str) -> None:
        self.events.put(("status", text))

    def run_async(self, work, done) -> None:
        self.busy = True
        self.btn_migrate.state(["disabled"])
        self.btn_rollback.state(["disabled"])
        self.progress.start(12)

        def runner():
            result, error = None, None
            try:
                result = work()
            except Exception as exc:            # noqa: BLE001 - 统一回传给界面
                error = exc
            self.events.put(("done", (result, error, done)))

        threading.Thread(target=runner, daemon=True).start()
        self.pump()

    def pump(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "status":
                    self.status.configure(text=payload)
                elif kind == "done":
                    result, error, done = payload
                    self.progress.stop()
                    self.busy = False
                    self.btn_migrate.state(["!disabled"])
                    self.btn_rollback.state(["!disabled"])
                    self.status.configure(text="完成" if error is None else "失败")
                    done(result, error)
                    return
        except queue.Empty:
            pass
        if self.busy:
            self.after(120, self.pump)


class SimplePrompt(tk.Toplevel):
    """轻量文本输入对话框。"""

    def __init__(self, master, title: str, prompt: str):
        super().__init__(master)
        self.title(title)
        self.resizable(False, False)
        self.result: str | None = None
        self.transient(master)
        ttk.Label(self, text=prompt, font=FONT).grid(row=0, column=0, columnspan=2, padx=14, pady=(14, 6), sticky="w")
        self.var = tk.StringVar()
        entry = ttk.Entry(self, textvariable=self.var, font=FONT, width=34)
        entry.grid(row=1, column=0, columnspan=2, padx=14, sticky="ew")
        entry.focus_set()
        ttk.Button(self, text="确定", command=self._ok).grid(row=2, column=0, padx=14, pady=12, sticky="e")
        ttk.Button(self, text="取消", command=self.destroy).grid(row=2, column=1, padx=(0, 14), pady=12, sticky="w")
        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        master.wait_window(self)

    def _ok(self) -> None:
        self.result = self.var.get().strip() or None
        self.destroy()


def main() -> int:
    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("1320x780")
    root.minsize(1000, 600)
    try:
        root.iconbitmap(default=str(Path(__file__).with_name("app.ico")))
    except (tk.TclError, OSError):
        pass

    def on_callback_error(exc_type, exc_value, exc_tb):
        """窗口程序没有 stderr：未处理异常必须弹出来，绝不静默消失。"""
        try:
            messagebox.showerror(APP_NAME, "程序遇到一个未预期的问题。\n\n" + engine.humanize_error(exc_value))
        except Exception:                             # noqa: BLE001 - 兜底，绝不再抛
            pass

    root.report_callback_exception = on_callback_error
    app = MigratorApp(root)
    root.mainloop()
    return 0
