"""Offline tests for the key store: keyring replaced by a dict, the key is never printed.

Run: PYTHONPATH=.. .venv/bin/python -m unittest test_api_key.py
"""

import contextlib
import io
import sys
import unittest
from types import ModuleType
from unittest import mock

import api_key

KEY = "hv_live_0123456789abcdefXYZ"


def fake_keyring():
    store = {}
    errors = ModuleType("keyring.errors")

    class PasswordDeleteError(Exception):
        pass

    def delete(service, account):
        if (service, account) not in store:
            raise PasswordDeleteError(service)
        del store[(service, account)]

    errors.PasswordDeleteError = PasswordDeleteError
    module = ModuleType("keyring")
    module.get_password = lambda service, account: store.get((service, account))
    module.set_password = lambda service, account, value: store.__setitem__((service, account), value)
    module.delete_password = delete
    module.errors = errors
    return store, {"keyring": module, "keyring.errors": errors}


class ApiKeyTest(unittest.TestCase):
    def setUp(self):
        self.store, modules = fake_keyring()
        patcher = mock.patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, *args, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = api_key.main(list(args))
        self.assertNotIn(KEY, out.getvalue() + err.getvalue())  # never printed
        return code, out.getvalue() + err.getvalue()

    def test_setup_from_stdin_stores_in_the_key_store(self):
        self.assertEqual(self.run_main("pruefen")[0], 1)
        code, text = self.run_main("einrichten", "--stdin", stdin=f"  {KEY}\n")
        self.assertEqual(code, 0)
        self.assertIn("sicher gespeichert", text)
        self.assertEqual(self.store, {("hausverwalter-computer", "api-key"): KEY})
        self.assertEqual(api_key.load(), KEY)
        self.assertEqual(self.run_main("pruefen")[0], 0)
        self.assertEqual(self.run_main("loeschen")[0], 0)
        self.assertIsNone(api_key.load())
        self.assertEqual(self.run_main("loeschen")[0], 0)  # twice is fine

    def test_interactive_setup_uses_hidden_input(self):
        with mock.patch.object(api_key.getpass, "getpass", return_value=KEY) as hidden:
            self.assertEqual(self.run_main("einrichten")[0], 0)
        hidden.assert_called_once()
        self.assertEqual(api_key.load(), KEY)

    def test_rejects_what_is_not_a_key(self):
        for bad in ("", "kurz", "hv live 0123456789abcdef"):
            code, text = self.run_main("einrichten", "--stdin", stdin=bad)
            self.assertEqual(code, 1)
            self.assertIn("nicht nach einem API-Schlüssel", text)
        self.assertEqual(self.store, {})


if __name__ == "__main__":
    unittest.main()
