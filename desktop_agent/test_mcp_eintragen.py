"""Offline tests for the MCP entry the installers write.

Run: .venv/bin/python -m unittest test_mcp_eintragen.py
"""

import json
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
