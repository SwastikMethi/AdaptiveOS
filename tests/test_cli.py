import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from adaptive_os.cli import build_parser, main


class CliTest(unittest.TestCase):
    def test_desktop_forwards_jev_env_file(self):
        desktop_main = Mock(return_value=0)
        with patch.dict('sys.modules', {'adaptive_os.desktop': SimpleNamespace(main=desktop_main)}):
            self.assertEqual(main(['desktop', '--data-dir', 'recordings', '--jev-env-file', '.env']), 0)
        desktop_main.assert_called_once_with(['--data-dir', 'recordings', '--jev-env-file', '.env'])

    def test_expected_commands_exist(self):
        for command in ("preflight", "calibrate", "run", "report", "cleanup"):
            with self.subTest(command=command):
                self.assertEqual(build_parser().parse_args([command]).command, command)

    def test_calibration_and_baseline_arguments(self):
        args = build_parser().parse_args(["calibrate", "--cpu", "auto", "--measurement-sec", "15"])
        self.assertEqual(args.measurement_sec, 15)
        args = build_parser().parse_args(["run", "--policy", "default", "--profile", "light",
                                         "--trials", "1", "--duration-sec", "20", "--smoke"])
        self.assertTrue(args.smoke)
        self.assertEqual(args.duration_sec, 20)

    def test_calibration_accepts_explicit_hardware_tuning_budgets(self):
        args = build_parser().parse_args(["calibrate", "--budgets-ms", "1", "2", "4", "8"])
        self.assertEqual(args.budgets_ms, [1, 2, 4, 8])


if __name__ == "__main__":
    unittest.main()
