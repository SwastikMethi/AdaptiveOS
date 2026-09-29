import unittest

from adaptive_os.cli import build_parser


class CliTest(unittest.TestCase):
    def test_expected_commands_exist(self):
        for command in ("preflight", "calibrate", "run", "report", "cleanup"):
            with self.subTest(command=command):
                self.assertEqual(build_parser().parse_args([command]).command, command)


if __name__ == "__main__":
    unittest.main()
