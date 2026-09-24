"""Agent tools shared by both backends.

The API backend exposes every tool here (plus server-side web search / fetch).
The Claude Code backend exposes only the team tools (board, git, notes, cycle control) as an
in-process MCP server and relies on Claude Code's built-in Read/Write/Edit/Bash/Grep/Glob/Web tools.
"""
from __future__ import annotations

import asyncio
import os
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .gitops import GitError, commit, recent_log
from .gitops import status as git_status
from .memory import AgentMemory
from .models import EFFORTS, MODEL_IDS
from .store import Store
from .types import ROLE_KEYS, AgentSpec, CycleState, HuntunConfig, InboxItem

MAX_TOOL_OUTPUT = 24_000
IGNORED_DIRS = {"node_modules", ".git", ".huntun", "dist", "build", ".next", "__pycache__", ".venv", "venv", "target", ".pytest_cache", ".mypy_cache"}


@dataclass
class ToolHooks:
    hire_agent: Callable[[dict[str, str]], Awaitable[str]] | None = None
    retire_agent: Callable[[str], Awaitable[str]] | None = None
    set_agent_model: Callable[[str, str | None, str | None], Awaitable[str]] | None = None
    resume_team: Callable[[], Awaitable[str]] | None = None
    set_goal: Callable[[str, str], Awaitable[str]] | None = None


@dataclass
class ToolContext:
    agent: AgentSpec
    team: Callable[[], list[AgentSpec]]
    workspace: Path
    store: Store
    memory: AgentMemory
    config: HuntunConfig
    cycle: CycleState
    hooks: ToolHooks = field(default_factory=ToolHooks)


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    run: Callable[[dict[str, Any], ToolContext], Awaitable[str]]
    master_only: bool = False
    api_only: bool = False  # covered by Claude Code built-ins on the claude-code backend


def truncate(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    head, tail = text[: int(limit * 0.7)], text[-int(limit * 0.25):]
    return f"{head}\n\n... [truncated {len(text) - len(head) - len(tail)} characters] ...\n\n{tail}"


def safe_path(workspace: Path, p: str, for_write: bool = False) -> Path:
    """Resolves a model-supplied path inside the workspace; rejects escapes and internal directories."""
    target = (workspace / p).resolve()
    try:
        rel = target.relative_to(workspace.resolve())
    except ValueError:
        raise ValueError(f"Path escapes the workspace: {p}") from None
    first = rel.parts[0] if rel.parts else ""
    if first == ".huntun":
        raise ValueError("The .huntun directory is managed by the orchestrator and is off limits.")
    if for_write and first == ".git":
        raise ValueError("Refusing to write inside .git")
    return target


def rel_path(workspace: Path, target: Path) -> str:
    return target.resolve().relative_to(workspace.resolve()).as_posix()


async def sh(command: str, cwd: Path, timeout: float) -> tuple[str, int | None, bool]:
    env = {**os.environ, "CI": "1", "FORCE_COLOR": "0", "GIT_TERMINAL_PROMPT": "0"}
    proc = await asyncio.create_subprocess_shell(
        command, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL, env=env
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
        timed_out = False
    except asyncio.TimeoutError:
        proc.kill()
        out, err = await proc.communicate()
        timed_out = True
    text = out.decode(errors="replace")
    if err:
        text += ("\n--- stderr ---\n" if text else "") + err.decode(errors="replace")
    return text, proc.returncode, timed_out


def _walk(directory: Path, root: Path, depth: int, out: list[str]) -> None:
    if depth < 0 or len(out) > 800:
        return
    try:
        entries = sorted(directory.iterdir(), key=lambda e: e.name)
    except OSError:
        return
    for e in entries:
        if e.name in IGNORED_DIRS:
            continue
        rel = rel_path(root, e)
        if e.is_dir():
            out.append(rel + "/")
            _walk(e, root, depth - 1, out)
        else:
            out.append(rel)


def human_confirmed(ctx: ToolContext, thread_id: Any) -> str | None:
    """Returns None when the human has replied in `thread_id` after the master's proposal there, else the reason it does not count."""
    try:
        tid = int(thread_id or 0)
    except (TypeError, ValueError):
        tid = 0
    if not tid or not ctx.store.get_thread(tid):
        return "post your proposal on the board tagging @human, wait for their reply in that thread, then pass its thread id as confirmation_thread_id"
    comments = ctx.store.get_comments(tid)
    thread = ctx.store.get_thread(tid) or {}
    posts = [{"author": thread.get("author"), "body": thread.get("body", ""), "id": 0}] + comments
    last_master = max((p["id"] for p in posts if p["author"] == ctx.agent.name), default=-1)
    human_after = [p for p in posts if p["author"] == "human" and p["id"] > last_master]
    if last_master < 0:
        return f"thread #{tid} has no proposal from you; propose the change there, tagging @human, and wait for their reply"
    if not human_after:
        return f"@human has not replied in thread #{tid} since your proposal; wait for their confirmation (do not proceed without it)"
    return None


def format_inbox(items: list[InboxItem]) -> str:
    lines = []
    for i in items:
        ref = f" comment #{i.comment_id}" if i.comment_id else ""
        body = i.body.replace("\n", "\n  ")
        lines.append(f'- [{i.kind}] thread #{i.thread_id} "{i.thread_title}"{ref} from @{i.author}:\n  {body}')
    return "\n".join(lines)


def collect_inbox(ctx: ToolContext) -> list[InboxItem]:
    """Unseen mentions plus new replies on the agent's threads; advances the agent's read cursor."""
    inbox = ctx.store.take_inbox(ctx.agent.name)
    if inbox and ctx.cycle.active_thread is None:
        ctx.cycle.active_thread = inbox[0].thread_id
    replies = ctx.store.new_comments_since(ctx.agent.name, ctx.memory.state.last_seen_comment_id)
    ctx.memory.state.last_seen_comment_id = ctx.store.max_comment_id()
    ctx.memory.save_state()
    seen = {i.comment_id for i in inbox}
    extra = [InboxItem("reply", r["thread_id"], r["title"], r["id"], r["author"], r["body"], r["created_at"]) for r in replies if r["id"] not in seen]
    return inbox + extra


# ---- tool implementations -------------------------------------------------------------


async def _read_file(a: dict[str, Any], ctx: ToolContext) -> str:
    p = safe_path(ctx.workspace, str(a["path"]))
    if not p.exists():
        return f"ERROR: {a['path']} does not exist"
    if p.is_dir():
        return f"ERROR: {a['path']} is a directory; use list_files"
    lines = p.read_text(errors="replace").split("\n")
    start = max(1, int(a.get("start_line") or 1))
    end = min(len(lines), int(a.get("end_line") or len(lines)))
    body = "\n".join(f"{n:5d}\t{lines[n - 1]}" for n in range(start, end + 1))
    return truncate(f"{a['path']} ({len(lines)} lines)\n{body}")


async def _write_file(a: dict[str, Any], ctx: ToolContext) -> str:
    p = safe_path(ctx.workspace, str(a["path"]), for_write=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    content = str(a["content"])
    p.write_text(content)
    rel = rel_path(ctx.workspace, p)
    ctx.memory.touch(rel)
    return f"Wrote {rel} ({len(content)} chars)"


async def _edit_file(a: dict[str, Any], ctx: ToolContext) -> str:
    p = safe_path(ctx.workspace, str(a["path"]), for_write=True)
    if not p.exists():
        return f"ERROR: {a['path']} does not exist"
    text = p.read_text(errors="replace")
    old, new = str(a["old_string"]), str(a["new_string"])
    count = text.count(old)
    if count == 0:
        return "ERROR: old_string not found in file"
    if count > 1 and not a.get("replace_all"):
        return f"ERROR: old_string matches {count} times; make it unique or set replace_all"
    p.write_text(text.replace(old, new) if a.get("replace_all") else text.replace(old, new, 1))
    rel = rel_path(ctx.workspace, p)
    ctx.memory.touch(rel)
    return f"Edited {rel} ({count} replacement{'s' if count != 1 else ''})"


async def _list_files(a: dict[str, Any], ctx: ToolContext) -> str:
    p = safe_path(ctx.workspace, str(a.get("path") or "."))
    if not p.exists():
        return f"ERROR: {a.get('path')} does not exist"
    out: list[str] = []
    _walk(p, ctx.workspace, int(a.get("depth") or 3) - 1, out)
    return truncate("\n".join(out) or "(empty)")


async def _search_files(a: dict[str, Any], ctx: ToolContext) -> str:
    p = safe_path(ctx.workspace, str(a.get("path") or "."))
    excludes = " ".join(f"--exclude-dir={d}" for d in IGNORED_DIRS)
    cmd = f"grep -rnIE {excludes} -e {shlex.quote(str(a['pattern']))} {shlex.quote(str(p))} | head -n {int(a.get('max_results') or 200)}"
    out, _, _ = await sh(cmd, ctx.workspace, 30)
    out = out.replace(str(ctx.workspace.resolve()) + "/", "")
    return truncate(out.strip() or "(no matches)")


async def _run_command(a: dict[str, Any], ctx: ToolContext) -> str:
    timeout = min(600, max(5, int(a.get("timeout_sec") or 120)))
    out, code, timed_out = await sh(str(a["command"]), ctx.workspace, timeout)
    header = f"[timed out after {timeout}s]" if timed_out else f"[exit code {code}]"
    return truncate(f"{header}\n{out or '(no output)'}")


async def _git_status(a: dict[str, Any], ctx: ToolContext) -> str:
    st, log = await asyncio.gather(git_status(ctx.workspace), recent_log(ctx.workspace, 15))
    touched = ", ".join(ctx.memory.state.touched_files) or "(none)"
    return f"Uncommitted changes:\n{st or '(clean)'}\n\nRecent commits:\n{log}\n\nFiles you touched since your last commit: {touched}"


async def _git_commit(a: dict[str, Any], ctx: ToolContext) -> str:
    explicit = [str(f) for f in (a.get("files") or [])]
    files = explicit or list(ctx.memory.state.touched_files)
    note = "" if files else "(No tracked touched files; committed all pending changes in the tree.)\n"
    try:
        result = await commit(ctx.workspace, ctx.agent.name, str(a["message"]), files)
    except GitError as e:
        return f"ERROR: {e}"
    ctx.memory.state.touched_files = [f for f in ctx.memory.state.touched_files if f not in result.files]
    ctx.memory.save_state()
    ctx.cycle.commits.append(result.sha)
    ctx.store.log_event(ctx.agent.name, "commit", f"{result.sha} {a['message']}")
    body = f"{a['summary_body']}\n\n`{result.sha}` {a['message'].splitlines()[0]} · {len(result.files)} file{'s' if len(result.files) != 1 else ''}"
    thread_id = int(a.get("thread_id") or ctx.cycle.active_thread or 0)
    if thread_id and ctx.store.get_thread(thread_id):
        c = ctx.store.add_comment(thread_id, ctx.agent.name, body)
        ctx.memory.state.last_seen_comment_id = max(ctx.memory.state.last_seen_comment_id, c["id"])
        where = f"Posted the summary as a reply on thread #{thread_id}."
    else:
        thread = ctx.store.create_thread(ctx.agent.name, str(a["summary_title"]), body, result.sha)
        where = f"Posted board thread #{thread['id']}."
    items = collect_inbox(ctx)
    if items:
        inbox_text = (
            f"\n\nNew board activity for you since your last check ({len(items)}):\n{format_inbox(items)}\n\n"
            "Decide for each item whether to reply, act, or ignore. Reply on the thread if someone is waiting on you."
        )
    else:
        inbox_text = "\n\nNo new comments or mentions for you."
    return f"{note}Committed {result.sha} ({len(result.files)} files). {where}{inbox_text}"


async def _list_threads(a: dict[str, Any], ctx: ToolContext) -> str:
    threads = ctx.store.list_threads(int(a.get("limit") or 25))
    if not threads:
        return "(no threads yet)"
    return "\n".join(f'#{t["id"]} "{t["title"]}" by @{t["author"]} — {t["comment_count"]} comments, last activity {t["last_activity"]}' for t in threads)


async def _read_thread(a: dict[str, Any], ctx: ToolContext) -> str:
    t = ctx.store.get_thread(int(a["thread_id"]))
    if not t:
        return f"ERROR: thread #{a['thread_id']} not found"
    ctx.cycle.active_thread = t["id"]
    sha = f" (commit {t['commit_sha']})" if t.get("commit_sha") else ""
    head = f"# #{t['id']} {t['title']}\nby @{t['author']} at {t['created_at']}{sha}\n\n{t['body']}"
    rest = "".join(f"\n\n--- comment #{c['id']} by @{c['author']} at {c['created_at']} ---\n{c['body']}" for c in ctx.store.get_comments(t["id"]))
    return truncate(head + rest)


async def _post_thread(a: dict[str, Any], ctx: ToolContext) -> str:
    t = ctx.store.create_thread(ctx.agent.name, str(a["title"]), str(a["body"]))
    ctx.cycle.active_thread = t["id"]
    return f"Posted thread #{t['id']}"


async def _post_comment(a: dict[str, Any], ctx: ToolContext) -> str:
    try:
        c = ctx.store.add_comment(int(a["thread_id"]), ctx.agent.name, str(a["body"]))
    except ValueError as e:
        return f"ERROR: {e}"
    ctx.memory.state.last_seen_comment_id = max(ctx.memory.state.last_seen_comment_id, c["id"])
    ctx.memory.save_state()
    ctx.cycle.active_thread = c["thread_id"]
    return f"Posted comment #{c['id']} on thread #{c['thread_id']}"


async def _check_inbox(a: dict[str, Any], ctx: ToolContext) -> str:
    items = collect_inbox(ctx)
    return format_inbox(items) if items else "(nothing new)"


async def _update_notes(a: dict[str, Any], ctx: ToolContext) -> str:
    ctx.memory.save_notes(str(a["notes"]))
    return f"Notes saved ({len(str(a['notes']))} chars)"


async def _list_agents(a: dict[str, Any], ctx: ToolContext) -> str:
    statuses = ctx.store.agent_statuses()
    lines = []
    for ag in ctx.team():
        s = statuses.get(ag.name)
        live = f" ({s['status']}{': ' + s['task'] if s and s['task'] else ''})" if s else ""
        lines.append(f"@{ag.name} — {ag.title} [{ag.status}]{live}\n  {ag.brief}")
    return "\n".join(lines)


async def _finish_cycle(a: dict[str, Any], ctx: ToolContext) -> str:
    ctx.cycle.finished = True
    ctx.cycle.summary = str(a["summary"])
    ctx.cycle.next_task = str(a["next_task"])
    ctx.cycle.wait_for_mention = bool(a.get("wait_for_mention"))
    return "Cycle finished. You will sleep until someone tags you." if ctx.cycle.wait_for_mention else "Cycle finished. Stop now; do not call more tools."


async def _hire_agent(a: dict[str, Any], ctx: ToolContext) -> str:
    if not ctx.hooks.hire_agent:
        return "ERROR: hiring is not available in this context"
    problem = human_confirmed(ctx, a.get("confirmation_thread_id"))
    if problem:
        return f"ERROR: staffing changes need the human's confirmation: {problem}"
    return await ctx.hooks.hire_agent({"name": str(a["name"]), "role": str(a["role"]), "title": str(a["title"]), "brief": str(a["brief"]),
                                       "model": str(a.get("model") or ""), "effort": str(a.get("effort") or "")})


async def _set_agent_model(a: dict[str, Any], ctx: ToolContext) -> str:
    if not ctx.hooks.set_agent_model:
        return "ERROR: not available in this context"
    problem = human_confirmed(ctx, a.get("confirmation_thread_id"))
    if problem:
        return f"ERROR: model changes need the human's confirmation: {problem}"
    return await ctx.hooks.set_agent_model(str(a["name"]), a.get("model"), a.get("effort"))


async def _resume_team(a: dict[str, Any], ctx: ToolContext) -> str:
    if not ctx.hooks.resume_team:
        return "ERROR: not available in this context"
    return await ctx.hooks.resume_team()


async def _set_goal(a: dict[str, Any], ctx: ToolContext) -> str:
    if not ctx.hooks.set_goal:
        return "ERROR: not available in this context"
    problem = human_confirmed(ctx, a.get("confirmation_thread_id"))
    if problem:
        return f"ERROR: goal changes need the human's confirmation: {problem}"
    return await ctx.hooks.set_goal(str(a["goal"]), str(a.get("definition_of_done") or ""))


async def _retire_agent(a: dict[str, Any], ctx: ToolContext) -> str:
    if not ctx.hooks.retire_agent:
        return "ERROR: retiring is not available in this context"
    problem = human_confirmed(ctx, a.get("confirmation_thread_id"))
    if problem:
        return f"ERROR: staffing changes need the human's confirmation: {problem}"
    return await ctx.hooks.retire_agent(str(a["name"]))


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


TOOLS: list[ToolSpec] = [
    ToolSpec("read_file", "Read a text file from the repository. Optionally limit to a line range (1-indexed, inclusive).",
             _obj({"path": {"type": "string", "description": "Path relative to the repository root"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, ["path"]), _read_file, api_only=True),
    ToolSpec("write_file", "Create or overwrite a file with the given content. Parent directories are created. Prefer edit_file for small changes to existing files.",
             _obj({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]), _write_file, api_only=True),
    ToolSpec("edit_file", "Replace an exact string in a file. old_string must match exactly once unless replace_all is true.",
             _obj({"path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}, "replace_all": {"type": "boolean"}}, ["path", "old_string", "new_string"]), _edit_file, api_only=True),
    ToolSpec("list_files", "List files and directories in the repository (ignores node_modules, .git, build output).",
             _obj({"path": {"type": "string", "description": "Directory to list, default repository root"}, "depth": {"type": "integer", "description": "Recursion depth, default 3"}}), _list_files, api_only=True),
    ToolSpec("search_files", "Search file contents with a regular expression (grep -E). Returns matching lines with file and line number.",
             _obj({"pattern": {"type": "string"}, "path": {"type": "string"}, "max_results": {"type": "integer"}}, ["pattern"]), _search_files, api_only=True),
    ToolSpec("run_command", "Run a shell command in the repository root (install dependencies, run tests, build, git log, etc). Non-interactive; output is captured. Default timeout 120s, max 600s. Never start long-lived servers without a timeout.",
             _obj({"command": {"type": "string"}, "timeout_sec": {"type": "integer"}}, ["command"]), _run_command, api_only=True),
    ToolSpec("git_status", "Show uncommitted changes, the recent commit log, and the files you have touched since your last commit.", _obj({}), _git_status),
    ToolSpec("git_commit",
             "Commit your work. Stages `files` if given, otherwise the files you wrote/edited since your last commit (or all changes if none were tracked). Posts a short summary on the board (as a reply on `thread_id` when the work belongs to an existing discussion, otherwise as a new thread) and returns any new comments addressed to you: read them and decide whether to reply or act before continuing.",
             _obj({"message": {"type": "string", "description": "Conventional commit message, e.g. 'feat(api): add user endpoints'"},
                   "summary_title": {"type": "string", "description": "Short, plain title (used only when starting a new thread)"},
                   "summary_body": {"type": "string", "description": "2-6 lines, written like a teammate's update: what landed, how you checked it, what is next or what you need. No headings."},
                   "thread_id": {"type": "integer", "description": "Thread to post the summary in. Defaults to the thread you are working in this cycle (the one you were tagged in, or last read / replied to); pass 0 to force a new thread."},
                   "files": {"type": "array", "items": {"type": "string"}, "description": "Explicit paths to stage (optional)"}},
                  ["message", "summary_title", "summary_body"]), _git_commit),
    ToolSpec("list_threads", "List recent board threads (newest activity first).", _obj({"limit": {"type": "integer"}}), _list_threads),
    ToolSpec("read_thread", "Read a board thread and all of its comments.", _obj({"thread_id": {"type": "integer"}}, ["thread_id"]), _read_thread),
    ToolSpec("post_thread", "Start a new board thread (proposal, design, bug report, review, question). @name tags someone and wakes them: use it only when you need them to act or answer; otherwise refer to people by name without @. @human only when the human must decide or provide something.",
             _obj({"title": {"type": "string"}, "body": {"type": "string"}}, ["title", "body"]), _post_thread),
    ToolSpec("post_comment", "Reply on a board thread. @name tags someone and wakes them: only when you need them to act or answer; otherwise write names without @. @human only when the human must decide or provide something.",
             _obj({"thread_id": {"type": "integer"}, "body": {"type": "string"}}, ["thread_id", "body"]), _post_comment),
    ToolSpec("check_inbox", "Fetch new mentions and replies addressed to you since you last checked.", _obj({}), _check_inbox),
    ToolSpec("update_notes", "Overwrite your persistent notes (your long-term memory). Include: what you own, key decisions, what is done, what is in progress, what is next, open questions, and useful file paths. This is all you will remember next cycle.",
             _obj({"notes": {"type": "string"}}, ["notes"]), _update_notes),
    ToolSpec("list_agents", "List the team: names, roles, briefs, and current status.", _obj({}), _list_agents),
    ToolSpec("finish_cycle", "End your current work cycle. Call this when your focused task or reaction is complete (after committing and updating notes).",
             _obj({"summary": {"type": "string", "description": "What you did this cycle, in one paragraph"},
                   "next_task": {"type": "string", "description": "What you will pick up next cycle"},
                   "wait_for_mention": {"type": "boolean", "description": "Set true when you have nothing useful to do until someone asks you (blocked, or your work is complete). You will then sleep until tagged on the board (leads still run scheduled reviews)."}},
                  ["summary", "next_task"]), _finish_cycle),
    ToolSpec("hire_agent", "Add a new agent to the team (after the human confirmed the staffing change on the board). It starts working immediately with the brief you give it.",
             _obj({"name": {"type": "string", "description": "Unique lowercase slug, e.g. backend-2"},
                   "role": {"type": "string", "enum": [r for r in ROLE_KEYS if r != "master"]},
                   "title": {"type": "string"},
                   "brief": {"type": "string", "description": "What this agent owns and its first task"},
                   "model": {"type": "string", "enum": MODEL_IDS, "description": "Model chosen for the role and task difficulty (cost matters)"},
                   "effort": {"type": "string", "enum": EFFORTS},
                   "confirmation_thread_id": {"type": "integer", "description": "Thread where you proposed this hire to @human and they replied confirming"}},
                  ["name", "role", "title", "brief", "confirmation_thread_id"]), _hire_agent, master_only=True),
    ToolSpec("set_agent_model", "Change an agent's model and/or effort (after the human confirmed on the board), e.g. downgrade a role doing routine work to save cost, or upgrade one that is struggling. Takes effect on its next cycle.",
             _obj({"name": {"type": "string"}, "model": {"type": "string", "enum": MODEL_IDS}, "effort": {"type": "string", "enum": EFFORTS},
                   "confirmation_thread_id": {"type": "integer", "description": "Thread where you proposed this change to @human and they replied confirming"}}, ["name", "confirmation_thread_id"]), _set_agent_model, master_only=True),
    ToolSpec("set_goal", "Change the project goal and/or definition of done (after proposing it to @human on the board and getting their confirmation). Every agent sees the new goal from its next cycle.",
             _obj({"goal": {"type": "string"}, "definition_of_done": {"type": "string", "description": "One condition per line"},
                   "confirmation_thread_id": {"type": "integer", "description": "Thread where you proposed the change to @human and they replied confirming"}}, ["goal", "confirmation_thread_id"]), _set_goal, master_only=True),
    ToolSpec("resume_team", "After a token-limit pause, release the rest of the team so they resume their cycles (you are resumed first so you can check the board and set direction).",
             _obj({}), _resume_team, master_only=True),
    ToolSpec("retire_agent", "Retire an agent whose work is complete or no longer needed (after the human confirmed on the board). Its memory is kept.",
             _obj({"name": {"type": "string"}, "confirmation_thread_id": {"type": "integer", "description": "Thread where you proposed this to @human and they replied confirming"}}, ["name", "confirmation_thread_id"]), _retire_agent, master_only=True),
]


def validate(schema: dict[str, Any], data: Any) -> str | None:
    if not isinstance(data, dict):
        return "input is not an object"
    for key in schema.get("required", []):
        if key not in data:
            return f'missing required field "{key}"'
    props = schema.get("properties", {})
    for key, val in data.items():
        prop = props.get(key)
        if prop is None:
            return f'unexpected field "{key}"'
        if val is None:
            continue
        t = prop.get("type")
        ok = {
            "string": isinstance(val, str),
            "integer": isinstance(val, int) and not isinstance(val, bool),
            "number": isinstance(val, (int, float)) and not isinstance(val, bool),
            "boolean": isinstance(val, bool),
            "array": isinstance(val, list),
        }.get(t, True)
        if not ok:
            return f'field "{key}" must be {t}'
        if "enum" in prop and str(val) not in prop["enum"]:
            return f'field "{key}" must be one of {", ".join(prop["enum"])}'
    return None


MASTER_EXCLUDED = {"write_file", "edit_file", "git_commit"}  # the master leads; it does not build or commit


def available_tools(ctx: ToolContext, backend: str) -> list[ToolSpec]:
    is_master = ctx.agent.role == "master"
    return [t for t in TOOLS if (not t.master_only or is_master) and (backend in ("api", "deepseek", "ollama") or not t.api_only) and not (is_master and t.name in MASTER_EXCLUDED)]


async def execute(spec: ToolSpec, data: Any, ctx: ToolContext) -> tuple[str, bool]:
    """Runs a tool with validation. Returns (content, is_error)."""
    problem = validate(spec.input_schema, data)
    if problem:
        return f'{{"INVALID_JSON": "{problem}"}}', True
    ctx.cycle.tool_calls += 1
    try:
        content = await spec.run(data, ctx)
    except Exception as e:  # tool bugs must surface to the model, not crash the agent
        content = f"ERROR: {e}"
    ctx.memory.activity("result", f"{spec.name}: {content[:600]}")
    return content, content.startswith("ERROR:")
