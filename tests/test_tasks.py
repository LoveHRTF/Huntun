"""Durable delivery cards, DoD migration, and completion after integration."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from huntun.config import default_config
from huntun.gitops import GitError
from huntun.memory import AgentMemory
from huntun.store import Store
from huntun.tools import ToolContext, ToolHooks, available_tools, execute
from huntun.types import AgentSpec, CycleState


class TaskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.store = Store(self.path / 'board.db')
        self.addCleanup(self.store.close)
        self.team = [AgentSpec('master', 'master', 'Master', 'Lead'), AgentSpec('tl', 'team-lead', 'TL', 'Lead'),
                     AgentSpec('dev', 'backend', 'Dev', 'Build'), AgentSpec('qa', 'qa', 'QA', 'Test')]

    def ctx(self, name, hooks=None):
        return ToolContext(next(a for a in self.team if a.name == name), lambda: self.team, self.path, self.store,
                           AgentMemory(self.path / 'agents', name), default_config('Build'), CycleState(), hooks or ToolHooks())

    async def call(self, ctx, name, **args):
        return await execute(next(t for t in available_tools(ctx, 'api') if t.name == name), args, ctx)

    async def test_existing_dod_migrates_once_and_preserves_progress_on_reopen(self):
        self.store.seed_tasks('- Ship API\n2. Tests pass\n- Ship API')
        cards = self.store.list_tasks()
        self.assertEqual([t['title'] for t in cards], ['Ship API', 'Tests pass'])
        self.store.update_task(cards[0]['id'], 'master', {'owner':'dev', 'status':'in_process'})
        second = Store(self.path / 'board.db')
        self.addCleanup(second.close)
        second.seed_tasks('Ship API\nTests pass\nDocs updated')
        self.assertEqual(len(second.list_tasks()), 3)
        self.assertEqual(second.get_task(cards[0]['id'])['status'], 'in_process')

    async def test_master_and_tl_breakdown_assigns_real_thread_and_inbox(self):
        self.store.seed_tasks('Ship API')
        parent = self.store.list_tasks()[0]['id']
        for lead in ('master', 'tl'):
            result, error = await self.call(self.ctx(lead), 'create_task', title='API ' + lead,
                                           acceptance='GET /api returns 200', owner='dev', parent_id=parent)
            self.assertFalse(error, result)
            task = json.loads(result)
            self.assertEqual(task['status'], 'backlog')
            self.assertEqual(task['parent_id'], parent)
            self.assertIn('@dev', self.store.get_thread(task['thread_id'])['body'])
        self.assertEqual(len(self.store.take_inbox('dev')), 2)
        self.assertTrue((await self.call(self.ctx('dev'), 'create_task', title='Unowned', acceptance='Works'))[1])

    async def test_completion_waits_for_successful_merge_and_evidence(self):
        task = self.store.create_task('tl', 'API', acceptance='Tests pass', owner='dev')
        self.store.update_task(task['id'], 'dev', {'status':'in_process'})
        attempts = []
        async def merge():
            attempts.append(1)
            if len(attempts) == 1:
                raise GitError('conflict')
            return 'Merged abc123'
        ctx = self.ctx('dev', ToolHooks(complete_task=merge))
        self.assertTrue((await self.call(ctx, 'finish_cycle', summary='Built', next_task='', task_id=task['id']))[1])
        self.assertFalse(attempts)
        args = {'summary':'Built', 'next_task':'', 'task_id':task['id'], 'evidence':'pytest: 12 passed; abc123'}
        self.assertTrue((await self.call(ctx, 'finish_cycle', **args))[1])
        self.assertEqual(self.store.get_task(task['id'])['status'], 'in_process')
        self.assertFalse(ctx.cycle.finished)
        self.assertFalse((await self.call(ctx, 'finish_cycle', **args))[1])
        self.assertEqual(self.store.get_task(task['id'])['status'], 'done')

    async def test_owners_cannot_complete_or_reassign_other_cards(self):
        card = self.store.create_task('tl', 'API', acceptance='200', owner='dev')
        qa = self.ctx('qa')
        self.assertTrue((await self.call(qa, 'update_task', task_id=card['id'], status='in_process'))[1])
        dev = self.ctx('dev')
        self.assertTrue((await self.call(dev, 'update_task', task_id=card['id'], owner='qa'))[1])
        self.assertTrue((await self.call(dev, 'update_task', task_id=card['id'], status='done', evidence='passed'))[1])
        self.assertTrue((await self.call(self.ctx('tl'), 'update_task', task_id=card['id'], owner='missing'))[1])

    async def test_parent_requires_finished_subtasks_and_blocks_inconsistent_reopening(self):
        parent = self.store.create_task('master', 'Ship', acceptance='Release works')
        child = self.store.create_task('tl', 'Build', acceptance='Tests pass', owner='dev', parent_id=parent['id'])
        with self.assertRaisesRegex(ValueError, 'subtasks'):
            self.store.update_task(parent['id'], 'master', {'status':'done', 'evidence':'reviewed'})
        self.store.update_task(child['id'], 'master', {'status':'done', 'evidence':'abc123; tests passed'})
        self.store.update_task(parent['id'], 'master', {'status':'done', 'evidence':'reviewed abc123'})
        with self.assertRaisesRegex(ValueError, 'Reopen the parent'):
            self.store.update_task(child['id'], 'master', {'status':'backlog'})
        with self.assertRaisesRegex(ValueError, 'Reopen the parent'):
            self.store.create_task('tl', 'More work', acceptance='Works', parent_id=parent['id'])

    async def test_invalid_transitions_and_delivery_with_open_cards_fail(self):
        card = self.store.create_task('master', 'Build', acceptance='Tests pass')
        for changes in ({'status':'unknown'}, {'status':'in_process'}, {'status':'done'}, {'acceptance':''}, {'title':' '}, {'owner':[]}):
            with self.assertRaises(ValueError):
                self.store.update_task(card['id'], 'human', changes)
        reply, error = await self.call(self.ctx('master'), 'deliver_project', title='Release', report='Delivered')
        self.assertTrue(error)
        self.assertIn('unfinished', reply)
