from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Effort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
BackendName = Literal["api", "claude-code", "codex", "kimi", "pi-clm", "deepseek", "ollama", "vllm", "llamacpp"]

ROLE_KEYS: tuple[str, ...] = (
    "master",
    "team-lead",
    "scrum-master",
    "backend",
    "frontend",
    "fullstack",
    "ux-researcher",
    "ui-designer",
    "qa",
    "ml-engineer",
    "data-scientist",
    "devops",
    "tech-writer",
    "security",
    "product-manager",
)


@dataclass
class AgentSpec:
    name: str
    role: str
    title: str
    brief: str
    status: str = "active"  # active | retired
    personality: str = ""
    personality_preset: str = ""  # preset id, or "custom"
    estimated_cycles: int = 0
    tokens_per_cycle: int = 0
    model: str | None = None
    effort: str | None = None
    backend: str = ""  # per-agent backend (vendor); empty = the project default
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentSpec":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class HuntunConfig:
    goal: str
    context: str = ""
    definition_of_done: str = ""
    goal_confirmed: bool = False
    # Master's estimate for completing the project: {"tokens", "cost_usd", "cycles", "notes", "per_agent": [...]}
    estimate: dict[str, Any] = field(default_factory=dict)
    # Maximum number of agents besides the master (0 = no limit).
    max_agents: int = 0
    # False: no team lead; the master leads the team technically as well (fewer agents, lower cost).
    team_lead: bool = True
    # Claude Code / Codex: how many cycles an agent keeps one continuous session before starting a fresh one (0 = never reset).
    session_max_cycles: int = 25
    backend: str = "auto"
    model: str = ""  # empty = backend default (claude-opus-5-5 for api, Claude Code's configured model for claude-code)
    # The master's own model, chosen by the human (empty = the project backend's default). It may run on another backend
    # than the project's: the goal check, the planning and the master's cycles all go to the backend that serves it.
    master_model: str = ""
    lead_effort: str = "xhigh"
    worker_effort: str = "high"
    review_interval_min: float = 20
    idle_interval_sec: float = 90
    lead_idle_interval_sec: float = 600
    max_tool_calls_per_cycle: int = 60
    max_tokens: int = 32000
    max_cycles_per_agent: int = 0
    port: int = 4747
    fallbacks: bool = True
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "HuntunConfig":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class AgentState:
    worktree_path: str = ""
    cycles: int = 0
    current_task: str = ""
    last_summary: str = ""
    touched_files: list[str] = field(default_factory=list)
    last_seen_comment_id: int = 0
    last_review_at: str | None = None
    review_count: int = 0
    last_cycle_at: str | None = None
    waiting: bool = False
    # Claude Code backend: session to resume after a pause / restart.
    session_id: str | None = None
    session_backend: str = ""  # prevents resuming a native session with a different harness
    resume_pending: bool = False
    # Cycles run on the current session (Claude Code / Codex keep one conversation across cycles).
    session_cycles: int = 0
    # Disjoint uncached input/output/cache_read/cache_write plus usage cost/turns.
    usage_totals: dict[str, float] = field(default_factory=dict)
    usage_cost_counts: dict[str, int] = field(default_factory=dict)
    usage_accounting_version: int = 0
    # Context window occupancy of the current / last model call.
    context_tokens: int = 0
    context_limit: int = 0
    # How many times the working context was compacted (server-side in Claude Code, client-side on the API backend).
    compactions: int = 0
    # True while a client-side compaction call is in flight (the office view sends the agent to the toilet).
    compacting: bool = False


@dataclass
class InboxItem:
    kind: str  # mention | reply
    thread_id: int
    thread_title: str
    comment_id: int | None
    author: str
    body: str
    created_at: str


@dataclass
class CycleState:
    tool_calls: int = 0
    finished: bool = False
    summary: str = ""
    next_task: str = ""
    commits: list[str] = field(default_factory=list)
    wait_for_mention: bool = False
    # Thread the agent is working in this cycle (the task it was tagged in, or the last one it read / replied to).
    active_thread: int | None = None
    task_complete: bool = True
    task_merged: bool = False


@dataclass
class CycleResult:
    outcome: str  # finished | paused | error | limit
    summary: str = ""
    next_task: str = ""
    error: str | None = None
    usage: dict[str, float] = field(default_factory=dict)
    # For outcome "limit": when the provider says the window resets (unix seconds), if known.
    resets_at: float | None = None
    cost_status: str = ""
