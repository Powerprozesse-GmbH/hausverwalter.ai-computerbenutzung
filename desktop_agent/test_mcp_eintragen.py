"""Offline tests for the MCP entry the installers write.

Run: .venv/bin/python -m unittest test_mcp_eintragen.py
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mcp_eintragen


class AddEntryTest(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.path = self.folder / "config.json"

    def test_program_not_installed_writes_nothing(self):
        path = self.folder / "fehlt" / "config.json"
        self.assertEqual(mcp_eintragen.add_entry(path, ["mcpServers"], {"command": "py"}), "kein Programm")
        self.assertFalse(path.exists())

    def test_keeps_other_entries_and_backs_up(self):
        self.path.write_text(json.dumps({"theme": "dunkel", "mcpServers": {"anderer": {"command": "x"}}}))
        self.assertEqual(mcp_eintragen.add_entry(self.path, ["mcpServers"], {"command": "py"}), "neu")
        config = json.loads(self.path.read_text())
        self.assertEqual(config["theme"], "dunkel")
        self.assertEqual(set(config["mcpServers"]), {"anderer", "computer"})
        self.assertIn("anderer", self.path.with_name("config.json.bak-hv").read_text())

    def test_second_run_changes_nothing(self):
        mcp_eintragen.add_entry(self.path, ["mcp", "servers"], {"command": "py"})
        self.assertEqual(mcp_eintragen.add_entry(self.path, ["mcp", "servers"], {"command": "py"}), "unverändert")

    def test_broken_file_stays_untouched(self):
        self.path.write_text("{kaputt")
        self.assertEqual(mcp_eintragen.add_entry(self.path, ["mcpServers"], {"command": "py"}), "Datei nicht lesbar")
        self.assertEqual(self.path.read_text(), "{kaputt")


class ClaudeConfigsTest(unittest.TestCase):
    """Windows Store/MSIX Claude app has no %APPDATA%\\Claude; it reads its packaged copy."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.local = self.home / "AppData" / "Local"
        env = {"APPDATA": str(self.home / "AppData" / "Roaming"), "LOCALAPPDATA": str(self.local)}
        for patcher in (mock.patch.object(mcp_eintragen.sys, "platform", "win32"), mock.patch.dict(mcp_eintragen.os.environ, env)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_msix_app_gets_entry_and_keeps_preferences(self):
        folder = self.local / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"
        folder.mkdir(parents=True)
        (folder / "claude_desktop_config.json").write_text(json.dumps({"preferences": {"x": 1}}))
        data = self.local / "Claude-Data" / "Claude_pzs8sxrjxfjjc"
        data.mkdir(parents=True)
        results = [mcp_eintragen.add_entry(p, ["mcpServers"], {"command": "py"}) for p in mcp_eintragen.claude_configs(self.home)]
        self.assertEqual(mcp_eintragen.best(results), "neu")
        config = json.loads((folder / "claude_desktop_config.json").read_text())
        self.assertEqual(config["preferences"], {"x": 1})
        self.assertIn("computer", config["mcpServers"])
        self.assertIn("computer", json.loads((data / "claude_desktop_config.json").read_text())["mcpServers"])

    def test_no_claude_app_writes_nothing(self):
        results = [mcp_eintragen.add_entry(p, ["mcpServers"], {"command": "py"}) for p in mcp_eintragen.claude_configs(self.home)]
        self.assertEqual(mcp_eintragen.best(results), "kein Programm")
        self.assertFalse(any(self.home.rglob("claude_desktop_config.json")))


class AddRuleTest(unittest.TestCase):
    def test_appends_once_and_keeps_text(self):
        path = Path(tempfile.mkdtemp()) / "AGENTS.md"
        path.write_text("eigene Regel\n")
        self.assertEqual(mcp_eintragen.add_rule(path), "neu")
        self.assertEqual(mcp_eintragen.add_rule(path), "unverändert")
        self.assertIn("eigene Regel", path.read_text())
        self.assertEqual(path.read_text().count(mcp_eintragen.RULE_MARK), 1)


if __name__ == "__main__":
    unittest.main()
