"""install.ps1 must stay pure ASCII.

PowerShell ends a string at typographic quotes, so „Erstellen“ inside "..." breaks the parser
(ticket 2026-73641). Windows PowerShell 5.1 also reads UTF-8 without BOM as ANSI.
Umlauts and German quotes in install.ps1 come from char codes ($ue, $lq, ...).
"""

import unittest
from pathlib import Path

INSTALL_PS1 = Path(__file__).resolve().parent.parent / "install.ps1"


class InstallPs1Test(unittest.TestCase):
    def test_pure_ascii(self):
        for number, line in enumerate(INSTALL_PS1.read_bytes().splitlines(), start=1):
            with self.subTest(line=number):
                self.assertTrue(line.isascii(), f"install.ps1 line {number} has a non-ASCII character")


if __name__ == "__main__":
    unittest.main()
