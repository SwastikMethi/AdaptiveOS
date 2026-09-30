"""Finder-like PATH and bundled build-source contracts (no GUI dependency)."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from adaptive_os.sandbox_runtime import build_context, CONTEXT_FILES, docker_executable


class PackagingTest(unittest.TestCase):
    def test_docker_desktop_can_be_found_without_shell_path(self):
        official = '/Applications/Docker.app/Contents/Resources/bin/docker'
        with patch('shutil.which', return_value=None), patch('os.path.isfile', side_effect=lambda p: p == official), patch('os.access', return_value=True):
            self.assertEqual(docker_executable(), official)

    def test_packaged_sources_are_used_instead_of_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / 'Test.app' / 'Contents'
            resources = bundle / 'Resources' / 'sandbox'
            resources.mkdir(parents=True)
            for name in CONTEXT_FILES:
                (resources / name).write_text('fixture')
            with patch('sys.executable', str(bundle / 'MacOS' / 'desktop_app')):
                self.assertEqual(build_context(), resources)
                (resources / 'agent.py').unlink()
                with self.assertRaisesRegex(Exception, 'sources are missing'):
                    build_context()


if __name__ == '__main__':
    unittest.main()
