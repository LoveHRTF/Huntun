"""Concurrent page/control requests must share one project orchestrator."""
import asyncio
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from huntun.hub import Hub
from huntun.store import Store


class LifecycleTests(unittest.TestCase):
    def test_parallel_load_and_close_do_not_spawn_competing_teams(self):
        async def exercise(root):
            hub=Hub(root/'home')
            project=root/'project'
            project.mkdir()
            entry=hub.add(str(project))
            entry.approved=lambda: True
            instances=[]
            entered,release=asyncio.Event(),asyncio.Event()

            class FakeOrchestrator:
                def __init__(self,path):
                    self.store=Store(path/'board.sqlite')
                    self.stopped=False
                    instances.append(self)
                async def start(self):
                    entered.set()
                    await release.wait()
                async def stop(self):
                    self.stopped=True
                    self.store.close()

            with patch('huntun.hub.is_initialized',return_value=True),patch('huntun.orchestrator.Orchestrator',FakeOrchestrator):
                tasks=[asyncio.create_task(hub.load(entry.id,running=True)) for _ in range(12)]
                await entered.wait()
                await asyncio.sleep(.02)
                self.assertEqual(len(instances),1,'load requests wait for the first startup')
                release.set()
                results=await asyncio.gather(*tasks)
                self.assertTrue(all(r is entry for r in results))
                self.assertIs(entry.orchestrator,instances[0])
                self.assertTrue(entry.orchestrator.store.is_running())
                self.assertFalse(instances[0].stopped)
                await hub.close(entry.id)
                self.assertTrue(instances[0].stopped)
                self.assertIsNone(entry.orchestrator)
                # Requests after a close share one replacement, too.
                await asyncio.gather(*(hub.load(entry.id,running=None) for _ in range(6)))
                self.assertEqual(len(instances),2)
                self.assertTrue(entry.orchestrator.store.is_running())
                await hub.shutdown()
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(exercise(Path(tmp)))

    def test_failed_partial_start_is_cleaned_up_before_retry(self):
        async def exercise(root, failure):
            hub=Hub(root/'home')
            project=root/'project'
            project.mkdir()
            entry=hub.add(str(project))
            entry.approved=lambda: True
            instances=[]
            class FakeOrchestrator:
                def __init__(self,path):
                    self.store=Store(path/'board.sqlite')
                    self.stopped=False
                    instances.append(self)
                async def start(self):
                    await asyncio.sleep(.01)
                    if len(instances)==1 and failure=='start':
                        raise RuntimeError('Partial startup failure')
                async def stop(self):
                    self.stopped=True
                    self.store.close()
            with patch('huntun.hub.is_initialized',return_value=True),patch('huntun.orchestrator.Orchestrator',FakeOrchestrator):
                with patch.object(hub,'_save_registry',side_effect=OSError('Registry write failed')) if failure=='registry' else contextlib.nullcontext():
                    await hub.load(entry.id)
                self.assertEqual(entry.state,'error')
                self.assertTrue(instances[0].stopped)
                self.assertIsNone(entry.orchestrator)
                await hub.load(entry.id)
                self.assertEqual(entry.state,'ready')
                self.assertEqual(len(instances),2)
                await hub.shutdown()
        for failure in ('start','registry'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as tmp:
                asyncio.run(exercise(Path(tmp),failure))
