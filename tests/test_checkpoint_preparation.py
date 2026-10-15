import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.prepare_laya import prepare


class CheckpointPreparationTest(unittest.TestCase):
    def test_changed_cached_weights_are_not_blessed_as_a_new_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'weights').write_bytes(b'changed')
            manifest = {'revision': 'a' * 40, 'files': {
                'weights': hashlib.sha256(b'pinned').hexdigest()}}
            with patch('scripts.prepare_laya.subprocess.run') as download:
                with self.assertRaises(ValueError):
                    prepare(root, manifest)
            download.assert_not_called()
            self.assertFalse((root / 'manifest.json').exists())

    def test_bad_download_cannot_replace_pinned_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = {'revision': 'a' * 40, 'files': {
                'weights': hashlib.sha256(b'pinned').hexdigest()}}
            def download(command, **kwargs):
                Path(command[-1]).write_bytes(b'incorrect download')
            with patch('scripts.prepare_laya.subprocess.run', side_effect=download):
                with self.assertRaises(ValueError):
                    prepare(root, manifest)
            self.assertFalse((root / 'weights').exists())
            self.assertFalse((root / 'manifest.json').exists())
