import unittest
from unittest.mock import patch

from nodriver_mcp import cli


class CliTests(unittest.TestCase):
    def test_installation_scope_is_accepted_before_and_after_subcommand(self):
        for command in ("install", "uninstall"):
            for arguments, project in (
                ([command, "cursor"], False),
                (["--scope", "project", command, "cursor"], True),
                ([command, "--scope", "project", "cursor"], True),
                ([command, "cursor", "--scope", "project"], True),
                (["--scope", "project", command, "--scope", "global"], False),
            ):
                with self.subTest(command=command, arguments=arguments):
                    with (
                        patch("sys.argv", ["nodriver-mcp", *arguments]),
                        patch.object(cli, "run_install_command") as run,
                    ):
                        cli.main()
                    run.assert_called_once_with(
                        uninstall=command == "uninstall",
                        targets_str="cursor" if "cursor" in arguments else "",
                        project=project,
                    )
