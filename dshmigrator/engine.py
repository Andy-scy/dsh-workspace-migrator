"""
DSH 工作区迁移器 —— 核心引擎（不依赖 GUI，可独立测试）

迁移一个 DSH 会话到另一个工作区，需要同时改齐三处（详见 README）：

  1. 日志第 0 个 zstd 帧里的 header.cwd
  2. 日志文件所在目录（目录名 = projectKey(cwd)）
  3. storages/workspace.json 里的会话归属

外加两个缓存：session_projcache（按 cwd 做身份校验，失配即作废）和
usage-stats（记录 cwd）。日志第 0 帧之后的事件帧一律原样搬运，上下文零改动。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import zstandard as zstd

SESSION_FILE = "session.v4.jsonl.zstd"
ZSTD_MAGIC = 4247762216
DSH_HOME_ENV = "DSH_HOME"
# DSH 桌面版会把 home 放在安装目录下的 .dsh，这里作为探测候选列出
COMMON_HOME_DIRS = (r"C:\DeepSeek harness", r"C:\Program Files\DeepSeek Harness")
# 进程名匹配（不区分大小写，子串匹配，兼容改名后的安装）
DSH_PROCESS_HINTS = ("deepseek harness",)

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class MigrationError(Exception):
    """可预期的失败，消息直接展示给用户。"""


def humanize_error(exc: BaseException) -> str:
    """把底层异常翻译成「看得懂 + 知道下一步做什么」的话。

    界面上直接抛 [WinError 5] 拒绝访问 这种原文，用户没法自救，必须同时给出
    原因和可执行的动作。
    """
    if isinstance(exc, MigrationError):
        return str(exc)

    winerror = getattr(exc, "winerror", None)
    filename = getattr(exc, "filename", None) or ""
    where = f"\n\n涉及路径：{filename}" if filename else ""
    message = str(exc)

    if isinstance(exc, PermissionError) or winerror == 5:
        return ("权限不足，无法写入 DSH 目录。" + where + "\n\n"
                "按可能性排序的原因：\n"
                "1) 本程序正放在 DSH 的工作区目录里运行。DSH 会给工作区打上 Windows 的\n"
                "   「低完整性」标签，工作区内的程序只能写工作区、写不了 .dsh，\n"
                "   而会话日志 / workspace.json / 备份都在 .dsh 里。\n"
                "   → 把整个程序文件夹移到工作区之外再运行（程序启动时会自检并提前提示）。\n"
                "2) DeepSeek Harness 仍在运行，占着这些文件。\n"
                "   → 完全退出 DSH 后重试。\n"
                "3) 安全软件 / 杀毒软件拦截了写入。\n"
                "   → 临时放行后重试。")

    if winerror == 32 or "being used by another process" in message:
        return ("文件正被占用，无法改写。" + where + "\n\n"
                "几乎总是因为 DeepSeek Harness 还在运行。请完全退出 DSH\n"
                "（在任务管理器里确认没有 “DeepSeek Harness.exe”）后重试。")

    if isinstance(exc, FileNotFoundError) or winerror == 3:
        return ("找不到路径，操作已中止。" + where + "\n\n"
                "目标目录可能已被删除、改名或移动。请确认目录存在后重试；\n"
                "若目标工作区目录不存在，可点「新建目录…」创建一个。")

    if winerror in (28, 112) or getattr(exc, "errno", None) == 28:
        return "磁盘空间不足，操作已中止。" + where + "\n\n请清理磁盘后重试。"

    if isinstance(exc, zstd.ZstdError):
        return ("会话日志解压失败：可能日志损坏，或不是 DSH 的 zstd 格式。" + where + "\n\n"
                "未做任何改动。可先在 DSH 里打开该会话确认它本身是否正常。")

    if isinstance(exc, json.JSONDecodeError):
        return ("JSON 解析失败，文件可能已损坏。" + where + "\n\n"
                "未做任何改动。若涉及 workspace.json，可从 migration-backups 里的备份恢复。")

    return f"{exc.__class__.__name__}: {message}"


# --------------------------------------------------------------------------
# DSH 路径算法（逐字符复刻 dsh-session-persistence-jsonl）
# --------------------------------------------------------------------------
def project_key(cwd: str) -> str:
    """会话所属的项目目录名。"""
    if not cwd:
        raise MigrationError("会话没有记录工作目录，无法定位它的日志")
    readable = []
    separator_run = False
    for ch in cwd:
        code = ord(ch)
        if ch in ("/", "\\", ":"):
            if not separator_run:
                readable.append("-")
            separator_run = True
        elif ch != "~" and (ch.isascii() and (ch.isalnum() or ch in "._-")):
            readable.append(ch)
            separator_run = False
        else:
            readable.append("~" + format(code, "04X"))
            separator_run = False
    text = "".join(readable).lstrip("-") or "root"
    return "--" + text[:251] + "--"


def encode_segment(value: str) -> str:
    """把会话 id 编码成一个安全路径段（与 DSH 一致）。"""
    out = []
    for ch in value:
        if ch != "~" and (ch.isascii() and (ch.isalnum() or ch in "._-")):
            out.append(ch)
        else:
            out.append("~" + format(ord(ch), "04X"))
    return "".join(out)


def canonical(path) -> str | None:
    """realpath 规范化后的字符串（与 Node 的 fs.realpath 同义）；不可解析返回 None。"""
    text = os.fspath(path)
    try:
        return os.path.realpath(text)
    except OSError:
        pass
    try:
        long_form = os.path.realpath(lp(text))
        return long_form[4:] if long_form.startswith("\\\\?\\") else long_form
    except OSError:
        return None


def same_path(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    ca, cb = canonical(a), canonical(b)
    if ca is not None and cb is not None:
        return ca == cb
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


# --------------------------------------------------------------------------
# 长路径支持
#
# 会话日志的真实路径可以是：
#   <DSH_HOME>\sessions\--C-...--\session-<uuid>\session.v4.jsonl.zstd
# 其中 projectKey 段最长 255、session id 段最长 44，很容易顶到 Windows 的
# MAX_PATH(260)。这里在超过阈值时自动加 \\?\ 前缀，读写/移动/删除都不受限制。
# --------------------------------------------------------------------------

LONG_PATH_THRESHOLD = 240


def lp(path) -> str:
    """用于系统调用的绝对路径。

    Windows 上统一加 \\\\?\\ 前缀：会话日志的真实路径（DSH_HOME + projectKey 段
    最长 255 + session id 段最长 44）本来就容易顶到 MAX_PATH，而“短相对路径的
    深层子项”同样会超限，所以这里不做长度判断，一律走长路径形式。
    """
    text = os.fspath(path)
    if text.startswith("\\\\?\\"):
        return text
    absolute = os.path.normpath(os.path.abspath(text))
    if sys.platform != "win32":
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def read_bytes(path) -> bytes:
    with open(lp(path), "rb") as fh:
        return fh.read()


def write_bytes(path, data: bytes) -> None:
    with open(lp(path), "wb") as fh:
        fh.write(data)


def read_text(path, encoding: str = "utf-8") -> str:
    with open(lp(path), "r", encoding=encoding) as fh:
        return fh.read()


def write_text(path, text: str, encoding: str = "utf-8") -> None:
    with open(lp(path), "w", encoding=encoding, newline="") as fh:
        fh.write(text)


def exists(path) -> bool:
    return os.path.exists(lp(path))


def is_dir(path) -> bool:
    return os.path.isdir(lp(path))


def is_file(path) -> bool:
    return os.path.isfile(lp(path))


def unlink(path) -> None:
    try:
        os.remove(lp(path))
    except FileNotFoundError:
        pass


def makedirs(path) -> None:
    os.makedirs(lp(path), exist_ok=True)


def replace(src, dst) -> None:
    os.replace(lp(src), lp(dst))


def rmtree(path) -> None:
    """长路径安全的递归删除（shutil.rmtree 不带 \\\\?\\ 前缀，会在大深度路径上失败）。"""
    target = lp(path)
    if not os.path.exists(target):
        return
    for root, dirs, files in os.walk(target, topdown=False):
        for name in files:
            try:
                os.remove(os.path.join(root, name))
            except OSError:
                pass
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except OSError:
                pass
    try:
        os.rmdir(target)
    except OSError:
        shutil.rmtree(target, ignore_errors=True)


def copytree(src, dst) -> None:
    shutil.copytree(lp(src), lp(dst))


def copy2(src, dst) -> None:
    shutil.copy2(lp(src), lp(dst))


def iterdir(path) -> list[Path]:
    base = Path(os.fspath(path))
    try:
        entries = list(os.scandir(lp(path)))
    except OSError:
        return []
    return [base / entry.name for entry in entries]


def write_preflight(home: Path) -> str | None:
    """开机自检：本进程能不能在 DSH 目录里建目录？不能就返回人话解释。

    典型场景：工具被放在 DSH 工作区目录里。DSH 的沙箱给工作区打了 Windows 的
    Low 完整性标签，Windows 的「禁止向上写」规则让工作区里的程序只能写工作区，
    而 .dsh（无标签 = 隐式 Medium）写不进去 —— 表现就是能读能列、一迁移就报
    「拒绝访问」。这里主动探测并给出可执行的建议。
    """
    probe = Path(home) / f".dsh-migrator-write-probe-{os.getpid()}"
    try:
        makedirs(probe)
    except OSError as exc:
        return (f"当前程序无法在 DSH 目录（{home}）里创建文件：{exc}\n\n"
                "最常见的原因：本工具放在 DSH 的工作区目录里了。DSH 会给工作区打上 Windows 的\n"
                "「低完整性」标签，而 Windows 规定低完整性进程只能写工作区内部，写不了 .dsh ——\n"
                "会话日志、workspace.json、备份都在 .dsh 里。\n\n"
                "解决：把整个工具文件夹移到工作区之外再运行。")
    finally:
        try:
            rmtree(probe)
        except OSError:
            pass
    return None


# --------------------------------------------------------------------------
# zstd 帧
# --------------------------------------------------------------------------

def scan_zstd_frames(buffer: bytes):
    """只按帧结构扫描边界，不解压 block（复刻 dsh 的 scanZstdFrames）。"""
    frames: list[tuple[int, int]] = []
    offset = 0
    size = len(buffer)
    while offset < size:
        start = offset
        if size - offset < 4:
            return frames, start
        if int.from_bytes(buffer[offset:offset + 4], "little") != ZSTD_MAGIC:
            raise MigrationError(f"日志损坏：第 {offset} 字节处不是 zstd 帧头")
        offset += 4
        if offset == size:
            return frames, start
        descriptor = buffer[offset]
        offset += 1
        if descriptor & 24:
            raise MigrationError("日志损坏：zstd 帧头保留位非 0")
        content_size_flag = descriptor >> 6
        single_segment = bool(descriptor & 32)
        checksum = bool(descriptor & 4)
        dict_flag = descriptor & 3
        dict_bytes = 4 if dict_flag == 3 else dict_flag
        fcs_bytes = (1 if single_segment else 0) if content_size_flag == 0 else (1 << content_size_flag)
        remaining = (0 if single_segment else 1) + dict_bytes + fcs_bytes
        if size - offset < remaining:
            return frames, start
        offset += remaining
        while True:
            if size - offset < 3:
                return frames, start
            block_header = int.from_bytes(buffer[offset:offset + 3], "little")
            offset += 3
            last_block = bool(block_header & 1)
            block_type = (block_header >> 1) & 3
            block_size = block_header >> 3
            if block_type == 3:
                raise MigrationError("日志损坏：zstd 保留块类型")
            payload = 1 if block_type == 1 else block_size
            if size - offset < payload:
                return frames, start
            offset += payload
            if last_block:
                break
        if checksum:
            if size - offset < 4:
                return frames, start
            offset += 4
        frames.append((start, offset))
    return frames, None


def _decompress_frame(data: bytes) -> bytes:
    obj = zstd.ZstdDecompressor().decompressobj()
    out = obj.decompress(data)
    try:
        out += obj.flush()
    except zstd.ZstdError:
        pass
    return out


def _compress_frame(data: bytes) -> bytes:
    # 与 DSH 的 ZSTD_c_checksumFlag=1 对齐；不写 content size，保持与 DSH 产物同形
    return zstd.ZstdCompressor(level=3, write_checksum=True, write_content_size=False).compress(data)


def _read_first_frame(path: Path, limit: int = 4 * 1024 * 1024):
    """只读日志的第 0 帧，避免整文件读入。"""
    chunk = 8192
    with open(lp(path), "rb") as fh:
        data = b""
        while len(data) < limit:
            piece = fh.read(chunk)
            if not piece:
                break
            data += piece
            try:
                frames, torn = scan_zstd_frames(data)
            except MigrationError:
                raise
            if frames:
                return data, frames[0]
            if torn is not None and torn > 0:
                break
    raise MigrationError(f"日志第 0 帧不完整：{path.name}")


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------

@dataclass
class SessionInfo:
    id: str
    cwd: str | None
    created_at: int
    title: str
    version: int
    log: Path
    size: int
    cwd_ok: bool
    workspace_id: str | None
    depth: int = 0
    origin: str | None = None
    parent: str | None = None

    @property
    def is_subagent(self) -> bool:
        """子代理会话：DSH 侧边栏不展示，迁移工具默认也应隐藏。"""
        return self.depth > 0 or self.origin == "subagent" or bool(self.parent)

    @property
    def created_text(self) -> str:
        try:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.created_at / 1000))
        except (OSError, ValueError, OverflowError):
            return "-"

    @property
    def size_text(self) -> str:
        n = float(self.size)
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} GB"

    @property
    def display_title(self) -> str:
        t = (self.title or "").strip().replace("\r", " ").replace("\n", " ")
        if not t:
            t = "（无标题）"
        return t if len(t) <= 60 else t[:59] + "…"


@dataclass
class WorkspaceInfo:
    id: str
    path: str
    title: str
    session_ids: list[str] = field(default_factory=list)
    path_ok: bool = True

    @property
    def display_title(self) -> str:
        return self.title or Path(self.path).name or self.path


@dataclass
class SessionMove:
    session_id: str
    from_cwd: str
    to_cwd: str
    from_dir: Path
    to_dir: Path
    from_log: Path
    to_log: Path


@dataclass
class Plan:
    home: Path
    target: str
    moves: list[SessionMove]
    db_before: dict
    db_after: dict
    db_changes: list[str]
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# 读取
# --------------------------------------------------------------------------

def home_candidates() -> list[Path]:
    """DSH home 的探测顺序：环境变量 → 用户目录 → 常见安装目录。"""
    candidates = []
    env = os.environ.get(DSH_HOME_ENV)
    if env:
        candidates.append(Path(env))
    candidates.append(Path.home() / ".dsh")
    for base in COMMON_HOME_DIRS:
        candidates.append(Path(base) / ".dsh")
    return candidates


def default_home() -> Path:
    """挑一个看起来真的装着 DSH 数据的 home；都没有就返回第一个候选。"""
    candidates = home_candidates()
    for candidate in candidates:
        if is_dir(candidate / "sessions") or is_file(candidate / "storages" / "workspace.json"):
            return candidate
    return candidates[0]


def sessions_root(home: Path) -> Path:
    return Path(home) / "sessions"


def storage_file(home: Path) -> Path:
    return Path(home) / "storages" / "workspace.json"


def projcache_file(home: Path, session_id: str) -> Path:
    return Path(home) / "storages" / "session_projcache" / "sessions" / f"{session_id}.json"


def usage_file(home: Path) -> Path:
    return Path(home) / "usage-stats" / "index-v1.json"


def migrate_log_path(home: Path, cwd: str, session_id: str) -> Path:
    return sessions_root(home) / project_key(cwd) / encode_segment(session_id) / SESSION_FILE


def load_db(home: Path) -> dict:
    path = storage_file(home)
    if not is_file(path):
        raise MigrationError(f"找不到工作区表：{path}")
    try:
        return json.loads(read_text(path))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationError(f"工作区表无法解析：{exc}") from exc


def save_db(home: Path, db: dict) -> None:
    write_text(storage_file(home), json.dumps(db, ensure_ascii=False, indent=2) + "\n")


def _load_titles(home: Path) -> dict[str, str]:
    titles: dict[str, str] = {}
    root = Path(home) / "storages" / "session_projcache" / "sessions"
    if not is_dir(root):
        return titles
    for entry in iterdir(root):
        if entry.suffix.lower() != ".json":
            continue
        try:
            doc = json.loads(read_text(entry))
            val = doc.get("record", {}).get("rows", {}).get("title", {}).get("val")
            if isinstance(val, str) and val.strip():
                titles[entry.stem] = val.strip()
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
    return titles


def list_workspaces(home: Path) -> tuple[list[WorkspaceInfo], dict]:
    db = load_db(home)
    global_state = db.get("global", {})
    table = db.get("tables", {}).get("workspaces", {})
    order = global_state.get("workspaceIds", [])
    seen = set()
    result: list[WorkspaceInfo] = []
    for wid in list(order) + [k for k in table if k not in order]:
        if wid in seen or wid not in table:
            continue
        seen.add(wid)
        rec = table[wid]
        result.append(WorkspaceInfo(
            id=wid,
            path=rec.get("path", ""),
            title=rec.get("title", ""),
            session_ids=list(rec.get("sessionIds", [])),
            path_ok=is_dir(rec.get("path", "")),
        ))
    return result, db


def list_sessions(home: Path) -> list[SessionInfo]:
    """扫描 sessions/<projectKey>/<id>/session.v4.jsonl.zstd，只读第 0 帧。"""
    home = Path(home)
    root = sessions_root(home)
    if not is_dir(root):
        raise MigrationError(f"找不到会话目录：{root}")
    titles = _load_titles(home)
    by_path: dict[str, str] = {}
    if is_file(storage_file(home)):
        for ws in list_workspaces(home)[0]:
            c = canonical(ws.path)
            if c is not None:
                by_path[c] = ws.id

    sessions: list[SessionInfo] = []
    for project_dir in sorted((p for p in iterdir(root) if is_dir(p)), key=lambda p: p.name):
        for session_dir in sorted((p for p in iterdir(project_dir) if is_dir(p)), key=lambda p: p.name):
            log = session_dir / SESSION_FILE
            if not is_file(log):
                continue
            try:
                data, (start, end) = _read_first_frame(log)
                header = json.loads(_decompress_frame(data[start:end])[:-1].decode("utf-8"))
            except (MigrationError, zstd.ZstdError, json.JSONDecodeError, UnicodeDecodeError, IndexError):
                continue
            if not isinstance(header, dict) or header.get("type") != "session":
                continue
            sid = header.get("id")
            if not isinstance(sid, str):
                continue
            cwd = header.get("cwd")
            cwd_canon = canonical(cwd) if isinstance(cwd, str) else None
            sessions.append(SessionInfo(
                id=sid,
                cwd=cwd if isinstance(cwd, str) else None,
                created_at=int(header.get("createdAt") or 0),
                title=titles.get(sid, ""),
                version=int(header.get("version") or 0),
                log=log,
                size=os.path.getsize(lp(log)),
                cwd_ok=bool(cwd_canon and os.path.isdir(cwd_canon)),
                workspace_id=by_path.get(cwd_canon) if cwd_canon else None,
                depth=int(header.get("delegationDepth") or 0),
                origin=header.get("origin") if isinstance(header.get("origin"), str) else None,
                parent=header.get("parentSession") if isinstance(header.get("parentSession"), str) else None,
            ))
    sessions.sort(key=lambda s: (s.cwd or "", -s.created_at))
    return sessions


def dsh_processes() -> list[tuple[int, str]]:
    """当前正在运行的 DSH 进程（pid, 映像名）。"""
    if sys.platform != "win32":
        return []
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, timeout=20, creationflags=_CREATE_NO_WINDOW,
        ).stdout.decode("mbcs", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith('"'):
            continue
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) < 2:
            continue
        name = parts[0].strip('"')
        low = name.lower()
        if any(h in low for h in DSH_PROCESS_HINTS) and low.endswith(".exe"):
            try:
                found.append((int(parts[1]), name))
            except ValueError:
                found.append((0, name))
    return found


# --------------------------------------------------------------------------
# 迁移计划
# --------------------------------------------------------------------------

def build_plan(home: Path, session_ids: list[str], target_path: str) -> Plan:
    home = Path(home)
    if not session_ids:
        raise MigrationError("没有选中任何会话")
    target = canonical(target_path)
    if target is None or not os.path.isdir(target):
        raise MigrationError(f"目标目录不存在：{target_path}")

    all_sessions = {s.id: s for s in list_sessions(home)}
    db = load_db(home)
    moves: list[SessionMove] = []
    notes: list[str] = []

    for sid in session_ids:
        info = all_sessions.get(sid)
        if info is None:
            raise MigrationError(f"找不到会话：{sid}")
        if info.cwd is None:
            raise MigrationError(f"会话 {sid} 的 header 没有 cwd，DSH 自己也无法把它归入工作区")
        if canonical(info.cwd) == target:
            notes.append(f"{info.display_title}：已经在该工作区，跳过")
            continue
        moves.append(SessionMove(
            session_id=sid,
            from_cwd=info.cwd,
            to_cwd=target,
            from_dir=info.log.parent,
            to_dir=migrate_log_path(home, target, sid).parent,
            from_log=info.log,
            to_log=migrate_log_path(home, target, sid),
        ))
    if not moves:
        raise MigrationError("选中的会话都已经在目标工作区里了")

    db_after, changes = _plan_db_edit(db, [m.session_id for m in moves], target)
    return Plan(home=home, target=target, moves=moves, db_before=db,
                db_after=db_after, db_changes=changes, notes=notes)


def _plan_db_edit(db: dict, session_ids: list[str], target: str):
    table = db.get("tables", {}).get("workspaces", {})
    changes: list[str] = []
    after = json.loads(json.dumps(db, ensure_ascii=False))

    target_id = None
    for wid, rec in table.items():
        if canonical(rec.get("path", "")) == target:
            target_id = wid
            break
    if target_id is None:
        target_id = str(uuid.uuid4())
        now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        after["tables"]["workspaces"][target_id] = {
            "path": target,
            "title": os.path.basename(target.rstrip("\\/")) or target,
            "sessionIds": [],
            "createdAt": now,
            "updatedAt": now,
        }
        after.setdefault("global", {}).setdefault("workspaceIds", [])
        after["global"]["workspaceIds"] = [target_id] + after["global"]["workspaceIds"]
        changes.append(f"新建工作区记录：{target}")
    else:
        changes.append(f"目标工作区已存在：{table[target_id].get('path')}")

    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    dst = after["tables"]["workspaces"][target_id]
    added = [s for s in session_ids if s not in dst.get("sessionIds", [])]
    dst["sessionIds"] = added + list(dst.get("sessionIds", []))
    dst["updatedAt"] = now
    if added:
        changes.append(f"把 {len(added)} 个会话登记到目标工作区")

    removed_total = 0
    for wid, rec in after["tables"]["workspaces"].items():
        if wid == target_id:
            continue
        before_ids = list(rec.get("sessionIds", []))
        keep = [s for s in before_ids if s not in session_ids]
        if len(keep) != len(before_ids):
            gone = len(before_ids) - len(keep)
            removed_total += gone
            rec["sessionIds"] = keep
            rec["updatedAt"] = now
            changes.append(f"从「{rec.get('title') or rec.get('path')}」注销 {gone} 个会话")
    validate_db(after)
    return after, changes


def validate_db(db: dict) -> None:
    """与 DSH 的 validateStoredState 同义的不变量检查。"""
    table = db.get("tables", {}).get("workspaces", {})
    order = list(db.get("global", {}).get("workspaceIds", []))
    if len(set(order)) != len(order):
        raise MigrationError("工作区表异常：workspaceIds 有重复")
    for wid in order:
        if wid not in table:
            raise MigrationError(f"工作区表异常：workspaceIds 引用了不存在的记录 {wid}")
    for wid in table:
        if wid not in order:
            raise MigrationError(f"工作区表异常：记录 {wid} 不在 workspaceIds 中（DSH 启动会报 inconsistent）")
    paths: dict[str, str] = {}
    accounted: dict[str, str] = {}
    for wid, rec in table.items():
        path = rec.get("path", "")
        if path in paths:
            raise MigrationError(f"工作区表异常：路径 {path} 被两条记录占用")
        paths[path] = wid
        for sid in rec.get("sessionIds", []):
            if sid in accounted:
                raise MigrationError(f"工作区表异常：会话 {sid} 被两个工作区同时登记")
            accounted[sid] = wid


# --------------------------------------------------------------------------
# 执行
# --------------------------------------------------------------------------

def _rewrite_header(log: Path, new_cwd: str) -> dict:
    """只重写第 0 帧，其余字节原样保留。"""
    data = read_bytes(log)
    frames, torn = scan_zstd_frames(data)
    if not frames or torn == 0:
        raise MigrationError(f"日志第 0 帧不完整，拒绝改写：{log}")
    start, end = frames[0]
    plain = _decompress_frame(data[start:end])
    if not plain or not plain.endswith(b"\n") or plain.count(b"\n") != 1:
        raise MigrationError(f"日志第 0 帧不是单行 header：{log}")
    header = json.loads(plain[:-1].decode("utf-8"))
    before = header.get("cwd")
    header["cwd"] = new_cwd
    line = (json.dumps(header, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    new_frame = _compress_frame(line)
    out = new_frame + data[end:]

    tmp = log.with_name(log.name + ".migrating")
    write_bytes(tmp, out)
    check_frames, check_torn = scan_zstd_frames(read_bytes(tmp))
    if len(check_frames) != len(frames) or check_torn is not None:
        unlink(tmp)
        raise MigrationError("改写自检失败（帧结构变化），已放弃")
    reloaded = json.loads(_decompress_frame(read_bytes(tmp)[check_frames[0][0]:check_frames[0][1]])[:-1].decode("utf-8"))
    if reloaded.get("cwd") != new_cwd or reloaded.get("id") != header.get("id"):
        unlink(tmp)
        raise MigrationError("改写自检失败（cwd/id 不符），已放弃")
    replace(tmp, log)
    return {"before": before, "after": new_cwd, "frames": len(frames),
            "bytes_before": len(data), "bytes_after": len(out)}


def _backup(plan: Plan) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = Path(plan.home) / "migration-backups" / f"gui-{stamp}"
    suffix = 1
    while exists(backup):
        backup = Path(plan.home) / "migration-backups" / f"gui-{stamp}-{suffix}"
        suffix += 1
    makedirs(backup / "sessions")
    manifest = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "home": str(plan.home),
                "target": plan.target, "sessions": []}
    for move in plan.moves:
        dest = backup / "sessions" / encode_segment(move.session_id)
        copytree(move.from_dir, dest)
        manifest["sessions"].append({
            "id": move.session_id,
            "from_cwd": move.from_cwd,
            "from_dir": str(move.from_dir),
            "from_log": str(move.from_log),
            "to_log": str(move.to_log),
        })
    if is_file(storage_file(plan.home)):
        copy2(storage_file(plan.home), backup / "workspace.json")
    for move in plan.moves:
        pc = projcache_file(plan.home, move.session_id)
        if is_file(pc):
            copy2(pc, backup / f"projcache-{encode_segment(move.session_id)}.json")
    if is_file(usage_file(plan.home)):
        copy2(usage_file(plan.home), backup / "usage-stats.json")
    write_text(backup / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return backup


def apply_plan(plan: Plan, allow_running: bool = False, progress=None) -> dict:
    """执行迁移。progress(stage, text) 可选回调。"""
    def emit(text):
        if progress:
            progress(text)

    if not allow_running:
        running = dsh_processes()
        if running:
            names = ", ".join(sorted({n for _, n in running}))
            raise MigrationError(f"检测到 DSH 正在运行（{names}），请先完全退出再迁移。")

    emit("正在备份…")
    try:
        backup = _backup(plan)
    except OSError as exc:
        raise MigrationError("备份失败，因此**没有做任何改动**（数据完好无损）。\n\n"
                             + humanize_error(exc)) from exc
    report = {"backup": str(backup), "moved": [], "notes": list(plan.notes)}

    for index, move in enumerate(plan.moves, start=1):
        emit(f"迁移 {index}/{len(plan.moves)} {Path(move.from_dir).name} …")
        try:
            if is_file(move.from_log):
                info = _rewrite_header(move.from_log, move.to_cwd)
                makedirs(move.to_dir)
                if is_file(move.to_log):
                    unlink(move.to_log)
                try:
                    replace(move.from_log, move.to_log)
                except OSError:
                    shutil.move(lp(move.from_log), lp(move.to_log))
                try:
                    os.rmdir(lp(move.from_dir))
                except OSError:
                    pass
                report["moved"].append({
                    "id": move.session_id,
                    "from": move.from_cwd,
                    "to": move.to_cwd,
                    "frames": info["frames"],
                    "bytes_before": info["bytes_before"],
                    "bytes_after": info["bytes_after"],
                })
            elif is_file(move.to_log):
                report["notes"].append(f"{move.session_id}：日志已在目标位置，仅补工作区归属")
            else:
                raise MigrationError(f"会话日志既不在原位置也不在目标位置：{move.session_id}")
        except MigrationError:
            raise
        except (OSError, zstd.ZstdError, json.JSONDecodeError) as exc:
            done = len(report["moved"])
            raise MigrationError(
                f"迁移中断：第 {index} 个会话（共 {len(plan.moves)} 个）出错。\n\n"
                f"出错会话：{move.session_id}\n"
                f"原本位置：{move.from_cwd}\n\n"
                + humanize_error(exc)
                + f"\n\n已经成功迁移 {done} 个会话，它们不会自动退回。\n"
                  f"备份目录：{backup}\n"
                  f"如需还原到迁移前的状态，用主界面的「回滚上次迁移」。"
            ) from exc

    emit("写入工作区表…")
    try:
        save_db(plan.home, plan.db_after)
    except OSError as exc:
        raise MigrationError(
            f"会话日志已迁移，但写入工作区表失败（第 {len(report['moved'])} 个会话已就位）。\n\n"
            + humanize_error(exc)
            + f"\n\n备份目录：{backup}\n"
              f"请用主界面的「回滚上次迁移」还原，或修好权限后重新执行一次迁移"
              f"（引擎是幂等的，日志已在目标位置的会话只会补登记）。"
        ) from exc

    emit("更新投影缓存…")
    for move in plan.moves:
        _retarget_projcache(plan.home, move.session_id, move.to_cwd)

    try:
        _patch_usage(plan)
    except (OSError, json.JSONDecodeError):
        pass

    emit("完成")
    return report


def _patch_usage(plan: Plan) -> None:
    path = usage_file(plan.home)
    if not is_file(path):
        return
    raw = read_text(path)
    doc = json.loads(raw)
    pretty = "\n" in raw
    changed = False
    for row in doc.get("sessions", []):
        if isinstance(row, dict) and row.get("id") in {m.session_id for m in plan.moves}:
            if row.get("cwd") != plan.target:
                row["cwd"] = plan.target
                changed = True
    if changed:
        text = json.dumps(doc, ensure_ascii=False, indent=2) if pretty else json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
        write_text(path, text + ("\n" if pretty else ""))


def _retarget_projcache(home: Path, session_id: str, new_cwd: str) -> bool:
    """把投影缓存的身份改成新目录，而不是删掉它。

    缓存的身份里含 cwd，迁移后必然失配；直接删掉会连标题等已算好的行一起丢，
    要等 DSH 冷读日志才重建。改写 cwd 后身份重新吻合，缓存继续有效。
    改不动就退回删除（留着失配的缓存没有意义，DSH 会自己重建）。
    """
    path = projcache_file(home, session_id)
    if not is_file(path):
        return False
    try:
        raw = read_text(path)
        doc = json.loads(raw)
        identity = doc.get("record", {}).get("identity")
        if not isinstance(identity, dict):
            raise ValueError("缓存里没有 identity")
        if identity.get("cwd") == new_cwd:
            return True
        identity["cwd"] = new_cwd
        pretty = "\n" in raw
        text = (json.dumps(doc, ensure_ascii=False, indent=2) if pretty
                else json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
        write_text(path, text + ("\n" if pretty else ""))
        return True
    except (OSError, json.JSONDecodeError, ValueError, AttributeError):
        try:
            unlink(path)
        except OSError:
            pass
        return False


def latest_backup(home: Path) -> Path | None:
    root = Path(home) / "migration-backups"
    if not is_dir(root):
        return None
    dirs = [d for d in iterdir(root) if is_dir(d) and is_file(d / "manifest.json")]
    if not dirs:
        return None
    return max(dirs, key=lambda d: os.path.getmtime(lp(d)))


def rollback(backup_dir: Path, allow_running: bool = False, progress=None) -> dict:
    backup = Path(backup_dir)
    manifest_path = backup / "manifest.json"
    if not is_file(manifest_path):
        raise MigrationError(f"备份目录无效（缺少 manifest.json）：{backup}")
    if not allow_running and dsh_processes():
        raise MigrationError("检测到 DSH 正在运行，请先完全退出再回滚。")
    manifest = json.loads(read_text(manifest_path))
    home = Path(manifest.get("home") or default_home())
    restored = []
    for item in manifest.get("sessions", []):
        src = backup / "sessions" / encode_segment(item["id"])
        if not is_dir(src):
            continue
        from_dir = Path(item["from_dir"])
        to_log = Path(item["to_log"])
        makedirs(from_dir.parent)
        if exists(from_dir):
            rmtree(from_dir)
        copytree(src, from_dir)
        to_dir = to_log.parent
        if exists(to_dir) and to_dir != from_dir:
            rmtree(to_dir)
        restored.append(item["id"])
        if progress:
            progress(f"已还原 {item['id']}")
    if is_file(backup / "workspace.json"):
        makedirs(storage_file(home).parent)
        copy2(backup / "workspace.json", storage_file(home))
    for item in manifest.get("sessions", []):
        pc = backup / f"projcache-{encode_segment(item['id'])}.json"
        if is_file(pc):
            dest = projcache_file(home, item["id"])
            makedirs(dest.parent)
            copy2(pc, dest)
    if is_file(backup / "usage-stats.json") and is_file(usage_file(home)):
        copy2(backup / "usage-stats.json", usage_file(home))
    return {"restored": restored, "backup": str(backup), "home": str(home)}


def verify(home: Path, session_ids: list[str], target: str) -> list[tuple[bool, str, str]]:
    """迁移后独立复核（供 GUI 展示）。"""
    home = Path(home)
    target_canon = canonical(target)
    checks: list[tuple[bool, str, str]] = []
    sessions = {s.id: s for s in list_sessions(home)}
    for sid in session_ids:
        info = sessions.get(sid)
        if info is None:
            checks.append((False, f"{sid} 未出现在会话列表中", ""))
            continue
        expected = migrate_log_path(home, target_canon, sid)
        checks.append((canonical(info.cwd) == target_canon, f"{sid[:28]}… 工作目录", info.cwd or "-"))
        checks.append((info.log == expected, f"{sid[:28]}… 日志路径与 header 自洽", str(info.log)))
        checks.append((info.cwd_ok, f"{sid[:28]}… 目标目录可访问", ""))
        pc = projcache_file(home, sid)
        if is_file(pc):
            try:
                doc = json.loads(read_text(pc))
                cached = doc.get("record", {}).get("identity", {}).get("cwd")
                checks.append((cached == target_canon, f"{sid[:28]}… 投影缓存已同步", str(cached)))
            except (OSError, json.JSONDecodeError, AttributeError) as exc:
                checks.append((False, f"{sid[:28]}… 投影缓存可读", str(exc)))
    try:
        db = load_db(home)
        validate_db(db)
        checks.append((True, "工作区表结构自检通过", f"{len(db['tables']['workspaces'])} 个工作区"))
        owners = [wid for wid, rec in db["tables"]["workspaces"].items() if any(s in rec.get("sessionIds", []) for s in session_ids)]
        checks.append((len(owners) == 1, "所选会话归属唯一", f"{len(owners)} 个持有者"))
    except MigrationError as exc:
        checks.append((False, "工作区表结构自检", str(exc)))
    return checks
