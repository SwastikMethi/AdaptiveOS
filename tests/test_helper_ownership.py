"""Exercise the real helper entry point with a blocking, credential-free provider."""
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest


class HelperOwnershipTest(unittest.TestCase):
    def program(self, marker):
        return ("import time\nfrom pathlib import Path\nfrom adaptive_os import model_helper\n"
                "class Blocking:\n"
                " def __init__(self, key): pass\n"
                " def predict(self, context):\n"
                f"  Path({str(marker)!r}).write_text('blocked')\n"
                "  time.sleep(100)\n"
                "model_helper.JevProvider = Blocking\nmodel_helper.main()\n")

    def test_pipe_closure_stops_blocked_warmup(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'blocked'
            child = subprocess.Popen([sys.executable, '-u', '-c', self.program(marker)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                child.stdin.write(b'{"provider":"jev","warmup":{"test":true}}\n')
                child.stdin.flush()
                until = time.monotonic() + 3
                while not marker.exists() and time.monotonic() < until:
                    time.sleep(.01)
                self.assertTrue(marker.exists(), 'fake provider never entered warmup')
                child.stdin.close()
                try:
                    child.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    self.fail('helper retained blocked warmup after its private pipe closed')
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=3)
                child.stdout.close()
                if not child.stdin.closed:
                    child.stdin.close()

    def test_parent_death_stops_blocked_prediction(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'blocked'
            child_program = self.program(marker)
            parent_program = (
                'import subprocess,sys,time\nfrom pathlib import Path\n'
                f'p=subprocess.Popen([sys.executable,"-u","-c",{child_program!r}], stdin=subprocess.PIPE, stdout=subprocess.PIPE)\n'
                'p.stdin.write(b\'{"provider":"jev"}\\n\'); p.stdin.flush()\n'
                'p.stdout.readline()\n'
                'p.stdin.write(b\'{"request_id":"test","context":{}}\\n\'); p.stdin.flush()\n'
                f'while not Path({str(marker)!r}).exists(): time.sleep(.01)\n'
                'print(p.pid,flush=True)\ntime.sleep(100)\n')
            parent = subprocess.Popen([sys.executable, '-u', '-c', parent_program],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            child_pid = None
            try:
                readable, _, _ = select.select([parent.stdout], [], [], 4)
                self.assertTrue(readable, 'fake helper did not enter prediction')
                child_pid = int(parent.stdout.readline())
                parent.kill()
                parent.wait(timeout=3)
                until = time.monotonic() + 1.5
                alive = True
                while alive and time.monotonic() < until:
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        alive = False
                    time.sleep(.01)
                self.assertFalse(alive, 'helper survived its owner while prediction was blocked')
            finally:
                if parent.poll() is None:
                    parent.kill()
                parent.wait(timeout=3)
                parent.stdout.close()
                if child_pid:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
