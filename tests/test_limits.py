"""Usage-limit watchdog: an agent hits the provider limit -> everyone pauses; a probe succeeds -> master resumes first and releases the team."""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

import huntun.orchestrator as orch_module
from huntun.config import default_config, save_config, save_team
from huntun.gitops import ensure_repo
from huntun.master import master_spec
from huntun.orchestrator import Orchestrator
from huntun.types import AgentSpec, CycleResult


class LimitBackend:
    """First worker cycle hits the limit; probes fail twice, then succeed; every later cycle is a no-op."""

    name = "fake"

    def __init__(self, config: Any) -> None:
        self.config = config
        self.probes = 0
        self.cycles: list[str] = []
        self.resets_at: float | None = None

    async def run_cycle(self, **kw: Any) -> CycleResult:
        agent = kw["ctx"].agent.name
        self.cycles.append(agent)
        if agent == "dev-1" and self.cycles.count("dev-1") == 1:
            return CycleResult("limit", error="rate limited: five_hour limit reached", resets_at=self.resets_at)
        if agent == "master" and "resume_team" in kw["prompt"]:
            await kw["ctx"].hooks.resume_team()  # the master releases the team, as its prompt asks
        kw["ctx"].cycle.finished = True
        kw["ctx"].cycle.summary = "ok"
        return CycleResult("finished", "ok", "next", None, {"input": 1, "output": 1, "cost_usd": 0.001})

    async def probe(self) -> bool:
        self.probes += 1
        return self.probes >= 3

    async def structured(self, **kw: Any) -> dict[str, Any]:
        raise NotImplementedError


class LimitWatchdogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        config = default_config("Mock goal")
        config.backend, config.idle_interval_sec, config.lead_idle_interval_sec = "api", 1, 1
        save_config(self.ws, config)
        save_team(self.ws, [master_spec(), AgentSpec("team-lead", "team-lead", "Lead", "lead"), AgentSpec("dev-1", "backend", "Dev", "dev")])
        asyncio.run(ensure_repo(self.ws))
        self._real = orch_module.make_backend
        orch_module.make_backend = lambda name, config: LimitBackend(config)

    def tearDown(self) -> None:
        orch_module.make_backend = self._real
        self.tmp.cleanup()

    def test_limit_pauses_all_and_master_resumes_first(self) -> None:
        import time

        async def run() -> None:
            orch = Orchestrator(self.ws)
            orch.probe_interval_sec = 0.3
            orch.probe_lead_sec = 0.5
            backend: LimitBackend = orch.backend  # type: ignore[assignment]
            backend.resets_at = time.time() + 1.5  # the provider announced a reset: checks start just before it
            orch.store.set_control("plan_approved", "1")
            orch.store.set_running(True, "test")
            await orch.start()

            async def until(cond, timeout=30):
                for _ in range(int(timeout / 0.1)):
                    if cond():
                        return True
                    await asyncio.sleep(0.1)
                return False

            # dev-1's first cycle hits the limit: everything pauses, counters and timer are set
            self.assertTrue(await until(lambda: orch.limits()["paused"]), "watchdog paused the team")
            self.assertTrue(orch.store.is_running(), "a vendor limit pauses that vendor's agents, not the Start/Pause switch")
            lim = orch.limits()
            self.assertEqual((lim["pause_count"], lim["resume_count"]), (1, 0))
            self.assertIn("dev-1", lim["reason"])
            self.assertIn("api", lim["backends"])
            self.assertTrue(lim["since"])
            self.assertTrue(lim["auto"])
            self.assertTrue(await until(lambda: orch.store.agent_statuses()["dev-1"]["status"] == "paused (usage limit: api)"))
            self.assertEqual(backend.probes, 0, "no checks before the reset time is near")

            # probes fail, then succeed: the limit lifts, master is gated first
            self.assertTrue(await until(lambda: not orch.limits()["paused"], timeout=30), "watchdog lifted the limit")
            self.assertGreaterEqual(backend.probes, 3)
            self.assertEqual(orch.limits()["resume_count"], 1)
            master_cycles_before = orch.runtimes["master"].memory.state.cycles

            # the master runs its resume cycle (with the resume note) and releases the team; workers follow
            self.assertTrue(await until(lambda: orch.runtimes["master"].memory.state.cycles > master_cycles_before))
            self.assertTrue(await until(lambda: orch.limits()["gate"] is None))
            self.assertTrue(await until(lambda: backend.cycles.count("dev-1") >= 2), "dev-1 resumed after the master")
            kinds = [e["kind"] for e in orch.store.list_events()]
            self.assertIn("limit", kinds)
            self.assertIn("resume", kinds)
            await orch.stop()

        asyncio.run(run())


class ManualProbeTests(LimitWatchdogTests):
    def test_no_reset_time_means_no_polling_until_human_checks(self) -> None:
        async def run() -> None:
            orch = Orchestrator(self.ws)
            orch.probe_interval_sec = 0.3
            backend: LimitBackend = orch.backend  # type: ignore[assignment]
            backend.probes = 2  # next probe succeeds
            orch.store.set_control("plan_approved", "1")
            orch.store.set_running(True, "test")
            await orch.start()
            for _ in range(200):
                if orch.limits()["paused"]:
                    break
                await asyncio.sleep(0.1)
            self.assertTrue(orch.limits()["paused"])
            self.assertFalse(orch.limits()["auto"])
            await asyncio.sleep(1.5)
            self.assertEqual(backend.probes, 2, "watchdog does not poll when no reset time is known")
            self.assertIsNone(orch.limits()["next_probe_at"])
            res = await orch.probe_now()
            self.assertEqual((res["ok"], orch.limits()["paused"], orch.store.is_running()), (True, False, True))
            self.assertEqual(res["results"], {"api": True})
            self.assertTrue(orch.store.get_control("limit:api:last_probe", ""), "the check time is recorded (shown while the vendor is limited)")
            await orch.stop()

        asyncio.run(run())

    def test_limit_pauses_all_and_master_resumes_first(self) -> None:  # inherited case runs once, in the parent
        pass


class MixedVendorTests(unittest.TestCase):
    """Agents on different vendors: a limit on one vendor pauses only its agents; each agent runs on its own backend."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        config = default_config("Mock goal")
        config.backend, config.idle_interval_sec, config.lead_idle_interval_sec = "api", 1, 1
        save_config(self.ws, config)
        save_team(self.ws, [master_spec(), AgentSpec("team-lead", "team-lead", "Lead", "lead"),
                            AgentSpec("dev-1", "backend", "Dev", "dev", model="gpt-5.3-codex", backend="codex")])
        asyncio.run(ensure_repo(self.ws))
        self.made: list[str] = []
        self._real = orch_module.make_backend

        def make(name, config):
            self.made.append(name)
            b = LimitBackend(config)
            b.name = name
            return b

        orch_module.make_backend = make

    def tearDown(self) -> None:
        orch_module.make_backend = self._real
        self.tmp.cleanup()

    def test_limit_on_one_vendor_only_pauses_its_agents(self) -> None:
        async def run() -> None:
            orch = Orchestrator(self.ws)
            orch.store.set_control("plan_approved", "1")
            orch.store.set_running(True, "test")
            await orch.start()

            async def until(cond, timeout=30):
                for _ in range(int(timeout / 0.1)):
                    if cond():
                        return True
                    await asyncio.sleep(0.1)
                return False

            self.assertTrue(await until(lambda: "codex" in orch.limits()["backends"]), "dev-1's codex limit was recorded")
            self.assertEqual(sorted(set(self.made)), ["api", "codex"], "each vendor got its own backend")
            self.assertTrue(orch.store.is_running())
            self.assertTrue(await until(lambda: orch.store.agent_statuses()["dev-1"]["status"] == "paused (usage limit: codex)"))
            self.assertTrue(await until(lambda: orch.runtimes["team-lead"].memory.state.cycles >= 1), "the Claude-side lead keeps working")
            self.assertFalse(orch.backend_limited("api"))
            self.assertIsNone(orch.limits()["gate"], "master is not on the limited vendor, so no master-first gate")
            res = await orch.probe_now("codex")
            self.assertFalse(res["ok"])  # first probes fail
            orch.backends["codex"].probes = 5
            res = await orch.probe_now("codex")
            self.assertTrue(res["ok"])
            self.assertTrue(await until(lambda: orch.runtimes["dev-1"].memory.state.cycles >= 1), "dev-1 resumed on codex")
            await orch.stop()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
