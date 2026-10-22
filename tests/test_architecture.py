"""Dependency rule: core modules depend on nothing near IO; adapters depend inward."""
import ast
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "eternalchat"
CORE = {"errors", "config", "tree", "llm", "prompt", "view", "compaction", "engine", "screen"}
ADAPTERS = {"storage", "provider", "openai_provider", "demo", "cli_provider", "cli_protocol", "machine", "computer", "history", "devices", "search", "device_state", "hooks", "audio", "push"}
COMPOSITION = {"app", "cli", "settings", "web", "remote", "web_imports", "device_client", "web_sessions", "__main__"}
IO_MODULES = {"os", "fcntl", "pathlib", "urllib", "socket", "threading", "subprocess",
              "shutil", "tempfile", "io", "sys", "argparse"}


def imports(module):
    tree = ast.parse((PACKAGE / f"{module}.py").read_text(encoding="utf-8"))
    internal, external = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                internal.add(node.module or "")
            else:
                external.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            external.update(alias.name.split(".")[0] for alias in node.names)
    return internal, external


class DependencyRuleTests(unittest.TestCase):
    def test_every_module_has_a_layer(self):
        modules = {p.stem for p in PACKAGE.glob("*.py")} - {"__init__"}
        self.assertEqual(modules, CORE | ADAPTERS | COMPOSITION)

    def test_core_imports_only_core_and_no_io(self):
        for module in CORE:
            internal, external = imports(module)
            self.assertLessEqual(internal, CORE, module)
            self.assertFalse(external & IO_MODULES, f"{module} imports {external & IO_MODULES}")

    def test_adapters_depend_inward_only(self):
        for module in ADAPTERS:
            internal, _ = imports(module)
            helpers = {"cli_provider": {"cli_protocol"}, "machine": {"computer"}, "device_state": {"storage"},
                       "openai_provider": {"provider"}, "hooks": {"machine"}}
            self.assertLessEqual(internal, CORE | helpers.get(module, set()), module)
            self.assertNotIn("engine", internal, module)

    def test_only_the_composition_root_wires_adapters(self):
        for module in CORE | ADAPTERS:
            internal, _ = imports(module)
            self.assertFalse(internal & COMPOSITION, module)
        self.assertNotIn("storage", imports("cli")[0], "the CLI opens chats through app.open_chat")
