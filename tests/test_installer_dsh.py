import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from nodriver_mcp import installer, installer_dsh
from nodriver_mcp.installer_data import (
    DSH_CLIENT_NAME,
    get_dsh_profile_dir,
)

COMMAND = "/opt/tool/bin/python"
ARGS = ["-m", "nodriver_mcp"]

EXISTING = """\
# profile tweaks
- id: web
  name: "@deepseek-ai/dsh-web"
  config:
    searchProvider: deepseek-search-api

- id: hmr
  name: '@deepseek-ai/dsh-hmr'
  disabled: !!js "!ctx.get('profileContext')"
"""

MANUAL_ROW = """\
- insert:
    - id: mcp-nodriver
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: nodriver
        transport: stdio
        command: "/old/python"
        toolCallTimeoutMs: 120000
"""


class RenderTests(unittest.TestCase):
    def test_block_registers_the_harness_bridge(self):
        block = installer_dsh.render_block(COMMAND, ARGS)
        self.assertTrue(block.startswith("- insert:\n"))
        self.assertIn("    - id: mcp-nodriver\n", block)
        self.assertIn("      name: '@deepseek-ai/dsh-mcp-client'\n", block)
        self.assertIn("        serverName: nodriver\n", block)
        self.assertIn("        transport: stdio\n", block)
        self.assertIn(f'        command: "{COMMAND}"\n', block)
        self.assertIn('        args: ["-m", "nodriver_mcp"]\n', block)

    def test_command_is_escaped_as_a_json_string(self):
        block = installer_dsh.render_block("C:\\Program Files\\py\\python.exe")
        self.assertIn('command: "C:\\\\Program Files\\\\py\\\\python.exe"', block)
        self.assertIn("args: []", block)


class UpsertTests(unittest.TestCase):
    def test_empty_file_gets_only_the_row(self):
        text = installer_dsh.upsert("", COMMAND, ARGS)
        self.assertEqual(text, installer_dsh.render_block(COMMAND, ARGS))
        self.assertEqual(text.count("id: mcp-nodriver"), 1)

    def test_existing_entries_are_preserved(self):
        text = installer_dsh.upsert(EXISTING, COMMAND, ARGS)
        self.assertTrue(text.startswith(EXISTING))
        self.assertEqual(text.count("id: mcp-nodriver"), 1)
        self.assertIn("!!js", text)

    def test_reinstall_is_a_no_op(self):
        once = installer_dsh.upsert(EXISTING, COMMAND, ARGS)
        self.assertEqual(installer_dsh.upsert(once, COMMAND, ARGS), once)

    def test_manual_row_is_replaced_instead_of_duplicated(self):
        text = installer_dsh.upsert(EXISTING + "\n" + MANUAL_ROW, COMMAND, ARGS)
        self.assertEqual(text.count("id: mcp-nodriver"), 1)
        self.assertNotIn("/old/python", text)
        self.assertNotIn("toolCallTimeoutMs", text)
        self.assertIn(f'command: "{COMMAND}"', text)
        self.assertIn(EXISTING, text)

    def test_row_shared_with_other_entries_keeps_its_siblings(self):
        shared = EXISTING + """
- insert:
    - id: mcp-other
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: other
    - id: mcp-nodriver
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: nodriver
"""
        text = installer_dsh.upsert(shared, COMMAND, ARGS)
        self.assertIn("id: mcp-other", text)
        self.assertEqual(text.count("id: mcp-nodriver"), 1)

    def test_root_placeholder_is_replaced(self):
        text = installer_dsh.upsert("[]\n", COMMAND, ARGS)
        self.assertNotIn("[]", text)
        self.assertTrue(text.startswith("- insert:"))

    def test_comments_stay_in_place(self):
        text = installer_dsh.upsert("# keep me\n", COMMAND, ARGS)
        self.assertTrue(text.startswith("# keep me\n"))
        self.assertIn("- insert:", text)

    def test_crlf_documents_keep_their_line_endings(self):
        windows = '- id: web\r\n  name: "@deepseek-ai/dsh-web"\r\n'
        text = installer_dsh.upsert(windows, COMMAND, ARGS)
        self.assertTrue(text.startswith(windows))
        self.assertNotIn("\n", text.replace("\r\n", ""))
        self.assertEqual(installer_dsh.upsert(text, COMMAND, ARGS), text)
        self.assertEqual(installer_dsh.remove(text), windows)

    def test_missing_final_newline_is_tolerated(self):
        text = installer_dsh.upsert("- id: web\n  name: web", COMMAND, ARGS)
        self.assertTrue(text.startswith("- id: web\n  name: web\n\n- insert:"))

    def test_duplicate_rows_collapse_to_one(self):
        doubled = (
            EXISTING
            + "\n"
            + installer_dsh.render_block("/old/python", ["-m", "old"])
            + "\n"
            + installer_dsh.render_block("/older/python")
        )
        text = installer_dsh.upsert(doubled, COMMAND, ARGS)
        self.assertEqual(text.count("id: mcp-nodriver"), 1)
        self.assertNotIn("/old", text)
        self.assertEqual(installer_dsh.remove(text), EXISTING)

    def test_exact_block_with_stale_duplicate_is_repaired(self):
        doubled = (
            EXISTING
            + "\n"
            + installer_dsh.render_block(COMMAND, ARGS)
            + "\n"
            + installer_dsh.render_block("/old/python", ["-m", "old"])
        )
        text = installer_dsh.upsert(doubled, COMMAND, ARGS)
        self.assertEqual(text.count("id: mcp-nodriver"), 1)
        self.assertNotIn("/old", text)

    def test_bom_is_preserved_and_does_not_hide_the_placeholder(self):
        text = installer_dsh.upsert("\ufeff[]\n", COMMAND, ARGS)
        self.assertTrue(text.startswith("\ufeff- insert:"))
        self.assertEqual(text.count("[]"), 0)
        self.assertTrue(installer_dsh.has_row(text))
        self.assertTrue(installer_dsh.remove(text).startswith("\ufeff[]"))


class RemoveTests(unittest.TestCase):
    def test_round_trip_restores_the_placeholder(self):
        installed = installer_dsh.upsert("[]\n", COMMAND, ARGS)
        self.assertEqual(installer_dsh.remove(installed), "[]\n")

    def test_removing_keeps_other_entries(self):
        text = installer_dsh.remove(installer_dsh.upsert(EXISTING, COMMAND, ARGS))
        self.assertIn("- id: web", text)
        self.assertIn("!!js", text)
        self.assertFalse(installer_dsh.has_row(text))

    def test_removing_the_only_entry_leaves_a_bootable_file(self):
        text = installer_dsh.remove(installer_dsh.upsert("", COMMAND, ARGS))
        self.assertEqual(text, "[]\n")

    def test_removing_from_a_group_leaves_an_empty_list(self):
        grouped = """\
- id: grouped
  group: true
  config:
    - insert:
        - id: mcp-nodriver
          name: '@deepseek-ai/dsh-mcp-client'
"""
        self.assertEqual(
            installer_dsh.remove(grouped),
            "- id: grouped\n  group: true\n  config:\n    []\n",
        )

    def test_comment_after_the_row_survives(self):
        document = """\
- insert:
    - id: mcp-nodriver
      name: '@deepseek-ai/dsh-mcp-client'

# web section
- id: web
  name: "@deepseek-ai/dsh-web"
"""
        text = installer_dsh.remove(document)
        self.assertIn("# web section", text)
        self.assertIn("- id: web", text)
        self.assertFalse(installer_dsh.has_row(text))

    def test_comment_inside_the_row_is_removed_with_it(self):
        document = """\
- insert:
    - id: mcp-nodriver
# note
      name: '@deepseek-ai/dsh-mcp-client'
"""
        self.assertEqual(installer_dsh.remove(document), "[]\n")

    def test_comment_between_group_and_wrapper_keeps_config_valid(self):
        grouped = """\
- id: grouped
  group: true
  config:
    # nodriver plugin
    - insert:
        - id: mcp-nodriver
          name: '@deepseek-ai/dsh-mcp-client'
"""
        self.assertEqual(
            installer_dsh.remove(grouped),
            "- id: grouped\n  group: true\n  config:\n    # nodriver plugin\n    []\n",
        )

    def test_removing_without_a_row_is_a_no_op(self):
        self.assertEqual(installer_dsh.remove(EXISTING), EXISTING)

    def test_shared_wrapper_survives(self):
        shared = EXISTING + """
- insert:
    - id: mcp-other
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: other
    - id: mcp-nodriver
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: nodriver
"""
        text = installer_dsh.remove(shared)
        self.assertIn("id: mcp-other", text)
        self.assertNotIn("id: mcp-nodriver", text)
        self.assertIn("- insert:", text)


class ProfileDirTests(unittest.TestCase):
    @staticmethod
    def _profiles(root, *names):
        for name in names:
            os.makedirs(os.path.join(root, "profiles", name))

    def _select(self, root, **environment):
        with patch.dict(os.environ, {"DSH_HOME": root, **environment}, clear=True):
            return get_dsh_profile_dir()

    def test_profile_comes_from_the_harness_environment(self):
        with tempfile.TemporaryDirectory() as root:
            self._profiles(root, "work", "desktop")
            self.assertEqual(
                self._select(root, DSH_PROFILE="work"),
                os.path.join(root, "profiles", "work"),
            )

    def test_selected_profile_is_not_replaced_when_missing(self):
        with tempfile.TemporaryDirectory() as root:
            self._profiles(root, "desktop")
            self.assertEqual(
                self._select(root, DSH_PROFILE="work"),
                os.path.join(root, "profiles", "work"),
            )

    def test_desktop_profile_is_the_default(self):
        with tempfile.TemporaryDirectory() as root:
            self._profiles(root, "desktop", "web")
            self.assertEqual(
                self._select(root),
                os.path.join(root, "profiles", "desktop"),
            )

    def test_single_profile_is_discovered(self):
        with tempfile.TemporaryDirectory() as root:
            self._profiles(root, "custom")
            self.assertEqual(
                self._select(root),
                os.path.join(root, "profiles", "custom"),
            )

    def test_remaining_profiles_are_selected_in_name_order(self):
        with tempfile.TemporaryDirectory() as root:
            self._profiles(root, "beta", "alpha")
            self.assertEqual(
                self._select(root),
                os.path.join(root, "profiles", "alpha"),
            )


class ProjectScopeTests(unittest.TestCase):
    def test_deepseek_points_at_the_profile_scope(self):
        output = StringIO()
        with (
            patch.object(
                installer,
                "get_project_configs",
                return_value={"Cursor": (".cursor", "mcp.json")},
            ),
            patch.object(
                installer,
                "get_global_configs",
                return_value={DSH_CLIENT_NAME: ("/tmp/dsh", "cordis.patch.yml")},
            ),
            redirect_stdout(output),
        ):
            installer.install_mcp_servers(only=["deepseek"], project=True)
        message = output.getvalue()
        self.assertIn("not a project directory", message)
        self.assertIn("without --scope project", message)


class InstallCommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config_dir = os.path.join(self.directory.name, "profiles", "desktop")
        os.makedirs(self.config_dir)
        self.config_path = os.path.join(self.config_dir, "cordis.patch.yml")
        self.configs = {DSH_CLIENT_NAME: (self.config_dir, "cordis.patch.yml")}

    def _run(self, *, uninstall=False):
        output = StringIO()
        with (
            patch.object(installer, "get_global_configs", return_value=self.configs),
            redirect_stdout(output),
        ):
            installer.install_mcp_servers(uninstall=uninstall, only=["deepseek"])
        return output.getvalue()

    def _read(self):
        with open(self.config_path, encoding="utf-8") as f:
            return f.read()

    def test_install_then_uninstall(self):
        message = self._run()
        self.assertIn("Installed DeepSeek Harness", message)
        self.assertIn(self.config_path, message)
        text = self._read()
        self.assertTrue(installer_dsh.has_row(text))
        self.assertIn("serverName: nodriver", text)

        message = self._run(uninstall=True)
        self.assertIn("Uninstalled DeepSeek Harness", message)
        self.assertEqual(self._read(), "[]\n")

    def test_install_keeps_existing_profile_configuration(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write(EXISTING)
        self._run()
        text = self._read()
        self.assertTrue(text.startswith(EXISTING))
        self.assertEqual(text.count("id: mcp-nodriver"), 1)

    def test_installed_state_is_reported_for_interactive_tui(self):
        self._run()
        installed = installer._is_installed(
            DSH_CLIENT_NAME, self.config_dir, "cordis.patch.yml", {}, False
        )
        self.assertTrue(installed)

    def test_missing_profile_is_skipped(self):
        self.configs = {
            DSH_CLIENT_NAME: (os.path.join(self.directory.name, "nope"), "cordis.patch.yml")
        }
        message = self._run()
        self.assertIn("directory not found", message)
        self.assertFalse(os.path.exists(self.config_path))


if __name__ == "__main__":
    unittest.main()
