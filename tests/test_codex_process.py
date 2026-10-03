"""A stopped native writer must not leave a detached tool host running."""
import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from huntun.backends.codex import CodexBackend
from huntun.config import default_config


@unittest.skipUnless(os.name=='posix','POSIX process groups')
class ProcessTests(unittest.TestCase):
    def exercise(self, cancel):
        async def run(root):
            marker=root/'children'
            host=root/'host.py'
            host.write_text('import signal,time\nsignal.signal(signal.SIGINT,signal.SIG_IGN)\ntime.sleep(60)\n')
            writer=root/'writer.py'
            writer.write_text('import subprocess,sys,time\n'
                              'p=subprocess.Popen([sys.executable,sys.argv[1]],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n'
                              'open(sys.argv[2],"w").write(str(p.pid))\n'
                              'time.sleep(60)\n')
            backend=CodexBackend(default_config('Test'))
            task=asyncio.create_task(backend._run([sys.executable,str(writer),str(host),str(marker)],'unused',root,lambda e:None,
                                                 should_stop=(lambda:marker.exists()) if not cancel else None))
            child=None
            try:
                for _ in range(100):
                    if marker.exists() and marker.read_text():
                        break
                    await asyncio.sleep(.02)
                self.assertTrue(marker.exists())
                child=int(marker.read_text())
                if cancel:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(task,5)
                else:
                    code,interrupted=await asyncio.wait_for(task,6)
                    self.assertTrue(interrupted)
                await asyncio.sleep(.05)
                status=subprocess.run(['ps','-o','stat=','-p',str(child)],text=True,capture_output=True).stdout.strip()
                self.assertTrue(not status or status.startswith('Z'),f'detached host {child} remains alive: {status}')
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task,return_exceptions=True)
                if child:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(child,signal.SIGKILL)
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(run(Path(tmp)))

    def test_pause_terminates_detached_hosts(self):
        self.exercise(False)

    def test_cancellation_terminates_detached_hosts(self):
        self.exercise(True)
