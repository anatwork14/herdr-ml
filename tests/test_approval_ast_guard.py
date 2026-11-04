"""AST guard: no test-only acceptance path in scripts/.

It detects only the named patterns below. It cannot stop code that is added to scripts/
and written to avoid them; in-process code under scripts/ is outside the threat model.
"""
import ast
from pathlib import Path
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
BANNED_NAMES = {'TEST_CAPABILITY', 'test_only', 'TestOnlyHandoffVerifier', '_test_only', 'test_capability',
                '_grant_test_capability', '_test_root', 'test_root', 'TEST_ONLY_PREFIX', 'TEST_MODE'}
REFLECTION = {'getattr', 'vars', 'gc', 'setattr', 'delattr'}


def _modules():
    return sorted(SCRIPTS.rglob('*.py'))


def _approval_aliases(tree):
    """Names bound to the approval module in this file."""
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split('.')[-1] == 'approval':
                    aliases.add(alias.asname or alias.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == 'approval':
                    aliases.add(alias.asname or alias.name)
    return aliases


def violations(path, tree):
    found = []
    is_approval = path.name == 'approval.py'
    aliases = _approval_aliases(tree)
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
        elif isinstance(node, ast.arg):
            names.append(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            names.append(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            names.append(node.name)
        for name in names:
            if name in BANNED_NAMES:
                found.append((getattr(node, 'lineno', 0), 'banned name %s' % name))
        if isinstance(node, ast.keyword) and node.arg == 'mode' and isinstance(node.value, ast.Constant) \
                and node.value.value == 'test':
            found.append((getattr(node, 'lineno', 0), "mode='test'"))
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [a.name for a in node.names]
            if isinstance(node, ast.ImportFrom):
                modules.append(node.module or '')
                modules.extend('%s.%s' % (node.module, a.name) for a in node.names if node.module)
            for module in modules:
                parts = module.replace('/', '.').split('.')
                if 'support' in parts or 'tests' in parts:
                    found.append((getattr(node, 'lineno', 0), 'imports %s' % module))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in REFLECTION:
            first = node.args[0] if node.args else None
            if (isinstance(first, ast.Name) and first.id in aliases) or (
                    is_approval and isinstance(first, ast.Name) and first.id in ('sys', 'module')):
                found.append((getattr(node, 'lineno', 0), '%s on the approval module' % node.func.id))
    if not is_approval:
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = [a.name for a in node.names] + [getattr(node, 'module', None) or '']
                if 'gc' in mods:
                    uses_approval = bool(aliases)
                    if uses_approval:
                        found.append((getattr(node, 'lineno', 0), 'gc imported next to the approval module'))
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) \
                    and node.value.attr == 'modules' and 'approval' in ast.dump(node.slice):
                found.append((getattr(node, 'lineno', 0), "sys.modules['approval']"))
    return found


class ScriptsHaveNoTestOnlyAcceptance(unittest.TestCase):
    def test_scripts_exist(self):
        self.assertTrue(any(p.name == 'approval.py' for p in _modules()))

    def test_no_named_test_only_pattern_in_any_script(self):
        for path in _modules():
            tree = ast.parse(path.read_text(), filename=str(path))
            self.assertEqual(violations(path, tree), [], str(path))

    def test_guard_detects_each_named_pattern(self):
        samples = ['TEST_CAPABILITY = object()', 'def f(test_only=False): pass', 'x.test_only',
                   'class TestOnlyHandoffVerifier: pass', 'f(mode=%r)' % 'test',
                   'import support.approval_fixtures', 'from support import approval_fixtures',
                   'from tests.support import x', 'import approval\ngetattr(approval, "x")',
                   'import approval\nvars(approval)', 'import approval as a\ngetattr(a, "x")',
                   'import approval\nimport gc', "import sys\nx = sys.modules['approval']"]
        for source in samples:
            with self.subTest(source=source):
                self.assertTrue(violations(Path('other.py'), ast.parse(source)), source)
        self.assertEqual(violations(Path('other.py'), ast.parse('import os\ngetattr(os, "x")')), [])


if __name__ == '__main__':
    unittest.main()
