"""The master agent's staffing decision: which roles, how many of each, and each agent's brief."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .backends import Backend
from .config import now_iso, slugify
from .models import (
    EFFORTS,
    MODEL_IDS,
    available_backends,
    backend_for_model,
    catalog_available,
    catalog_for,
    catalog_text,
    estimate_cost_usd,
)
from .personalities import PERSONALITY_IDS, personality_text
from .personalities import catalog_text as personality_catalog
from .roles import ROLE_CATALOG
from .types import ROLE_KEYS, AgentSpec, HuntunConfig

WORKER_ROLES = [r for r in ROLE_KEYS if r != "master"]

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rationale": {"type": "string", "description": "Why this team shape fits the goal (2-5 sentences)"},
        "agents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "unique lowercase slug, e.g. team-lead, backend-1, qa-1"},
                    "role": {"type": "string", "enum": WORKER_ROLES},
                    "title": {"type": "string"},
                    "brief": {"type": "string", "description": "What this agent owns, its first concrete task, and how it should coordinate"},
                    "model": {"type": "string", "enum": MODEL_IDS, "description": "Model for this agent, chosen for its role and task difficulty"},
                    "effort": {"type": "string", "enum": EFFORTS, "description": "Reasoning effort for this agent"},
                    "why": {"type": "string", "description": "One sentence: why this model and effort for this role"},
                    "personality": {"type": "string", "enum": PERSONALITY_IDS, "description": "Personality preset for this agent; make teammates distinct"},
                    "personality_note": {"type": "string", "description": "Optional one-sentence tweak to the preset for this agent (empty if none)"},
                    "estimated_cycles": {"type": "integer", "description": "How many work cycles this agent will likely need to finish its part of the project"},
                    "tokens_per_cycle": {"type": "integer", "description": "Typical tokens consumed per cycle for this agent (input + output + cached reads). A focused coding cycle is roughly 150000-400000; a review or QA cycle roughly 80000-200000."},
                },
                "required": ["name", "role", "title", "brief", "model", "effort", "why", "personality", "estimated_cycles", "tokens_per_cycle"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["rationale", "agents"],
    "additionalProperties": False,
}
PLAN_SCHEMA["properties"]["estimate_notes"] = {"type": "string", "description": "Two or three sentences on how you estimated the effort and what could make it larger"}
PLAN_SCHEMA["required"].append("estimate_notes")


def estimate_for(team: list[AgentSpec], notes: str = "", master_cycles: int = 8, master_tokens: int = 120_000) -> dict[str, Any]:
    """Tokens and dollars to finish the project, from each agent's estimated cycles and model prices."""
    per_agent = []
    total_tokens = total_cost = 0.0
    total_cycles = 0
    for a in team:
        if a.status == "retired":
            continue
        cycles = a.estimated_cycles or (master_cycles if a.role == "master" else 6)
        per_cycle = a.tokens_per_cycle or (master_tokens if a.role == "master" else 200_000)
        tokens = cycles * per_cycle
        cost = estimate_cost_usd(a.model, tokens)
        per_agent.append({"name": a.name, "model": a.model or "", "cycles": cycles, "tokens_per_cycle": per_cycle, "tokens": tokens, "cost_usd": cost})
        total_tokens += tokens
        total_cost += cost
        total_cycles += cycles
    return {"tokens": int(total_tokens), "cost_usd": round(total_cost, 2), "cycles": total_cycles, "notes": notes, "per_agent": per_agent}


MANIFESTS = ("README.md", "readme.md", "README.rst", "package.json", "pyproject.toml", "requirements.txt", "go.mod", "Cargo.toml",
             "pom.xml", "build.gradle", "Gemfile", "composer.json", "Makefile", "Dockerfile", "docker-compose.yml", "CLAUDE.md")
SKIP_DIRS = {"node_modules", ".git", ".huntun", ".venv", "venv", "__pycache__", "dist", "build", "target", ".next", ".idea", ".vscode"}


def describe_workspace(path: Path, max_entries: int = 80) -> str:
    """A compact description of an existing project directory for the planning prompt (empty for an empty dir)."""
    entries: list[str] = []

    def walk(d: Path, depth: int) -> None:
        if depth > 2 or len(entries) >= max_entries:
            return
        try:
            children = sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return
        for c in children:
            if c.name in SKIP_DIRS or (c.name.startswith(".") and c.is_dir()):
                continue
            if len(entries) >= max_entries:
                entries.append("...")
                return
            rel = c.relative_to(path).as_posix()
            entries.append(rel + "/" if c.is_dir() else rel)
            if c.is_dir():
                walk(c, depth + 1)

    walk(path, 0)
    if not entries:
        return ""
    parts = ["# Existing project", "This directory already contains a project. Plan the team around extending it, not starting over.",
             "Files (depth 2):", "\n".join(entries)]
    for name in MANIFESTS:
        f = path / name
        if f.is_file():
            try:
                head = "\n".join(f.read_text(errors="replace").splitlines()[:40])
            except OSError:
                continue
            parts.append(f"\n--- {name} (first lines) ---\n{head}")
    try:
        log = subprocess.run(["git", "log", "-n5", "--format=%h %an: %s"], cwd=path, capture_output=True, text=True, timeout=10)
        if log.returncode == 0 and log.stdout.strip():
            parts.append("\nRecent git history:\n" + log.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return "\n".join(parts)


GOAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "message": {"type": "string", "description": "What you say to the human, conversationally: how you understood the goal, what you are assuming, and what you need from them (2-6 sentences)"},
        "goal": {"type": "string", "description": "The goal restated precisely in one or two sentences, as you propose the team should pursue it"},
        "definition_of_done": {"type": "array", "items": {"type": "string"}, "description": "4-10 concrete, checkable conditions that mean the project is finished"},
        "assumptions": {"type": "array", "items": {"type": "string"}, "description": "Assumptions you are making that the human may want to correct"},
        "questions": {"type": "array", "items": {"type": "string"}, "description": "Questions whose answers would change the plan (empty if none)"},
    },
    "required": ["message", "goal", "definition_of_done", "assumptions", "questions"],
    "additionalProperties": False,
}


def goal_prompt(goal: str, context: str, existing: str, conversation: str = "") -> str:
    return f"""You are the Master Agent of Huntun, an autonomous software team. Before staffing anyone, confirm the goal with the human owner.

# Goal as the human wrote it
{goal}
{f"# Context from the human{chr(10)}{context}{chr(10)}" if context else ""}
{existing or ""}
{f"# Conversation so far{chr(10)}{conversation}{chr(10)}" if conversation else ""}
Restate the goal precisely, propose a definition of done (concrete, checkable conditions), list the assumptions you are making, and ask only the questions whose answers would change the team or the plan. Be brief and conversational; the human will confirm, edit, or reply.
{"Revise your proposal to take the human's reply into account." if conversation else ""}
Submit by calling the propose_goal tool."""


async def draft_goal(backend: Backend, config: HuntunConfig, existing: str, conversation: str = "") -> dict[str, Any]:
    draft = await backend.structured(
        prompt=goal_prompt(config.goal, config.context, existing, conversation),
        tool_name="propose_goal",
        description="Submit the restated goal, definition of done, assumptions, and questions for the human to confirm.",
        schema=GOAL_SCHEMA,
        model=config.model,
        effort=config.lead_effort,
    )
    dod = [str(x) for x in (draft.get("definition_of_done") or []) if str(x).strip()]
    return {
        "message": str(draft.get("message") or "").strip(),
        "goal": str(draft.get("goal") or config.goal).strip(),
        "definition_of_done": dod,
        "assumptions": [str(x) for x in (draft.get("assumptions") or []) if str(x).strip()],
        "questions": [str(x) for x in (draft.get("questions") or []) if str(x).strip()],
    }


def goal_thread_body(draft: dict[str, Any], revised: bool = False) -> str:
    lines = [f"@human {draft['message']}", "", "**Goal as I understand it**", draft["goal"], "", "**Definition of done**"]
    lines += [f"- {d}" for d in draft["definition_of_done"]] or ["- (none proposed)"]
    if draft["assumptions"]:
        lines += ["", "**Assumptions**"] + [f"- {a}" for a in draft["assumptions"]]
    if draft["questions"]:
        lines += ["", "**Questions for you**"] + [f"- {q}" for q in draft["questions"]]
    lines += ["", "Confirm on the board (you can edit the goal and the definition of done first), or reply here and I will revise." if not revised else "Updated per your reply. Confirm on the board, or reply again."]
    return "\n".join(lines)


def _vendor_note(config: HuntunConfig) -> str:
    avail = available_backends()
    vendors = {m.vendor for m, _ in catalog_available(avail)}
    if len(vendors) > 1:
        return ("Several vendors are logged in on this machine, so you may mix them within one team: put each agent on the model that fits its work best, "
                "and consider spreading agents across vendors so one vendor's usage limit does not stall everyone. Each vendor has its own usage limits and pricing.")
    if not vendors:
        return "(Only the project's default backend is available; choose the effort per role.)"
    return "(One vendor is available here; choose the model tier and effort per role.)"


def plan_prompt(config: HuntunConfig, extra_context: str = "") -> str:
    catalog = "\n".join(f"- {r}: {ROLE_CATALOG[r].title} — {ROLE_CATALOG[r].summary}" for r in WORKER_ROLES)
    extra = f"\n# Additional context\n{extra_context}\n" if extra_context else ""
    return f"""You are the Master Agent of Huntun, an autonomous software team. Staff the team for this project.

# Goal (confirmed with the human)
{config.goal}

# Definition of done (agreed with the human)
{config.definition_of_done or "(not specified)"}
{extra}
# Available roles
{catalog}

# Models and effort (be cost-effective without giving up quality)
Pick a model and effort for every agent from its role and the difficulty of its tasks. The team is billed per token, so use the strongest model only where judgment matters, and cheaper models for routine work.
{catalog_text(config.backend)}
{_vendor_note(config)}
Effort levels: low, medium, high, xhigh, max. Higher effort means more reasoning per step and more tokens. Guidance:
- team-lead: a frontier model at high or xhigh effort (architecture, reviews, integration).
- engineers on well-specified features: a strong model at medium or high effort; a frontier model only for the hardest, most cross-cutting component.
- qa, tech-writer, scrum-master, and other routine roles: a fast or strong model at low or medium effort.
- Research-heavy roles (ux-researcher, data-scientist): a strong model at medium effort.
State the reason for each choice in "why".

# Personality
Pick a personality preset for every agent so the team reads like real, different people, not job descriptions: mix in the "people" presets (quirks, humour, moods, interests) rather than only working styles, and never give two agents the same one. Add a one-sentence note if the preset needs a tweak (a hobby, a catchphrase, a pet peeve). Their board posts and their work will follow it. The human can change these before approving. Presets:
{personality_catalog()}

# Estimate
For each agent, estimate how many work cycles it needs to finish its part and the typical tokens per cycle, so the human can see the expected total tokens and cost before approving. Be honest and slightly conservative; explain the basis in estimate_notes.

# Rules
- Always include exactly one "team-lead". Include a "scrum-master" only for teams of 5 or more workers.
- Choose the number of agents per role from the goal's actual needs (a CLI tool may need 2-3 agents; a full product with ML needs more). Fewer, well-briefed agents beat many idle ones; you can hire more later.
- Every agent name must be unique and a lowercase slug (letters, digits, hyphens). Use numbered names for multiple agents in one role (backend-1, backend-2).
- Each brief must state what the agent owns, its first concrete task, and who it coordinates with. The team lead's brief must include writing the initial architecture / plan thread and assigning first tasks.
- Agents share one git repository and a discussion board; briefs should reflect that.

Submit the plan by calling the propose_team tool."""


async def plan_team(backend: Backend, config: HuntunConfig, extra_context: str = "") -> tuple[str, list[AgentSpec]]:
    plan = await backend.structured(
        prompt=plan_prompt(config, extra_context),
        tool_name="propose_team",
        description="Submit the team composition for this project.",
        schema=PLAN_SCHEMA,
        model=config.model,
        effort=config.lead_effort,
    )
    raw = plan.get("agents") or []
    if not isinstance(raw, list) or not raw:
        raise RuntimeError("Team plan contained no agents")
    seen = {"master", "human", "all"}
    agents: list[AgentSpec] = []
    now = now_iso()
    for a in raw:
        name = slugify(str(a.get("name") or a.get("role") or "agent"))
        while name in seen:
            name = f"{name}-{len(agents) + 1}"
        seen.add(name)
        role = a.get("role") if a.get("role") in ROLE_KEYS else "fullstack"
        avail = available_backends()
        allowed = {m.id for m, _ in catalog_available(avail)} or {m.id for m in catalog_for(config.backend)}
        model = a.get("model") if a.get("model") in allowed else (next(iter(allowed)) if config.backend == "codex" else None)
        backend = backend_for_model(model, avail, default="") if model else ""
        effort = a.get("effort") if a.get("effort") in EFFORTS else None
        why = str(a.get("why") or "").strip()
        brief = str(a.get("brief") or "") + (f"\n\nModel choice: {why}" if why else "")
        agents.append(AgentSpec(name=name, role=role, title=str(a.get("title") or ROLE_CATALOG[role].title), brief=brief, model=model, effort=effort, backend=backend, created_at=now,
                                personality=personality_text(str(a.get("personality") or ""), str(a.get("personality_note") or "")),
                                personality_preset=str(a.get("personality")) if a.get("personality") in PERSONALITY_IDS else "custom",
                                estimated_cycles=max(1, int(a.get("estimated_cycles") or 0)), tokens_per_cycle=max(10_000, int(a.get("tokens_per_cycle") or 0))))
    if not any(a.role == "team-lead" for a in agents):
        agents.insert(0, AgentSpec("team-lead", "team-lead", ROLE_CATALOG["team-lead"].title,
                                   "Own architecture and coordination. Write the initial plan thread, assign first tasks, review commits.", created_at=now))
    return (str(plan.get("rationale") or "") + ("\n\nEstimate notes: " + str(plan.get("estimate_notes")).strip() if plan.get("estimate_notes") else "")), agents


def plan_thread_body(config: HuntunConfig, rationale: str, specs: list[AgentSpec], revised: bool) -> str:
    """The proposal the master posts for the human to approve before anything starts."""
    rows = []
    for a in specs:
        brief = a.brief.split("\n\nModel choice:")[0].strip()
        why = a.brief.split("\n\nModel choice:")[1].strip() if "\n\nModel choice:" in a.brief else ""
        model = a.model or ("(backend default)" if a.role == "master" else "(default)")
        via = f" via {a.backend}" if a.backend and a.backend != config.backend else ""
        rows.append(f"- **@{a.name}** — {a.title} · model `{model}`{via} · effort `{a.effort or 'default'}`\n  {brief}" + (f"\n  _Why: {why}_" if why else "") +
                    (f"\n  _Personality: {a.personality}_" if a.personality else "") +
                    (f"\n  _Estimate: ~{a.estimated_cycles} cycles × ~{a.tokens_per_cycle // 1000}k tokens_" if a.estimated_cycles else ""))
    est = estimate_for(specs)
    head = "**Revised plan** after your feedback." if revised else "**Proposed plan** for this project."
    return (f"@human {head}\n\n**Goal**\n{config.goal}\n\n**Why this team**\n{rationale}\n\n**Team, models, and effort**\n" + "\n".join(rows) +
            f"\n\n**Estimate to finish**: about {est['cycles']} cycles, ~{est['tokens'] / 1e6:.1f}M tokens" + (f", roughly ${est['cost_usd']:,.0f} at list prices" if est['cost_usd'] else " (Codex pricing is not tracked)") + ". Removing or downgrading agents on the board updates it."
            "\n\nReview the team size, roles, models, and the estimate. Approve on the board (you can change any agent's model or effort, or remove an agent, before approving), "
            "or ask for changes and I will revise the plan. Nothing starts until you approve.")


def master_spec() -> AgentSpec:
    return AgentSpec(
        name="master", role="master", title=ROLE_CATALOG["master"].title,
        brief="Own the outcome. Staff and steer the team, review progress periodically, raise the bar, and answer the human.",
        estimated_cycles=8, tokens_per_cycle=120_000,
        created_at=now_iso(),
    )
