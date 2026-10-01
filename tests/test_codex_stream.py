"""Real subprocess regressions for oversized Codex JSONL events."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

from huntun.backends.codex import CodexBackend, _event_lines
from huntun.types import HuntunConfig


class CodexStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_stdout_and_stderr_are_drained_without_truncating_events(self):
        text = 'Watchdog 恢复🙂' * 50000
        event = {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / 'event.json'
            payload.write_text(json.dumps(event, ensure_ascii=False))
            script = "import sys; from pathlib import Path; sys.stderr.write('x'*200000+'TAIL'); sys.stderr.flush(); sys.stdout.buffer.write(Path(sys.argv[1]).read_bytes()+b'\\n'+b'{\"type\":\"turn.completed\"}'); sys.stdout.flush(); sys.exit(1)"
            events = []
            code, interrupted = await asyncio.wait_for(CodexBackend(HuntunConfig('test'))._run(
                [sys.executable, '-c', script, str(payload)], 'unused', root, events.append), 10)
        self.assertEqual((code, interrupted), (1, False))
        self.assertEqual(events[0], event)
        self.assertEqual(events[1]['type'], 'turn.completed')
        self.assertTrue(events[-1]['text'].endswith('TAIL'))
        self.assertLessEqual(len(events[-1]['text']), 2000)

    async def test_chunk_boundaries_blank_lines_and_final_unterminated_event(self):
        stream = asyncio.StreamReader()
        data = '恢复🙂'.encode()
        async def feed():
            for chunk in [b'\n', data[:1], data[1:5], data[5:] + b'\nnext\nlast']:
                stream.feed_data(chunk)
                await asyncio.sleep(0)
            stream.feed_eof()
        task = asyncio.create_task(feed())
        self.assertEqual([line async for line in _event_lines(stream)], [b'', data, b'next', b'last'])
        await task

    async def test_event_callback_failure_terminates_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def fail(event):
                raise RuntimeError('callback failed')
            script = "import time; print('{}',flush=True); time.sleep(60)"
            with self.assertRaisesRegex(RuntimeError, 'callback failed'):
                await asyncio.wait_for(CodexBackend(HuntunConfig('test'))._run(
                    [sys.executable, '-c', script], 'unused', root, fail), 5)
