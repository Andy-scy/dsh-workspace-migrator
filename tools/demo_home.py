"""生成一份合成的 DSH 数据（演示、测试、截图都用它），不含任何真实信息。

产物结构与真实 DSH 完全一致：

    <home>/storages/workspace.json
    <home>/sessions/<projectKey(cwd)>/<id>/session.v4.jsonl.zstd
    <home>/storages/session_projcache/sessions/<id>.json
    <home>/usage-stats/index-v1.json

用法：
    python tools/demo_home.py <目标目录>          # 生成一份演示数据
    from tools.demo_home import build             # 代码里直接建
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dshmigrator import engine  # noqa: E402

# (cwd, 工作区标题, [会话标题])
DEFAULT_WORKSPACES = [
    (r"C:\Demo\web-shop", "web-shop", ["重构结算流程", "接入支付回调", "排查库存超卖"]),
    (r"C:\Demo\desktop-app", "desktop-app", ["打包体积优化", "崩溃日志分析"]),
    (r"C:\Demo\old-project", "old-project", ["迁移前的旧会话"]),
]

SUBTASK_TITLES = ["梳理接口依赖", "补齐单元测试", "核对错误码映射"]


def _header(session_id: str, cwd: str, created_at: int,
            depth: int = 0, parent: str | None = None) -> dict:
    header = {
        "type": "session",
        "version": 4,
        "id": session_id,
        "createdAt": created_at,
        "isSeeded": False,
        "delegationDepth": depth,
        "cwd": cwd,
    }
    if parent is not None:
        header["parentSession"] = parent
        header["origin"] = "subagent"
    return header


def _write_session(home: Path, header: dict, title: str, events: int = 3) -> None:
    """按 DSH 的物理格式写一份会话日志：第 0 帧是 header，之后每帧一批事件。"""
    log = engine.migrate_log_path(home, header["cwd"], header["id"])
    engine.makedirs(log.parent)
    frames = [engine._compress_frame(
        (json.dumps(header, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))]
    for index in range(events):
        event = {"seq": index + 1, "kind": "demo",
                 "text": f"第 {index + 1} 条演示事件（合成数据，非真实对话）"}
        frames.append(engine._compress_frame(
            (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")))
    engine.write_bytes(log, b"".join(frames))

    # 投影缓存：只放标题，够本工具的列表用（DSH 会自行重建其余部分）
    cache = engine.projcache_file(home, header["id"])
    engine.makedirs(cache.parent)
    engine.write_text(cache, json.dumps({
        "version": 7,
        "record": {
            "identity": {
                "formatVersion": 4,
                "createdAt": header["createdAt"],
                "cwd": header["cwd"],
                "isSeeded": False,
                "inheritedEventCount": 0,
            },
            "rows": {"title": {"ver": 1, "seq": events, "val": title}},
        },
    }, ensure_ascii=False, indent=2))


def build(home: Path, workspaces=None, subagents_per_session: int = 2,
          missing_dir_workspace: bool = True, base_dir: Path | None = None) -> dict:
    """在 `home` 下生成一份完整可用的合成 DSH 数据，返回它的构成摘要。

    base_dir 给定时，各工作区目录会落在它下面并真实创建（最后一个仍然故意缺失，
    用来演示「目录已失效」的提示）。
    """
    home = Path(home)
    workspaces = list(workspaces if workspaces is not None else DEFAULT_WORKSPACES)
    now = int(time.time() * 1000)
    table: dict[str, dict] = {}
    order: list[str] = []
    summary = {"home": str(home), "workspaces": [], "sessions": [], "subagents": [],
               "parent": None, "old_cwd": workspaces[0][0] if workspaces else None}

    for w_index, (cwd, title, titles) in enumerate(workspaces):
        if base_dir is not None:
            cwd = str(Path(base_dir) / title)
        # 最后一个工作区故意指向不存在的目录，用来演示「目录已失效」的提示
        if missing_dir_workspace and w_index == len(workspaces) - 1:
            cwd = cwd + r"\已删除"
            summary["old_cwd"] = cwd
        elif base_dir is not None:
            engine.makedirs(cwd)
        ws_id = str(uuid.uuid4())
        session_ids = []
        for s_index, session_title in enumerate(titles):
            created = now - (len(workspaces) - w_index) * 3_600_000 - s_index * 600_000
            sid = str(uuid.uuid4())
            _write_session(home, _header(sid, cwd, created), session_title,
                           events=2 + s_index)
            session_ids.append(sid)
            summary["sessions"].append({"id": sid, "title": session_title, "cwd": cwd,
                                        "workspace": ws_id, "subagent": False})
            if summary["parent"] is None and subagents_per_session:
                summary["parent"] = sid
                for k in range(subagents_per_session):
                    child = str(uuid.uuid4())
                    _write_session(home,
                                   _header(child, cwd, created + 60_000, depth=1, parent=sid),
                                   SUBTASK_TITLES[k % len(SUBTASK_TITLES)],
                                   events=1)
                    session_ids.append(child)
                    summary["subagents"].append(child)
                    summary["sessions"].append({"id": child, "title": SUBTASK_TITLES[k % len(SUBTASK_TITLES)],
                                                "cwd": cwd, "workspace": ws_id, "subagent": True})
        table[ws_id] = {"path": cwd, "title": title, "sessionIds": session_ids,
                        "createdAt": _iso(now), "updatedAt": _iso(now)}
        order.append(ws_id)
        summary["workspaces"].append({"id": ws_id, "path": cwd, "title": title})

    engine.makedirs(engine.storage_file(home).parent)
    engine.write_text(engine.storage_file(home), json.dumps({
        "unit": {"name": "workspace", "version": 2},
        "global": {"initialized": True, "defaultWorkspaceId": order[0],
                   "workspaceIds": order, "archivedSessionIds": [], "pinnedSessionIds": []},
        "tables": {"workspaces": table},
    }, ensure_ascii=False, indent=2) + "\n")

    engine.makedirs(engine.usage_file(home).parent)
    engine.write_text(engine.usage_file(home), json.dumps({
        "schema": {"name": "usage", "version": 1},
        "sessions": [{"id": s["id"], "cwd": s["cwd"], "lastSeq": 3,
                      "indexedAt": now, "activities": []} for s in summary["sessions"]],
    }, ensure_ascii=False, indent=2))
    return summary


def _iso(ms: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ms / 1000))


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else Path.cwd() / "demo-dsh-home"
    if target.exists():
        engine.rmtree(target)
    summary = build(target)
    print(f"已生成演示数据：{target}")
    print(f"  工作区 {len(summary['workspaces'])} 个，会话 {len(summary['sessions'])} 个"
          f"（其中子代理 {len(summary['subagents'])} 个）")
    print(f"  主会话：{summary['parent']}")
    print("用法： DSH_HOME=" + str(target) + " 再启动 DSH工作区迁移器.exe --list")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
