"""Pi JSONL lifecycle plus optional real Pi/CLM integration against a local fixture provider."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from huntun import pi
from huntun.backends import make_backend
from huntun.backends.pi_clm import PiClmBackend
from huntun.config import (
    agents_dir,
    db_path,
    default_config,
    load_config,
    save_config,
    save_team,
)
from huntun.gitops import ensure_repo, ensure_worktree, merge_task
from huntun.memory import AgentMemory
from huntun.models import backend_for_model, model_info
from huntun.orchestrator import AgentRuntime, Orchestrator
from huntun.store import Store
from huntun.tools import ToolContext, ToolHooks, ToolSpec, available_tools
from huntun.types import AgentSpec, CycleResult, CycleState

FAKE = r"""#!__PY__
import asyncio,json,os,sys,time
from pathlib import Path
args=sys.argv[1:]
if '--version' in args:
 print('1.0.0');sys.exit(0)
prompt=sys.stdin.read()
if '--session-dir' in args:
 root=Path(args[args.index('--session-dir')+1]); root.mkdir(exist_ok=True)
 resumed='--session' in args
 file=Path(args[args.index('--session')+1]) if resumed else root/('timestamp_'+args[args.index('--session-id')+1]+'.jsonl')
 if resumed:
  assert file.exists() and ('New cycle' in prompt or 'paused mid-cycle' in prompt)
 assert 'SYS: current team' in Path(args[args.index('--append-system-prompt')+1]).read_text()
 with file.open('a') as f:f.write(json.dumps({'prompt':prompt})+'\n')
else: file=None
if not os.environ.get('FAKE_PI_NOT_READY'):
 print(json.dumps({'type':'huntun_ready','sessionFile':str(file) if file else None,'model':{'contextWindow':123456,'provider':os.environ.get('FAKE_PI_PROVIDER','fixture')}}),file=sys.stderr,flush=True)
def emit(value):print(json.dumps(value),flush=True)
if os.environ.get('FAKE_PI_PAUSE'):
 time.sleep(20);sys.exit(0)
if os.environ.get('FAKE_PI_LIMIT'):
 emit({'type':'message_end','message':{'role':'assistant','content':[],'stopReason':'error','errorMessage':'429 rate limit','usage':{}}});sys.exit(0)
if 'Reply with OK.' in prompt:
 emit({'type':'message_end','message':{'role':'assistant','content':[{'type':'text','text':'OK'}],'stopReason':'stop'}});sys.exit(0)
if 'Schema:' in prompt:
 emit({'type':'message_end','message':{'role':'assistant','content':[{'type':'text','text':'{"answer":"ok"}'}],'stopReason':'stop'}});sys.exit(0)
async def tools():
 from mcp import ClientSession
 from mcp.client.streamable_http import streamable_http_client
 async with streamable_http_client(os.environ['HUNTUN_PI_MCP_URL']) as streams:
  r,w=streams[:2]
  async with ClientSession(r,w) as client:
   await client.initialize()
   names={t.name for t in (await client.list_tools()).tools}
   assert 'post_comment' in names and 'extra_probe' in names
   for name,arguments in [('extra_probe',{}),('post_comment',{'thread_id':1,'body':'Pi reporting in.'}),('finish_cycle',{'summary':'done','next_task':'more','task_complete':False})]:
    emit({'type':'tool_execution_start','toolName':'mcp__huntun__'+name,'args':arguments})
    result=await client.call_tool(name,arguments)
    assert not getattr(result,'is_error',False),result
    emit({'type':'tool_execution_end','toolName':name,'result':{'content':[{'type':'text','text':'ok'}]}})
asyncio.run(tools())
emit({'type':'message_update','usage':{'input':9999},'assistantMessageEvent':{'type':'thinking_delta','delta':'Thinking'}})
emit({'type':'message_end','message':{'role':'assistant','stopReason':'stop','content':[{'type':'text','text':'a'*140000+'🚀'}],'usage':{'input':100,'output':20,'cacheRead':30,'cacheWrite':5,'cost':{'total':0.002}}}})
emit({'type':'message_end','message':{'role':'assistant','stopReason':'stop','content':[{'type':'text','text':'Done'}],'usage':{'input':200,'output':40,'cacheRead':50,'cacheWrite':10,'cost':{'total':0.003}}}})
if os.environ.get('FAKE_PI_COMPACT'):
 emit({'type':'compaction_start','reason':'threshold'})
 emit({'type':'compaction_end','result':{'estimatedTokensAfter':333,'usage':{'input':9,'output':5}}})
 entry={'type':'entry_appended','entry':{'customType':'live-context-state','data':{'lastOutcome':{'kind':'applied','at':'fixture-at','beforeEstimate':1000,'afterEstimate':500}}}}
 emit(entry);emit(entry)
"""


class PiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name) / "project"
        self.ws.mkdir()
        self.cli = Path(self.tmp.name) / "pi"
        self.cli.write_text(FAKE.replace("__PY__", sys.executable))
        self.cli.chmod(0o755)
        self.extension = Path(self.tmp.name) / "clm.ts"
        self.extension.write_text("export default function() {}")
        self.env = patch.dict(
            os.environ,
            {
                "HUNTUN_PI_BIN": str(self.cli),
                "HUNTUN_PI_CLM_EXTENSION": str(self.extension),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cfg = default_config("fixture")
        self.cfg.backend = "pi-clm"
        self.cfg.model = "pi-clm:default"
        self.team = [
            AgentSpec("master", "master", "Master", "lead"),
            AgentSpec("dev", "backend", "Dev", "implement"),
        ]
        save_config(self.ws, self.cfg)
        save_team(self.ws, self.team)
        asyncio.run(ensure_repo(self.ws))
        self.store = Store(db_path(self.ws))
        self.addCleanup(self.store.close)
        self.store.create_thread("master", "Task", "@dev do it")
        self.extra_called = False

    def ctx(self, memory=None):
        async def extra(data, ctx):
            self.extra_called = True
            return "extra ok"

        ctx = ToolContext(
            agent=self.team[1],
            team=lambda: self.team,
            workspace=self.ws,
            store=self.store,
            memory=memory or AgentMemory(agents_dir(self.ws), "dev"),
            config=self.cfg,
            cycle=CycleState(),
            tool_specs=None,
        )
        ctx.tool_specs = available_tools(ctx, "pi-clm") + [
            ToolSpec(
                "extra_probe", "fixture", {"type": "object", "properties": {}}, extra
            )
        ]
        return ctx

    def run_cycle(self, ctx, stop=lambda: False):
        return asyncio.run(
            PiClmBackend(self.cfg).run_cycle(
                ctx=ctx,
                system="SYS: current team",
                prompt="go",
                model=self.cfg.model,
                effort="high",
                should_stop=stop,
                log=lambda _: None,
            )
        )

    def test_codex_subscription_nominal_rates_are_untracked(self):
        with patch.dict(os.environ, {'FAKE_PI_PROVIDER':'openai-codex'}):
            result = self.run_cycle(self.ctx())
        self.assertEqual(result.outcome,'finished',result.error)
        self.assertEqual(result.cost_status,'untracked')
        self.assertEqual(result.usage['cost_usd'],0)
        self.assertEqual(result.usage['input'] + result.usage['cache_read'] + result.usage['cache_write'] + result.usage['output'],455)

    def test_mcp_usage_large_events_and_native_session_resume(self):
        ctx = self.ctx()
        result = self.run_cycle(ctx)
        self.assertEqual(result.outcome, "finished", result.error)
        self.assertEqual(result.cost_status, 'reported')
        self.assertTrue(self.extra_called)
        self.assertEqual(result.summary, "done")
        self.assertEqual(
            result.usage,
            {
                "input": 300,
                "output": 60,
                "cache_read": 80,
                "cache_write": 15,
                "cost_usd": 0.005,
                "turns": 2,
            },
        )
        self.assertEqual(ctx.memory.state.context_tokens, 300)
        self.assertEqual(ctx.memory.state.context_limit, 123456)
        session = Path(ctx.memory.state.session_id)
        self.assertTrue(session.exists())
        self.assertEqual(self.store.get_comments(1)[0]["body"], "Pi reporting in.")
        kinds = {r["kind"] for r in ctx.memory.read_activity()[0]}
        self.assertTrue({"thinking", "tool", "result", "text"} <= kinds)
        resumed = self.ctx(ctx.memory)
        self.assertEqual(self.run_cycle(resumed).outcome, "finished")
        self.assertEqual(resumed.memory.state.session_id, str(session))
        self.assertEqual(len(session.read_text().splitlines()), 2)
        self.assertEqual(resumed.memory.state.session_cycles, 2)
        self.assertFalse((self.ws / ".pi/mcp.json").exists())

    def test_limits_pause_and_unready_extension(self):
        ctx = self.ctx()
        with patch.dict(os.environ, {"FAKE_PI_LIMIT": "1"}):
            result = self.run_cycle(ctx)
        self.assertEqual(result.outcome, "limit")
        self.assertTrue(ctx.memory.state.resume_pending)
        session = ctx.memory.state.session_id
        with patch.dict(os.environ, {"FAKE_PI_PAUSE": "1"}):
            result = self.run_cycle(self.ctx(ctx.memory), lambda: True)
        self.assertEqual(result.outcome, "paused")
        self.assertEqual(ctx.memory.state.session_id, session)
        with patch.dict(os.environ, {"FAKE_PI_NOT_READY": "1"}):
            result = self.run_cycle(self.ctx())
        self.assertEqual(result.outcome, "error")
        self.assertIn("did not initialize", result.error)

    def test_native_and_clm_compaction_update_office_and_count_once(self):
        ctx = self.ctx()
        with patch.dict(os.environ, {"FAKE_PI_COMPACT": "1"}):
            result = self.run_cycle(ctx)
        self.assertEqual(result.outcome, "finished", result.error)
        self.assertEqual(ctx.memory.state.compactions, 2)
        self.assertFalse(ctx.memory.state.compacting)
        self.assertEqual((result.usage["input"], result.usage["output"]), (309, 65))

    def test_rotation_and_foreign_sessions(self):
        ctx = self.ctx()
        ctx.memory.state.session_id = "a-codex-thread"
        self.run_cycle(ctx)
        first = ctx.memory.state.session_id
        self.cfg.session_max_cycles = 1
        self.run_cycle(self.ctx(ctx.memory))
        self.assertNotEqual(ctx.memory.state.session_id, first)
        self.assertTrue(Path(first).is_file(), "rotated session is preserved")

    def test_structured_probe_factory_routing_and_install_error(self):
        backend = make_backend("pi-clm", self.cfg)
        value = asyncio.run(
            backend.structured(
                prompt="plan",
                tool_name="fixture",
                description="fixture",
                schema={"type": "object"},
                model="pi-clm:default",
                effort="ultra",
            )
        )
        self.assertEqual(value, {"answer": "ok"})
        self.assertTrue(asyncio.run(backend.probe()))
        self.assertEqual(
            backend._args("pi-clm:openai/gpt-fixture", "none")[-6:],
            ["--provider", "openai", "--model", "gpt-fixture", "--thinking", "off"],
        )
        self.assertEqual(
            backend_for_model("pi-clm:openai/gpt-fixture", {"codex": "ready"}), "pi-clm"
        )
        with patch.dict(os.environ, {"HUNTUN_PI_CLM_EXTENSION": "/missing/clm.ts"}):
            with self.assertRaisesRegex(
                RuntimeError, "pi install npm:@lolipopshock/pi-clm"
            ):
                PiClmBackend(self.cfg)

    def test_harness_change_archives_api_transcript_and_starts_fresh_session(self):
        observed = []

        class Backend:
            async def run_cycle(self, *, ctx, **kwargs):
                observed.append(
                    (
                        ctx.memory.state.session_id,
                        ctx.memory.load_transcript(),
                        ctx.memory.state.resume_pending,
                    )
                )
                return CycleResult("finished", "checked", "continue", None, {})

        tree = asyncio.run(ensure_worktree(self.ws, "dev"))
        with patch("huntun.orchestrator.make_backend", return_value=Backend()):
            orch = Orchestrator(self.ws)
            self.addCleanup(orch.store.close)
            runtime = AgentRuntime(orch, orch.team[1])
            runtime.memory.state.worktree_path = str(tree)
            runtime.memory.state.session_backend = "api"
            runtime.memory.state.session_id = "previous-session"
            runtime.memory.state.resume_pending = True
            runtime.memory.save_transcript(
                [{"role": "user", "content": "unfinished context"}]
            )
            self.assertEqual(asyncio.run(runtime._run_one("resume")), "finished")
        self.assertEqual(observed, [(None, None, False)])
        archives = list(runtime.memory.dir.glob("transcript-*.json"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(
            json.loads(archives[0].read_text())[0]["content"], "unfinished context"
        )
        self.assertEqual(runtime.memory.state.session_backend, "pi-clm")
        self.assertTrue(
            any(
                entry.get("previous_session_id") == "previous-session"
                for entry in runtime.memory.recent_journal()
            )
        )

    def test_existing_project_switch_persists_and_preserves_overrides(self):
        self.cfg.backend = "codex"
        self.cfg.model = ""
        save_config(self.ws, self.cfg)
        self.team[1].backend = "codex"
        self.team[1].model = "gpt-5.3-codex"
        save_team(self.ws, self.team)
        with patch("huntun.orchestrator.make_backend", return_value=object()):
            orch = Orchestrator(self.ws)
            self.addCleanup(orch.store.close)
            asyncio.run(orch.set_harness("pi-clm", "pi-clm:default", False))
            self.assertEqual(orch.team[1].backend, "codex")
            asyncio.run(orch.set_harness("pi-clm", "pi-clm:default", True))
            self.assertTrue(
                all(not a.backend and not a.model for a in orch.active_team())
            )
        config = load_config(self.ws)
        self.assertEqual((config.backend, config.model), ("pi-clm", "pi-clm:default"))


@unittest.skipUnless(
    pi.binary() and pi.clm_extension(), "install Pi + CLM to run native integration"
)
class NativePiTests(unittest.TestCase):
    def test_native_clm_tools_worktree_merge_resume_and_catalog(self):
        requests = []
        actions = [
            ("write", {"path": "hello.txt", "content": "Pi implemented this.\n"}),
            (
                "mcp__huntun__post_comment",
                {"thread_id": 1, "body": "Pi native tools are connected."},
            ),
            (
                "mcp__huntun__git_commit",
                {
                    "message": "feat: pi fixture",
                    "summary_title": "Pi fixture",
                    "summary_body": "Verified locally.",
                    "thread_id": 1,
                    "files": ["hello.txt"],
                },
            ),
            (
                "mcp__huntun__finish_cycle",
                {
                    "summary": "Pi integrated task",
                    "next_task": "wait",
                    "task_complete": True,
                },
            ),
        ]

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                requests.append(request)
                n = len(requests) - 1
                text = (
                    '{"answer":"native"}'
                    if "Schema:" in json.dumps(request["messages"])
                    else "Done."
                )
                delta = {"role": "assistant", "content": text}
                reason = "stop"
                if n < len(actions):
                    name, args = actions[n]
                    delta = {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": f"call_{n}",
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(args),
                                },
                            }
                        ],
                    }
                    reason = "tool_calls"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in [
                    {
                        "id": f"fixture_{n}",
                        "object": "chat.completion.chunk",
                        "model": "fixture",
                        "choices": [
                            {"index": 0, "delta": delta, "finish_reason": None}
                        ],
                    },
                    {
                        "id": f"fixture_{n}",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
                        "usage": {
                            "prompt_tokens": 100 + n,
                            "completion_tokens": 10,
                            "total_tokens": 110 + n,
                        },
                    },
                ]:
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "pi-home"
            home.mkdir()
            ws = root / "project"
            ws.mkdir()
            (home / "models.json").write_text(
                json.dumps(
                    {
                        "providers": {
                            "fixture": {
                                "baseUrl": f"http://127.0.0.1:{server.server_port}/v1",
                                "api": "openai-completions",
                                "apiKey": "fixture",
                                "models": [
                                    {
                                        "id": "fixture",
                                        "name": "Fixture",
                                        "reasoning": False,
                                        "input": ["text"],
                                        "cost": {
                                            "input": 1,
                                            "output": 2,
                                            "cacheRead": 0,
                                            "cacheWrite": 0,
                                        },
                                        "contextWindow": 123456,
                                        "maxTokens": 4096,
                                    }
                                ],
                            }
                        }
                    }
                )
            )
            extension = pi.clm_extension()
            with patch.dict(
                os.environ,
                {
                    "PI_CODING_AGENT_DIR": str(home),
                    "HUNTUN_PI_CLM_EXTENSION": str(extension),
                    "HUNTUN_PI_MODELS": "fixture/fixture",
                    "PI_OFFLINE": "1",
                },
            ):
                cfg = default_config("native fixture")
                cfg.backend = "pi-clm"
                cfg.model = "pi-clm:fixture/fixture"
                save_config(ws, cfg)
                asyncio.run(ensure_repo(ws))
                tree = asyncio.run(ensure_worktree(ws, "dev"))
                store = Store(db_path(ws))
                self.addCleanup(store.close)
                store.create_thread("master", "Task", "@dev do it")
                worker = AgentSpec("dev", "backend", "Dev", "fixture")
                memory = AgentMemory(agents_dir(ws), "dev")
                ctx = ToolContext(
                    worker,
                    lambda: [worker],
                    tree,
                    store,
                    memory,
                    cfg,
                    CycleState(),
                    ToolHooks(complete_task=lambda: merge_task(ws, tree, "dev")),
                )
                backend = PiClmBackend(cfg)
                result = asyncio.run(
                    backend.run_cycle(
                        ctx=ctx,
                        system="SYS: use your private worktree and finish_cycle.",
                        prompt="Implement task.",
                        model=cfg.model,
                        effort="none",
                        should_stop=lambda: False,
                        log=lambda _: None,
                    )
                )
                self.assertEqual(result.outcome, "finished", result.error)
                self.assertTrue(
                    ctx.cycle.task_merged,
                    "finish_cycle must integrate private worktree commits",
                )
                self.assertEqual(
                    (ws / "hello.txt").read_text(), "Pi implemented this.\n"
                )
                self.assertEqual(memory.state.context_limit, 123456)
                self.assertEqual(result.usage["input"], 510)
                self.assertEqual(result.usage["output"], 50)
                self.assertTrue(result.usage["cost_usd"] > 0)
                names = {t["function"]["name"] for t in requests[0]["tools"]}
                self.assertTrue(
                    {"read", "write", "edit", "bash", "mcp__huntun__finish_cycle"}
                    <= names,
                    names,
                )
                self.assertIn("LIVE_CONTEXT", json.dumps(requests[0]["messages"]))
                session = Path(memory.state.session_id)
                before = session.stat().st_size
                ctx.cycle = CycleState()
                resumed = asyncio.run(
                    backend.run_cycle(
                        ctx=ctx,
                        system="SYS: updated team",
                        prompt="Review previous work.",
                        model=cfg.model,
                        effort="none",
                        should_stop=lambda: False,
                        log=lambda _: None,
                    )
                )
                self.assertEqual(resumed.outcome, "finished", resumed.error)
                self.assertEqual(memory.state.session_id, str(session))
                self.assertGreater(session.stat().st_size, before)
                self.assertIn("updated team", json.dumps(requests[-1]["messages"]))
                info = model_info(cfg.model)
                self.assertEqual(
                    (info.context, info.input_per_m, info.reasoning_levels),
                    (123456, 1, ("none",)),
                )
                self.assertEqual(backend_for_model(cfg.model), "pi-clm")
                value = asyncio.run(
                    backend.structured(
                        prompt="Plan task",
                        tool_name="fixture",
                        description="fixture",
                        schema={"type": "object"},
                        model=cfg.model,
                        effort="none",
                    )
                )
                self.assertEqual(value, {"answer": "native"})
                self.assertFalse((tree / ".pi/mcp.json").exists())
