"""Recovery remains reachable while paused and preserves history across model changes."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from huntun.config import db_path, save_config, save_team
from huntun.gitops import ensure_repo
from huntun.hub import Hub
from huntun.models import model_info
from huntun.store import Store
from huntun.tools import available_tools, execute
from huntun.types import AgentSpec, CycleResult, HuntunConfig


class FakeBackend:
    contexts = []
    prompts = []
    calls = []

    async def run_cycle(self, **kw):
        ctx = kw['ctx']
        if ctx.agent.role == 'watchdog':
            self.contexts.append(ctx)
            self.prompts.append(kw['prompt'])
            self.calls.append(kw['model'])
            tool = next(t for t in available_tools(ctx, 'api') if t.name == 'watchdog_status')
            status, error = await execute(tool, {}, ctx)
            assert not error and 'agents' in status
            ctx.cycle.finished = True
            return CycleResult('finished', 'Status checked.')
        ctx.cycle.finished = True
        ctx.cycle.wait_for_mention = True
        return CycleResult('finished', 'Recovery done.')


class WatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        (self.root / 'README.md').write_text('project')
        save_config(self.root, HuntunConfig('Build', backend='api', max_cycles_per_agent=1))
        save_team(self.root, [AgentSpec('master', 'master', 'Master', 'Lead'), AgentSpec('dev', 'fullstack', 'Dev', 'Build')])
        store = Store(db_path(self.root))
        store.set_control('plan_approved', '1')
        store.close()
        await ensure_repo(self.root)
        self.hub = Hub(Path(self.tmp.name) / 'home')
        self.hub.loop = asyncio.get_running_loop()
        self.entry = self.hub.add(str(self.root))
        self.fake = FakeBackend()
        self.fake.contexts, self.fake.prompts, self.fake.calls = [], [], []
        self.patches = [patch('huntun.watchdog.make_backend', return_value=self.fake),
                        patch('huntun.orchestrator.make_backend', return_value=self.fake),
                        patch('huntun.models.backend_for_model', return_value='api'),
                        patch('huntun.orchestrator.backend_for_model', return_value='api'),
                        patch('huntun.watchdog.catalog_available', return_value=[(model_info('claude-sonnet-5'), 'api'), (model_info('claude-opus-5'), 'api')])]
        for p in self.patches:
            p.start()
        self.agent = await self.hub.watchdog(self.entry.id)

    async def asyncTearDown(self):
        await self.hub.shutdown()
        if self.entry.store:
            self.entry.store.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    async def ask(self, model='claude-sonnet-5', target='master', message='Check status'):
        self.agent.send({'message': message, 'model': model, 'effort': 'high', 'target': target})
        await self.agent.task

    async def test_switching_models_and_targets_preserves_durable_conversation(self):
        await self.ask(message='First question')
        await self.ask('claude-opus-5', 'dev', 'Second question')
        self.assertEqual([m['body'] for m in self.agent.view()['messages']], ['First question', 'Status checked.', 'Second question', 'Status checked.'])
        self.assertIn('First question', self.fake.prompts[-1])
        self.assertIn('Selected teammate: dev', self.fake.prompts[-1])
        self.assertEqual(self.fake.calls, ['claude-sonnet-5', 'claude-opus-5'])
        self.assertFalse(self.agent.store.is_running())
        self.assertIn('worktrees/local-watchdog', str(self.fake.contexts[-1].workspace))
        names = {t.name for t in available_tools(self.fake.contexts[-1], 'codex')}
        self.assertNotIn('shell', names)
        self.assertNotIn('write_file', names)
        self.assertIn('recover_agent', names)
        await self.hub.close(self.entry.id)
        self.agent = await self.hub.watchdog(self.entry.id)
        self.assertEqual(len(self.agent.view()['messages']), 4)
        self.assertEqual(self.agent.view()['selection']['target'], 'dev')

    async def test_validation_does_not_store_invalid_requests(self):
        for override in ({'model': 'invalid'}, {'target': 'missing'}, {'effort': 'invalid'}, {'message': ''}):
            body = {'message': 'Status', 'model': 'claude-sonnet-5', 'target': 'master', 'effort': 'high', **override}
            with self.assertRaises(ValueError):
                self.agent.send(body)
        self.assertEqual(self.agent.view()['messages'], [])

    async def test_wake_master_bypasses_pause_waiting_and_cycle_cap_once(self):
        await self.hub.load(self.entry.id)
        orch = self.entry.orchestrator
        rt = orch.runtimes['master']
        rt.memory.state.cycles = 10
        rt.memory.state.waiting = True
        result = await self.agent.recover({'action': 'wake'}, 'master')
        self.assertIn('one cycle', result)
        for _ in range(100):
            if rt.memory.state.cycles > 10:
                break
            await asyncio.sleep(.02)
        self.assertEqual(rt.memory.state.cycles, 11)
        self.assertFalse(orch.running())
        self.assertEqual(orch.runtimes['dev'].memory.state.cycles, 0)
        self.assertEqual(orch.store.get_control('recovery:master', ''), '')
        await asyncio.sleep(.1)
        self.assertEqual(rt.memory.state.cycles, 11)

    async def test_swap_master_archives_session_and_keeps_notes(self):
        await self.hub.load(self.entry.id)
        rt = self.entry.orchestrator.runtimes['master']
        rt.memory.state.session_id = 'old-session'
        rt.memory.save_notes('Keep this context')
        rt.memory.save_transcript([{'role': 'user', 'content': 'old task'}])
        result = await self.agent.recover({'action': 'swap_master', 'model': 'claude-opus-5'}, 'dev')
        self.assertIn('New master model', result)
        self.assertEqual(rt.current().model, 'claude-opus-5')
        self.assertIsNone(rt.memory.state.session_id)
        self.assertEqual(rt.memory.notes(), 'Keep this context')
        self.assertTrue(list(rt.memory.dir.glob('transcript-*.json')))
        self.assertEqual(rt.memory.recent_journal()[-1]['previous_session_id'], 'old-session')

    async def test_resume_preserves_session_and_busy_requests_are_rejected(self):
        await self.hub.load(self.entry.id)
        orch = self.entry.orchestrator
        rt = orch.runtimes['master']
        rt.memory.state.session_id = 'saved-session'
        rt.memory.save_transcript([{'role': 'user', 'content': 'unfinished task'}])
        await self.agent.recover({'action': 'resume_session'}, 'master')
        self.assertEqual(rt.memory.state.session_id, 'saved-session')
        self.assertTrue(rt.memory.state.resume_pending)
        self.assertEqual(rt.memory.load_transcript()[0]['content'], 'unfinished task')
        body = {'message': 'Check status', 'model': 'claude-sonnet-5', 'effort': 'high', 'target': 'master'}
        self.agent.send(body)
        with self.assertRaisesRegex(ValueError, 'still handling'):
            self.agent.send(body)
        await self.agent.task
        self.assertEqual(len([m for m in self.agent.view()['messages'] if m['role'] == 'human']), 1)

    async def test_usage_limits_still_block_recovery(self):
        await self.hub.load(self.entry.id)
        orch = self.entry.orchestrator
        orch.store.set_control('limit:api:paused', '1')
        await self.agent.recover({'action': 'wake'}, 'master')
        await asyncio.sleep(.1)
        self.assertEqual(orch.runtimes['master'].memory.state.cycles, 0)
        self.assertEqual(orch.store.get_control('recovery:master', ''), 'wake')
        orch.store.set_running(False)
        self.assertEqual(orch.store.get_control('recovery:master', ''), '')

    async def test_earlier_history_is_paged_and_survives_restart(self):
        for i in range(150):
            self.agent.store.watchdog_message('human', str(i), 'master', 'claude-sonnet-5', 'high')
        page = self.agent.view()['messages']
        self.assertEqual(len(page), 100)
        earlier = self.agent.view(page[0]['id'])['messages']
        self.assertEqual(len(earlier), 50)
        self.agent.store.set_control('watchdog_busy', '1')
        await self.agent.close()
        self.hub.recovery_agents.pop(self.entry.id)
        self.agent = await self.hub.watchdog(self.entry.id)
        self.assertFalse(self.agent.view()['busy'])
        self.assertIn('interrupted', self.agent.view()['messages'][-1]['body'])
