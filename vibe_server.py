"""
vibe_server.py — Mobile-Vibe-Monitor 后端中枢 v3.0
==================================================
FastAPI + WebSocket 远程协作服务器。
- 以进程当前 CWD 为绝对沙箱边界，所有手机端操作受路径约束。
- 全局状态机（IDLE / RUNNING / WAITING_CONFIRM）控制 VibeCoding 审批流。
- 后台轮询 git diff，变更时自动进入待审批并阻断。
- psutil 系统进程扫描，实时感知电脑端 AI 插件（Claude Code / Kimi）存活状态。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

# =============================================================================
# 动态工作区锁定 —— 进程启动时的 CWD 即不可突破的绝对沙箱底线
# =============================================================================

BASE_DIR: str = os.path.abspath(os.getcwd())
CURRENT_WORKSPACE: str = os.path.basename(BASE_DIR.rstrip(os.sep)) or "root"

# =============================================================================
# 权限沙箱 —— os.path.realpath + os.path.commonpath 硬校验
# =============================================================================

def is_safe_path(target_path: str) -> bool:
    try:
        resolved = os.path.realpath(os.path.join(BASE_DIR, target_path))
        base_resolved = os.path.realpath(BASE_DIR)
        return os.path.commonpath([resolved, base_resolved]) == base_resolved
    except (ValueError, OSError):
        return False


def safe_resolve(target_path: str) -> str:
    if not is_safe_path(target_path):
        raise ValueError(f"🚫 路径越界被阻止: {target_path}")
    return os.path.realpath(os.path.join(BASE_DIR, target_path))


# =============================================================================
# AI 插件进程感知
# =============================================================================

ACTIVE_AGENT: str = "OFFLINE"

# 特征指纹：命令行中包含以下任一子串即判定为对应 Agent
AGENT_FINGERPRINTS: dict[str, list[str]] = {
    "Claude Code": [
        "claude-code", "claude code", "@anthropic",
        "claude.ai", "claude-code.exe",
    ],
    "Kimi": [
        "kimi", "kimi-code", "moonshot",
        "kimi.moonshot", "kimi-code.exe",
    ],
}


def detect_active_agent() -> str:
    """
    快速扫描操作系统活动进程的命令行参数，
    匹配已知 AI 插件指纹。返回 agent_name。
    """
    try:
        for proc in psutil.process_iter(['cmdline']):
            try:
                cmdline = proc.info.get('cmdline')
                if not cmdline:
                    continue
                cmd = ' '.join(str(x) for x in cmdline).lower()
                for agent_name, fingerprints in AGENT_FINGERPRINTS.items():
                    for fp in fingerprints:
                        if fp in cmd:
                            return agent_name
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    except Exception:
        pass
    return "OFFLINE"


def detect_agent_workspace() -> tuple[str | None, str | None]:
    """
    从所有 VS Code 窗口标题中提取项目名和路径。
    返回 (项目名, 完整路径)，项目名至少保证有值（从标题提取），路径可能为 None。
    """
    import re

    # 简单可靠：不用 Add-Type，只用 Get-Process 的 MainWindowTitle
    ps_script = r'''
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$titles = @()

# 方法1：Get-Process MainWindowTitle
Get-Process -Name "Code" -ErrorAction SilentlyContinue | ForEach-Object {
    $t = $_.MainWindowTitle
    if ($t -and $t -ne 'Visual Studio Code') {
        $titles += $t
    }
}

# 方法2：System.Diagnostics.Process（可能找到更多实例）
[System.Diagnostics.Process]::GetProcessesByName('Code') | ForEach-Object {
    $t = $_.MainWindowTitle
    if ($t -and $t -ne 'Visual Studio Code' -and $t -notin $titles) {
        $titles += $t
    }
}

Write-Output ($titles -join "`n")
'''

    def _find_git_root(path_str: str) -> str | None:
        check = path_str
        for _ in range(10):
            if os.path.isdir(check) and os.path.isdir(os.path.join(check, '.git')):
                return os.path.realpath(check)
            parent = os.path.dirname(check)
            if parent == check:
                break
            check = parent
        return None

    def _search_project_dir(name: str) -> str | None:
        roots = [
            os.path.dirname(os.path.realpath(BASE_DIR)),
            r'D:\Myidea',
            'D:\\',
        ]
        for root in roots:
            if not os.path.isdir(root):
                continue
            try:
                for entry in os.listdir(root):
                    full = os.path.join(root, entry)
                    if not os.path.isdir(full):
                        continue
                    if entry.lower() == name.lower() or name.lower() in entry.lower() or entry.lower() in name.lower():
                        if os.path.isdir(os.path.join(full, '.git')):
                            return os.path.realpath(full)
            except PermissionError:
                continue
        # 深度搜索
        try:
            ps = (
                f'Get-ChildItem -Path D:\\ -Directory -Depth 2 -ErrorAction SilentlyContinue '
                f'| Where-Object {{ $_.Name -like "*{name}*" }} '
                f'| ForEach-Object {{ $_.FullName }}'
            )
            p = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", ps],
                capture_output=True, encoding="utf-8", errors="replace", timeout=20,
            )
            if p.returncode == 0 and p.stdout.strip():
                for line in p.stdout.strip().split('\n'):
                    line = line.strip()
                    if line and os.path.isdir(line) and os.path.isdir(os.path.join(line, '.git')):
                        return os.path.realpath(line)
        except Exception:
            pass
        return None

    try:
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            encoding="utf-8", errors="replace",
            timeout=10,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None, None

        titles = [t.strip() for t in proc.stdout.strip().split('\n') if t.strip()]
        if not titles:
            return None, None

        # 从所有窗口标题中提取项目名，选不同于当前工作区的
        current_ws = os.path.basename(os.path.realpath(BASE_DIR).rstrip(os.sep))
        best_proj = None
        for title in titles:
            if ' - Visual Studio Code' not in title:
                continue
            rest = title[:-len(' - Visual Studio Code')].strip()
            if ' - ' in rest:
                proj_name = rest.rsplit(' - ', 1)[-1].strip()
            else:
                proj_name = rest
            if not proj_name:
                continue
            if proj_name.lower() != current_ws.lower():
                best_proj = proj_name
                break
            if best_proj is None:
                best_proj = proj_name

        if not best_proj:
            return None, None

        full_path = _search_project_dir(best_proj)
        return best_proj, full_path

    except subprocess.TimeoutExpired:
        return None, None
    except Exception:
        return None, None


# =============================================================================
# 全局状态机
# =============================================================================

class AppState:

    def __init__(self) -> None:
        self.status: str = "IDLE"
        self.current_file: str = ""
        self.diff_text: str = ""
        self.thinking: str = ""
        self.pending_prompt: str = ""
        self.logs: list[str] = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "current_file": self.current_file,
            "diff_text": self.diff_text,
            "thinking": self.thinking,
            "pending_prompt": self.pending_prompt,
            "logs": self.logs[-30:],
        }


state = AppState()

# =============================================================================
# WebSocket 连接管理器
# =============================================================================

class ConnectionManager:

    def __init__(self) -> None:
        self._sockets: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._sockets.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self._sockets:
            self._sockets.remove(ws)

    async def broadcast(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message, ensure_ascii=False)
        stale: list[WebSocket] = []
        for ws in self._sockets:
            try:
                await ws.send_text(payload)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.disconnect(ws)

    async def send_to(self, ws: WebSocket, message: dict[str, Any]) -> None:
        try:
            await ws.send_text(json.dumps(message, ensure_ascii=False))
        except Exception:
            self.disconnect(ws)

    @property
    def active_count(self) -> int:
        return len(self._sockets)


manager = ConnectionManager()

# =============================================================================
# FastAPI 应用
# =============================================================================

app = FastAPI(title="Mobile-Vibe-Monitor", version="3.0.0")

_INDEX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")


@app.get("/")
async def root() -> HTMLResponse:
    try:
        with open(_INDEX_PATH, "r", encoding="utf-8") as fh:
            return HTMLResponse(content=fh.read())
    except FileNotFoundError:
        return HTMLResponse(
            content="<h2>index.html 未找到，请确认文件已放置于服务端同目录。</h2>",
            status_code=404,
        )


# =============================================================================
# WebSocket 端点
# =============================================================================

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await manager.connect(ws)

    # ① 连接时检测当前 VS Code 项目，以更新工作区名称
    global BASE_DIR, CURRENT_WORKSPACE
    loop = asyncio.get_running_loop()
    proj_name, new_path = await loop.run_in_executor(None, detect_agent_workspace)
    if proj_name:
        if new_path and os.path.isdir(new_path):
            BASE_DIR = new_path
        CURRENT_WORKSPACE = proj_name

    # ② 宣告工作区 + 当前 AI Agent 状态
    await manager.send_to(ws, {
        "type": "init_workspace",
        "workspace_name": CURRENT_WORKSPACE,
        "base_dir": BASE_DIR,
        "active_agent": ACTIVE_AGENT,
        "timestamp": time.time(),
    })

    # ③ 推送当前全局状态快照
    await manager.send_to(ws, {
        "type": "state_sync",
        "data": state.to_dict(),
        "active_agent": ACTIVE_AGENT,
    })

    try:
        while True:
            raw = await ws.receive_text()
            try:
                payload: dict[str, Any] = json.loads(raw)
            except json.JSONDecodeError:
                await manager.send_to(ws, {
                    "type": "error",
                    "message": "无效的 JSON 格式",
                })
                continue

            action = payload.get("action", "")
            await handle_action(ws, action, payload)

    except WebSocketDisconnect:
        manager.disconnect(ws)


# =============================================================================
# 指令处理中枢
# =============================================================================

async def handle_action(ws: WebSocket, action: str, payload: dict[str, Any]) -> None:

    # ---- 人工干预：发送键盘数字键到 Claude Code 对话框 ----
    if action == "intervene":
        if ACTIVE_AGENT == "OFFLINE":
            await manager.send_to(ws, {
                "type": "error",
                "message": "AI 插件已断联，无法干预",
            })
            return
        key: str = str(payload.get("key", "1"))[:1]  # 只取第一个字符，限数字键
        if key not in ("1", "2", "3"):
            await manager.send_to(ws, {
                "type": "error",
                "message": f"无效的干预键: {key}",
            })
            return

        # 后台线程执行 SendKeys 发送数字键
        loop = asyncio.get_running_loop()
        ok, msg = await loop.run_in_executor(None, _send_key_to_claude, key)
        if ok:
            await broadcast_log(f"⌨️ 已发送按键 [{key}] → {msg}")
        else:
            await broadcast_log(f"❌ 按键发送失败 [{key}]: {msg}")
        await manager.send_to(ws, {
            "type": "intervene_sent",
            "key": key,
            "message": msg,
        })

    # ---- 刷新工作区 ----
    elif action == "refresh_workspace":
        loop = asyncio.get_running_loop()
        proj_name, new_path = await loop.run_in_executor(None, detect_agent_workspace)
        if proj_name:
            global BASE_DIR, CURRENT_WORKSPACE
            if new_path and os.path.isdir(new_path):
                BASE_DIR = new_path
            CURRENT_WORKSPACE = proj_name
            await manager.broadcast({
                "type": "workspace_switch",
                "workspace_name": CURRENT_WORKSPACE,
                "base_dir": BASE_DIR,
                "timestamp": time.time(),
            })
            await broadcast_log(f"📁 工作区已刷新: {CURRENT_WORKSPACE}")
            await broadcast_state()
            await manager.send_to(ws, {
                "type": "workspace_refreshed",
                "workspace_name": CURRENT_WORKSPACE,
            })
        else:
            await manager.send_to(ws, {
                "type": "error",
                "message": "未检测到 VS Code 窗口，请确认 VS Code 已打开",
            })

    # ---- 紧急暂停 ----
    elif action == "pause":
        state.status = "IDLE"
        state.thinking = "⏸️ 已紧急暂停，等待恢复指令…"
        state.pending_prompt = ""
        await broadcast_state()

    # ---- 提示词修正 ----
    elif action == "prompt":
        if ACTIVE_AGENT == "OFFLINE":
            await manager.send_to(ws, {
                "type": "error",
                "message": "AI 插件已断联，提示词无法送达",
            })
            return
        prompt_text: str = payload.get("text", "").strip()
        if not prompt_text:
            await manager.send_to(ws, {
                "type": "error",
                "message": "提示词内容不能为空",
            })
            return
        state.pending_prompt = prompt_text
        state.status = "RUNNING"
        state.thinking = f"💭 {ACTIVE_AGENT} 正在思考: {prompt_text[:60]}{'…' if len(prompt_text) > 60 else ''}"
        await broadcast_state()

        # 立即回复发送者
        await manager.send_to(ws, {
            "type": "prompt_received",
            "length": len(prompt_text),
            "message": f"已送达 {ACTIVE_AGENT}，正在粘贴到对话框…",
        })

        # 后台线程执行剪贴板粘贴 + SendKeys
        loop = asyncio.get_running_loop()
        ok, msg = await loop.run_in_executor(None, _deliver_prompt_to_claude, prompt_text)
        if ok:
            state.thinking = f"✅ {msg}"
            await broadcast_log(f"📤 {msg}")
        else:
            state.thinking = f"❌ {msg}"
            await broadcast_log(f"❌ {msg}")
        state.status = "IDLE"
        state.pending_prompt = ""
        await broadcast_state()

    # ---- 安全立项 / 新会话 ----
    elif action == "new_session":
        folder_name: str = payload.get("folder_name", "").strip()
        if not folder_name:
            await manager.send_to(ws, {
                "type": "error",
                "message": "文件夹名称不能为空",
            })
            return
        if os.sep in folder_name or "/" in folder_name or ".." in folder_name:
            await manager.send_to(ws, {
                "type": "error",
                "message": "文件夹名称不得包含路径分隔符或 '..'",
            })
            return
        try:
            target = safe_resolve(folder_name)
            os.makedirs(target, exist_ok=True)
            await manager.broadcast({
                "type": "log",
                "message": f"[{CURRENT_WORKSPACE}] 新建子会话文件夹: {folder_name}",
            })
            await manager.send_to(ws, {
                "type": "session_created",
                "folder_name": folder_name,
                "path": target,
            })
        except ValueError as exc:
            await manager.send_to(ws, {
                "type": "error",
                "message": str(exc),
            })

    # ---- 安全检索 ----
    elif action == "search":
        keyword: str = payload.get("keyword", "").strip()
        if not keyword:
            await manager.send_to(ws, {
                "type": "error",
                "message": "搜索关键词不能为空",
            })
            return
        results = await search_in_workspace(keyword)
        await manager.send_to(ws, {
            "type": "search_results",
            "keyword": keyword,
            "results": results,
        })

    # ---- 列出文件 ----
    elif action == "list_files":
        subpath: str = payload.get("path", "").strip()
        try:
            target_dir = safe_resolve(subpath) if subpath else BASE_DIR
            if not os.path.isdir(target_dir):
                await manager.send_to(ws, {
                    "type": "error",
                    "message": "路径不是目录",
                })
                return
            entries: list[dict[str, Any]] = []
            with os.scandir(target_dir) as it:
                for entry in it:
                    entries.append({
                        "name": entry.name,
                        "is_dir": entry.is_dir(),
                        "size": entry.stat().st_size if entry.is_file() else 0,
                    })
            entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
            await manager.send_to(ws, {
                "type": "file_list",
                "path": subpath or ".",
                "entries": entries[:80],
            })
        except ValueError as exc:
            await manager.send_to(ws, {
                "type": "error",
                "message": str(exc),
            })

    # ---- 读取文件 ----
    elif action == "read_file":
        file_path: str = payload.get("path", "").strip()
        if not file_path:
            await manager.send_to(ws, {
                "type": "error",
                "message": "文件路径不能为空",
            })
            return
        try:
            target = safe_resolve(file_path)
            if not os.path.isfile(target):
                await manager.send_to(ws, {
                    "type": "error",
                    "message": "文件不存在",
                })
                return
            size = os.path.getsize(target)
            if size > 512 * 1024:
                await manager.send_to(ws, {
                    "type": "error",
                    "message": f"文件过大 ({size / 1024:.1f} KB)，拒绝读取",
                })
                return
            with open(target, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
            await manager.send_to(ws, {
                "type": "file_content",
                "path": file_path,
                "content": content,
            })
        except ValueError as exc:
            await manager.send_to(ws, {
                "type": "error",
                "message": str(exc),
            })

    # ---- 获取 git diff ----
    elif action == "get_git_diff":
        diff = get_git_diff()
        await manager.send_to(ws, {
            "type": "git_diff",
            "diff": diff,
        })

    # ---- 心跳 ----
    elif action == "ping":
        await manager.send_to(ws, {
            "type": "pong",
            "workspace": CURRENT_WORKSPACE,
            "active_connections": manager.active_count,
            "active_agent": ACTIVE_AGENT,
            "timestamp": time.time(),
        })

    else:
        await manager.send_to(ws, {
            "type": "error",
            "message": f"未知 action: {action}",
        })


# =============================================================================
# 广播辅助
# =============================================================================

async def broadcast_state() -> None:
    """广播当前 AppState + active_agent。"""
    await manager.broadcast({
        "type": "state_sync",
        "data": state.to_dict(),
        "active_agent": ACTIVE_AGENT,
    })


async def broadcast_log(message: str) -> None:
    await manager.broadcast({
        "type": "log",
        "message": f"[{CURRENT_WORKSPACE}] {message}",
    })


# =============================================================================
# 工具函数
# =============================================================================

TEXT_EXTENSIONS = frozenset({
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".scss", ".less",
    ".json", ".md", ".txt", ".yaml", ".yml", ".toml", ".cfg", ".ini",
    ".sh", ".bat", ".ps1", ".go", ".rs", ".java", ".c", ".cpp", ".h", ".hpp",
    ".vue", ".svelte", ".sql", ".rb", ".php", ".swift", ".kt", ".dart",
    ".xml", ".svg", ".r", ".m", ".mm", ".pl", ".lua", ".zig", ".nim",
})

SKIP_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv", ".vibe_env",
    ".idea", ".vscode", "dist", "build", ".next", ".nuxt", "target",
    "__MACOSX", ".DS_Store",
})


async def search_in_workspace(keyword: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    keyword_lower = keyword.lower()
    max_results = 30

    def _walk() -> list[dict[str, Any]]:
        local: list[dict[str, Any]] = []
        for root, dirs, files in os.walk(BASE_DIR):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for fname in files:
                if len(local) >= max_results:
                    return local
                _base, ext = os.path.splitext(fname)
                if ext.lower() not in TEXT_EXTENSIONS:
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                        for lineno, line in enumerate(fh, 1):
                            if keyword_lower in line.lower():
                                rel = os.path.relpath(fpath, BASE_DIR)
                                local.append({
                                    "file": rel,
                                    "line": lineno,
                                    "content": line.strip()[:200],
                                })
                                if len(local) >= max_results:
                                    return local
                except OSError:
                    continue
        return local

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _walk)


def get_git_diff() -> str:
    try:
        r1 = subprocess.run(
            ["git", "-C", BASE_DIR, "diff", "--stat"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=10,
        )
        if not r1.stdout.strip():
            return ""
        r2 = subprocess.run(
            ["git", "-C", BASE_DIR, "diff", "--unified=3"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=10,
        )
        detailed = r2.stdout.strip()
        if len(detailed) > 12000:
            detailed = detailed[:12000] + "\n… (diff 过长已截断)"
        return detailed
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def get_git_status_porcelain() -> str:
    try:
        r = subprocess.run(
            ["git", "-C", BASE_DIR, "status", "--porcelain"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=8,
        )
        return r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def _find_claude_binary() -> str | None:
    """定位 Claude Code CLI 可执行文件路径。"""
    import glob as _glob
    # 1) 扫描 VS Code 扩展目录
    pattern = os.path.expandvars(
        r"%USERPROFILE%\.vscode\extensions\anthropic.claude-code-*\resources\native-binary\claude.exe"
    )
    matches = sorted(_glob.glob(pattern), reverse=True)
    if matches:
        return matches[0]
    # 2) 检查 PATH
    for d in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(d, "claude.exe")
        if os.path.isfile(candidate):
            return candidate
    return None


def _git_changed_files() -> list[str]:
    """返回工作区中已修改但未提交的文件列表。"""
    try:
        r = subprocess.run(
            ["git", "-C", BASE_DIR, "diff", "--name-only"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=10,
        )
        return [f for f in r.stdout.strip().split("\n") if f]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []


def _git_approve() -> tuple[bool, str]:
    """暂存所有变更并提交，表示批准 AI 的修改。"""
    changed = _git_changed_files()
    if not changed:
        return True, "没有需要批准的变更"
    try:
        subprocess.run(
            ["git", "-C", BASE_DIR, "add", "-A"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=15,
            check=True,
        )
        subprocess.run(
            ["git", "-C", BASE_DIR, "commit", "-m",
             "✅ Approved via Mobile-Vibe-Monitor"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=15,
            check=True,
        )
        return True, f"已批准并提交 {len(changed)} 个文件"
    except subprocess.CalledProcessError as e:
        return False, f"Git 操作失败: {e.stderr.strip()[:200]}"


def _git_reject() -> tuple[bool, str]:
    """丢弃所有未提交的变更，回到干净状态。"""
    changed = _git_changed_files()
    if not changed:
        return True, "没有需要拒绝的变更"
    try:
        subprocess.run(
            ["git", "-C", BASE_DIR, "checkout", "--", "."],
            capture_output=True, encoding="utf-8", errors="replace", timeout=15,
            check=True,
        )
        # 也清理未跟踪文件
        subprocess.run(
            ["git", "-C", BASE_DIR, "clean", "-fd"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=15,
            check=False,
        )
        return True, f"已丢弃 {len(changed)} 个文件的变更"
    except subprocess.CalledProcessError as e:
        return False, f"Git 操作失败: {e.stderr.strip()[:200]}"


def _deliver_prompt_to_claude(prompt_text: str) -> tuple[bool, str]:
    """将提示词写入 UTF-8 临时文件，通过 PowerShell 设置剪贴板并粘贴到前台窗口。
    返回 (成功与否, 消息)。"""
    import tempfile
    try:
        # 1) 将提示词写入 UTF-8 临时文件（避免 stdout 管道编码问题）
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", encoding="utf-8", delete=False
        )
        tmp.write(prompt_text)
        tmp.close()

        # 2) PowerShell 读取 UTF-8 文件 → 设置剪贴板 → 粘贴并发送
        ps_script = (
            f'Add-Type -AssemblyName System.Windows.Forms;'
            f'$text = Get-Content -Path "{tmp.name}" -Encoding UTF8 -Raw;'
            f'[System.Windows.Forms.Clipboard]::SetText($text);'
            f'[System.Windows.Forms.SendKeys]::SendWait("^v");'
            f'Start-Sleep -Milliseconds 300;'
            f'[System.Windows.Forms.SendKeys]::SendWait("{{ENTER}}");'
        )
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            encoding="utf-8", errors="replace",
            timeout=15,
            cwd=BASE_DIR,
        )

        # 3) 清理临时文件
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

        if proc.returncode != 0:
            err = proc.stderr.strip()[:200] if proc.stderr else "未知错误"
            return False, f"PowerShell 执行失败: {err}"
        return True, "提示词已粘贴到 Claude Code 对话框"
    except subprocess.TimeoutExpired:
        return False, "PowerShell 执行超时"
    except Exception as e:
        return False, f"提示词投递异常: {e}"


def _send_key_to_claude(key: str) -> tuple[bool, str]:
    """通过 PowerShell SendKeys 发送单个数字键到当前活跃窗口（Claude Code）。
    用于手机端远程选择 Claude Code 的 1/2/3 选项。
    返回 (成功与否, 消息)。"""
    try:
        ps_script = (
            f'Add-Type -AssemblyName System.Windows.Forms;'
            f'[System.Windows.Forms.SendKeys]::SendWait("{key}");'
        )
        proc = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            encoding="utf-8", errors="replace",
            timeout=10,
        )
        if proc.returncode != 0:
            err = proc.stderr.strip()[:200] if proc.stderr else "未知错误"
            return False, f"SendKeys 失败: {err}"
        return True, f"按键 [{key}] 已发送到 Claude Code"
    except subprocess.TimeoutExpired:
        return False, "SendKeys 执行超时"
    except Exception as e:
        return False, f"SendKeys 异常: {e}"


# =============================================================================
# 后台协程 ① — AI 插件进程存活监控
# =============================================================================

async def monitor_agent_processes(interval: float = 2.5) -> None:
    """
    每 interval 秒扫描系统进程，检测 AI 插件是否在线。
    当 ACTIVE_AGENT 或工作区发生切换时，立即广播给所有手机端。
    """
    global ACTIVE_AGENT
    while True:
        await asyncio.sleep(interval)
        try:
            loop = asyncio.get_running_loop()
            agent_name = await loop.run_in_executor(None, detect_active_agent)
            if agent_name != ACTIVE_AGENT:
                previous = ACTIVE_AGENT
                ACTIVE_AGENT = agent_name

                await manager.broadcast({
                    "type": "agent_status_change",
                    "active_agent": ACTIVE_AGENT,
                    "previous_agent": previous,
                    "timestamp": time.time(),
                })
                await broadcast_state()

                if ACTIVE_AGENT == "OFFLINE" and state.status == "WAITING_CONFIRM":
                    state.status = "IDLE"
                    state.thinking = "⚠️ AI 插件已断联，待审批状态自动清除"
                    state.diff_text = ""
                    state.current_file = ""
                    await broadcast_state()

                status_icon = "🟢" if ACTIVE_AGENT != "OFFLINE" else "🔴"
                await broadcast_log(
                    f"{status_icon} AI 状态切换: {previous} → {ACTIVE_AGENT}"
                )
        except Exception:
            pass


# =============================================================================
# 后台协程 ② — Git Diff 变更轮询
# =============================================================================

_last_diff_hash: str = ""

async def monitor_workspace_changes(interval: float = 3.0) -> None:
    global _last_diff_hash
    while True:
        await asyncio.sleep(interval)
        try:
            diff = get_git_diff()
            status_text = get_git_status_porcelain()

            if diff:
                current_hash = hashlib.md5(diff.encode()).hexdigest()
                is_new = (current_hash != _last_diff_hash)
                _last_diff_hash = current_hash

                changed_files = [
                    line[3:].strip()
                    for line in status_text.split("\n")
                    if len(line) >= 3 and line[:3].strip()
                ] if status_text else []

                if is_new:
                    state.diff_text = diff
                    state.current_file = changed_files[0] if changed_files else "(多个文件)"

                    # 仅在 AI 在线时进入待审批
                    if ACTIVE_AGENT != "OFFLINE" and state.status in ("IDLE", "RUNNING"):
                        state.status = "WAITING_CONFIRM"
                        state.thinking = f"🔍 {ACTIVE_AGENT} 产生 {len(changed_files)} 个文件变更，等待审批…"
                        await manager.broadcast({
                            "type": "workspace_change",
                            "workspace_name": CURRENT_WORKSPACE,
                            "diff": diff,
                            "changed_files": changed_files,
                            "active_agent": ACTIVE_AGENT,
                            "timestamp": time.time(),
                        })
                        await broadcast_state()
                    elif ACTIVE_AGENT == "OFFLINE":
                        # AI 不在线，仅记录日志不进入待审批
                        state.diff_text = diff
                        state.current_file = changed_files[0] if changed_files else "(多个文件)"
                        await broadcast_state()
            else:
                _last_diff_hash = ""
                if state.status == "WAITING_CONFIRM":
                    state.status = "IDLE"
                    state.diff_text = ""
                    state.current_file = ""
                    state.thinking = ""
                    await broadcast_state()
        except Exception:
            pass


# =============================================================================
# 启动事件
# =============================================================================

@app.on_event("startup")
async def startup_event() -> None:
    # 初始检测一次 Agent 状态
    global ACTIVE_AGENT
    try:
        loop = asyncio.get_running_loop()
        ACTIVE_AGENT = await loop.run_in_executor(None, detect_active_agent)
    except Exception:
        ACTIVE_AGENT = "OFFLINE"

    asyncio.create_task(monitor_agent_processes())
    asyncio.create_task(monitor_workspace_changes())


# =============================================================================
# 入口
# =============================================================================

if __name__ == "__main__":
    import uvicorn

    BIND_PORT = 8000

    BANNER = rf"""
╔══════════════════════════════════════════════════════╗
║       Mobile-Vibe-Monitor  已启动  v3.0              ║
╠══════════════════════════════════════════════════════╣
║  工作区   : {CURRENT_WORKSPACE:<38} ║
║  沙箱路径 : {BASE_DIR:<38} ║
║  AI 插件  : {ACTIVE_AGENT:<38} ║
╠══════════════════════════════════════════════════════╣
║  [局域网调试]                                       ║
║  → http://localhost:{BIND_PORT}                            ║
║                                                     ║
║  [跨省远控调试]                                     ║
║  → 启动 cpolar 穿透 {BIND_PORT} 端口:                      ║
║    cpolar http {BIND_PORT}                                ║
║  → 手机端使用 cpolar 分配的公网 https 域名访问       ║
╚══════════════════════════════════════════════════════╝
"""

    print(BANNER)
    uvicorn.run(app, host="0.0.0.0", port=BIND_PORT, log_level="info")
