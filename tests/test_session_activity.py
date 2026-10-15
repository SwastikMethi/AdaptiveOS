"""Session-lifetime App Nap protection; Docker and OS activity are external boundaries."""

from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from adaptive_os.desktop_worker import SessionWorker
from adaptive_os.session_store import SessionStore


class Activity:
    def __init__(self):
        self.active = False
        self.begins = self.ends = 0

    def begin(self):
        self.active = True
        self.begins += 1

    def end(self):
        if self.active:
            self.active = False
            self.ends += 1


class Runtime:
    def __init__(self, activity, start_error=False, cleanup_error=False):
        self.activity = activity
        self.start_error, self.cleanup_error = start_error, cleanup_error
        self.cancel = threading.Event()
        self.process = SimpleNamespace(poll=lambda: None)
        self.items = []
        self.protected_at_start = self.protected_at_cleanup = False

    def prepare(self, progress):
        pass

    def start(self, session_id):
        self.protected_at_start = self.activity.active
        if self.start_error:
            raise RuntimeError('Docker start failed')
        self.ready = {'kind': 'ready', 'sequence': 0, 'mono_ns': 1,
                      'receipt_ns': time.monotonic_ns()}

    def send(self, command):
        pass

    def command(self, op):
        if op == 'shutdown':
            self.items.append(({'kind': 'closed', 'sequence': 1, 'mono_ns': 2}, time.monotonic_ns()))

    def poll_message(self, timeout=0):
        return self.items.pop(0) if self.items else None

    def cleanup_session(self, session_id):
        self.protected_at_cleanup = self.activity.active
        if self.cleanup_error:
            raise RuntimeError('Docker cleanup failed')

    def close_transport(self):
        pass


class SessionActivityTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.activity = Activity()
        self.runtime = Runtime(self.activity)
        self.worker = SessionWorker(Path(self.directory.name) / 'sessions.sqlite3',
                                    runtime_factory=lambda: self.runtime)
        self.worker.activity = self.activity

    def open_store(self):
        self.worker.store = SessionStore(self.worker.database)
        self.addCleanup(self.worker.store.close)

    def test_activity_spans_start_through_cleanup_and_is_reacquired_per_session(self):
        # Removing begin, releasing before cleanup, or leaking between sessions breaks this.
        self.open_store()
        for _ in range(2):
            self.worker.handle({'op': 'start_session', 'workloads': []})
            self.assertTrue(self.runtime.protected_at_start)
            self.assertTrue(self.activity.active)
            self.worker.stop_session('completed')
            self.assertTrue(self.runtime.protected_at_cleanup)
            self.assertFalse(self.activity.active)
        self.assertEqual((self.activity.begins, self.activity.ends), (2, 2))

    def test_failed_start_releases_activity(self):
        # An exception before active=True must not leak the acquired token.
        self.open_store()
        self.runtime.start_error = True
        with self.assertRaisesRegex(RuntimeError, 'Docker start failed'):
            self.worker.handle({'op': 'start_session', 'workloads': []})
        self.assertTrue(self.runtime.protected_at_start)
        self.assertFalse(self.activity.active)
        self.assertEqual(self.activity.ends, 1)
        self.assertEqual(self.worker.store.session(self.worker.session_id)['status'], 'failed')

    def test_disconnect_releases_activity_even_if_cleanup_fails(self):
        # The fail path must release protection even when Docker cleanup reports an error.
        self.open_store()
        self.worker.handle({'op': 'start_session', 'workloads': []})
        self.runtime.cleanup_error = True
        self.worker.fail(RuntimeError('transport disconnected'))
        self.assertTrue(self.runtime.protected_at_cleanup)
        self.assertFalse(self.activity.active)
        self.assertEqual(self.activity.ends, 1)
        self.assertFalse(self.worker.active)
        self.assertIn('transport disconnected', self.worker.error)

    def test_quit_releases_active_session_token(self):
        # Thread shutdown must not leave the process protected after its sandbox ends.
        self.worker.submit({'op': 'start_session', 'workloads': []})
        self.worker.submit({'op': 'quit'})
        self.worker.start()
        self.worker.join(timeout=5)
        self.assertFalse(self.worker.is_alive())
        self.assertIsNone(self.worker.error)
        self.assertTrue(self.runtime.protected_at_start)
        self.assertTrue(self.runtime.protected_at_cleanup)
        self.assertFalse(self.activity.active)
        self.assertEqual((self.activity.begins, self.activity.ends), (1, 1))


class NativeActivityTest(unittest.TestCase):
    def activity(self):
        # Fake only Objective-C calls; verify the real guard's ownership/options contract.
        from adaptive_os.macos_activity import SessionActivity
        activity = SessionActivity()
        events = []
        objc = SimpleNamespace(objc_getClass=lambda name: name,
                               sel_registerName=lambda name: name)
        activity._objc = objc

        def send_id(receiver, selector):
            events.append((selector, receiver))
            return {b'new': 10, b'processInfo': 20, b'retain': receiver}[selector]

        def begin(receiver, selector, options, reason):
            events.append((selector, receiver, options, reason))
            return 30

        activity._id = send_id
        activity._string = lambda receiver, selector, text: text
        activity._begin = begin
        activity._end = lambda receiver, selector, token: events.append((selector, token))
        activity._void = lambda receiver, selector: events.append((selector, receiver))
        return activity, events

    def test_native_token_is_retained_once_and_ended_before_release(self):
        # Wrong sleep flags, duplicate acquisitions, or dropping an autoreleased token break this.
        activity, events = self.activity()
        with patch('adaptive_os.macos_activity.sys.platform', 'darwin'):
            activity.begin()
            activity.begin()
            self.assertTrue(activity.active)
            activity.end()
            activity.end()
        self.assertFalse(activity.active)
        self.assertEqual([e[0] for e in events], [b'new', b'processInfo',
            b'beginActivityWithOptions:reason:', b'retain', b'drain',
            b'new', b'endActivity:', b'release', b'drain'])
        begin = events[2]
        self.assertEqual(begin[1:3], (20, 0x00EFFFFF))
        self.assertTrue(begin[3])
        self.assertEqual(events[6:8], [(b'endActivity:', 30), (b'release', 30)])

    def test_other_platforms_do_not_acquire_an_activity(self):
        # Linux/Windows must not call Cocoa or claim protection.
        activity, events = self.activity()
        with patch('adaptive_os.macos_activity.sys.platform', 'linux'):
            activity.begin()
            activity.end()
        self.assertFalse(activity.active)
        self.assertEqual(events, [])

    def test_nil_native_token_fails_closed_and_drains_pool(self):
        # A failed native acquisition must not silently start an unprotected sandbox.
        activity, events = self.activity()
        activity._begin = lambda *args: None
        with patch('adaptive_os.macos_activity.sys.platform', 'darwin'):
            with self.assertRaisesRegex(RuntimeError, 'App Nap'):
                activity.begin()
        self.assertFalse(activity.active)
        self.assertEqual(events[-1], (b'drain', 10))


if __name__ == '__main__':
    unittest.main()
