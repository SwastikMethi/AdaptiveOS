"""Shared, local process ownership for start.sh and stop.sh (macOS/Linux)."""

import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / '.venv' / 'bin' / 'python'
DIRECTORY = ROOT / 'output' / 'desktop'
STATE = DIRECTORY / 'launcher.json'
LOG = DIRECTORY / 'app.log'


def process_identity(pid):
    if type(pid) is not int or pid <= 1:
        return None
    result = subprocess.run(
        ['/bin/ps', '-ww', '-p', str(pid), '-o', 'stat=', '-o', 'lstart=', '-o', 'uid=', '-o', 'command='],
        capture_output=True, text=True, timeout=5,
        env={**os.environ, 'LC_ALL': 'C', 'TZ': 'UTC'})
    if result.returncode and result.stderr.strip():
        raise RuntimeError(f'Cannot verify process ownership: {result.stderr.strip()}')
    fields = result.stdout.strip().split(None, 7)
    if len(fields) != 8 or fields[0].startswith('Z') or fields[6] != str(os.getuid()):
        return None
    # macOS framework Python can re-exec under a different executable path.
    _, marker, remainder = fields[7].partition(f' -m adaptive_os.desktop --data-dir {DIRECTORY}')
    if not marker or (remainder and not remainder.startswith(' ')):
        return None
    # Status and the framework executable can change; birth, owner and module arguments cannot.
    return ' '.join(fields[1:7]) + '\n' + marker + remainder


def owned_pid():
    if not STATE.exists():
        return None
    try:
        record = json.loads(STATE.read_text())
    except (ValueError, OSError) as exc:
        raise RuntimeError(f'Cannot read launcher state at {STATE}: {exc}') from exc
    pid = record.get('pid') if isinstance(record, dict) else None
    identity = process_identity(pid)
    if identity and identity == record.get('identity'):
        return pid
    STATE.unlink()
    print('Cleared stale launcher state.')
    return None


def start(arguments):
    pid = owned_pid()
    if pid:
        print(f'Already running (PID {pid}). Use ./stop.sh before restarting with new options.')
        return 0
    env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src'), 'PYTHONUNBUFFERED': '1'}
    command = [str(PYTHON), '-m', 'adaptive_os.desktop', '--data-dir', str(DIRECTORY), *arguments]
    if '--help' in arguments or '-h' in arguments:
        return subprocess.call(command, cwd=ROOT, env=env)
    with LOG.open('a') as log:
        log.write(f'\n--- Launch {time.strftime("%Y-%m-%d %H:%M:%S")} ---\n')
        log.flush()
        check = subprocess.run([str(PYTHON), '-c', 'import PySide6.QtWidgets'], cwd=ROOT, env=env,
                               stdout=log, stderr=subprocess.STDOUT, timeout=30)
        if check.returncode:
            raise RuntimeError("Desktop dependencies unavailable. Run .venv/bin/python -m pip install -e '.[desktop]' "
                               f'from the project folder. Details: {LOG}')
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            identity = process_identity(process.pid)
            if not identity:
                raise RuntimeError(f'App did not start. See {LOG}')
            temporary = STATE.with_suffix('.tmp')
            temporary.write_text(json.dumps({'pid': process.pid, 'identity': identity}) + '\n')
            temporary.replace(STATE)
        except Exception:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=30)
            raise
        deadline = time.monotonic() + 2
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(.1)
        if process.poll() is not None:
            STATE.unlink(missing_ok=True)
            raise RuntimeError(f'App exited during startup (code {process.returncode}). See {LOG}')
    print(f'Started Adaptive OS Controller (PID {process.pid}).\nLog: {LOG}\nStop: {ROOT / "stop.sh"}')
    return 0


def stop():
    pid = owned_pid()
    if not pid:
        print('Already stopped; no script-managed app is running.')
        return 0
    print(f'Stopping PID {pid}; saving recordings and cleaning up its sandbox…', flush=True)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if owned_pid() is None:
            print('App stopped. Recordings and logs were kept.')
            return 0
        time.sleep(.2)
    raise RuntimeError(f'Shutdown exceeded 60 seconds. The process was not force-killed. '
                       f'Launcher state was kept; check {LOG} and retry ./stop.sh.')


def main():
    try:
        action, *arguments = sys.argv[1:] or ['']
        if action not in {'start', 'stop'} or (action == 'stop' and arguments):
            raise RuntimeError('Usage: start.sh [desktop options] | stop.sh')
        DIRECTORY.mkdir(parents=True, exist_ok=True)
        with (DIRECTORY / 'launcher.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('Another start/stop operation is in progress; retry when it finishes.') from None
            return start(arguments) if action == 'start' else stop()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
