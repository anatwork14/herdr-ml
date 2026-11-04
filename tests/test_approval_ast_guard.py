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
DYNAMIC_NAMES = {'__import__', 'import_module', 'compile', 'exec', 'eval', 'globals', 'getmembers', '__dict__'}
VERIFIER_BASES = {'HandoffVerifier', 'SignatureVerifier'}
# Exact allow-listed calls. re.compile is the regex compiler, matched only as the attribute
# 'compile' on the name 're'; it never compiles code. The builtin compile(), exec and eval stay
# flagged. runtime_identity.py line 65 only tests membership of sitecustomize/usercustomize in
# sys.modules for an identity report; it neither reads nor writes a module. Keyed by file and
# line, so any move or addition there is flagged again.
ALLOWED_SYS_MODULES = {('runtime_identity.py', 65)}
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
    if not is_approval:
        found.extend(_extended(tree, path.name))
    return found


def _extended(tree, filename=''):
    found = []
    for node in ast.walk(tree):
        line = getattr(node, 'lineno', 0)
        name = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
            if name == 'modules' and isinstance(node.value, ast.Name) and node.value.id == 'sys' \
                    and (filename, line) not in ALLOWED_SYS_MODULES:
                found.append((line, 'sys.modules'))
            if name == 'compile' and isinstance(node.value, ast.Name) and node.value.id == 're':
                continue
        if name in DYNAMIC_NAMES:
            found.append((line, 'dynamic access %s' % name))
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                bname = base.id if isinstance(base, ast.Name) else getattr(base, 'attr', None)
                if bname in VERIFIER_BASES:
                    found.append((line, 'subclass of %s' % bname))
        if isinstance(node, ast.Call):
            fname = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, 'attr', None)
            if fname == 'TrustAnchor':
                found.append((line, 'TrustAnchor construction'))
        if isinstance(node, ast.ImportFrom) and node.module == 'sys' and any(a.name == 'modules' for a in node.names):
            found.append((line, 'from sys import modules'))
    return found


def check_source(source, name='synthetic.py'):
    return violations(Path(name), ast.parse(source))


class ScriptsHaveNoTestOnlyAcceptance(unittest.TestCase):
    def test_scripts_exist(self):
        self.assertTrue(any(p.name == 'approval.py' for p in _modules()))

    def test_no_named_test_only_pattern_in_any_script(self):
        for path in _modules():
            tree = ast.parse(path.read_text(), filename=str(path))
            self.assertEqual(violations(path, tree), [], str(path))

    def test_real_scripts_tree_is_clean_under_the_extended_checks(self):
        for path in _modules():
            if path.name == 'approval.py':
                continue
            tree = ast.parse(path.read_text(), filename=str(path))
            self.assertEqual(_extended(tree, path.name), [], str(path))

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


class ExtendedCategories(unittest.TestCase):
    def _check_each(self, sources):
        import tempfile
        with tempfile.TemporaryDirectory(dir=str(Path(__file__).resolve().parents[1] / '.test-tmp')
                                         if (Path(__file__).resolve().parents[1] / '.test-tmp').is_dir()
                                         else None) as tmp:
            for index, source in enumerate(sources):
                path = Path(tmp) / ('synthetic_%d.py' % index)
                path.write_text(source)
                with self.subTest(source=source):
                    self.assertTrue(violations(path, ast.parse(path.read_text())), source)

    def test_dunder_import_and_import_module(self):
        self._check_each(['__import__("approval")', 'import importlib\nimportlib.import_module("approval")',
                          'from importlib import import_module\nimport_module("x")'])

    def test_compile_exec_eval(self):
        self._check_each(['compile("1", "f", "eval")', 'exec("x=1")', 'eval("1")'])

    def test_globals_and_dict_and_getmembers(self):
        self._check_each(['globals()', 'x.__dict__', 'import inspect\ninspect.getmembers(x)',
                          'from inspect import getmembers\ngetmembers(x)'])

    def test_sys_modules(self):
        self._check_each(['import sys\nsys.modules["approval"]', 'import sys\nsys.modules.get("approval")',
                          'from sys import modules'])

    def test_verifier_subclasses(self):
        self._check_each(['class V(HandoffVerifier): pass', 'import approval\nclass V(approval.HandoffVerifier): pass',
                          'class S(SignatureVerifier): pass'])

    def test_trust_anchor_construction(self):
        self._check_each(['TrustAnchor(1, 2, 3, 4, 5)', 'import approval\napproval.TrustAnchor(1)'])

    def test_approval_module_itself_is_exempt_and_clean_code_passes(self):
        self.assertEqual(violations(Path('approval.py'), ast.parse('exec("x")\nclass V(HandoffVerifier): pass')), [])
        self.assertEqual(check_source('import re\nre.compile("a")'), [])
        self.assertTrue(check_source('import regex\nregex.compile("a")'))
        self.assertEqual(check_source('import os\nx = os.path.join("a", "b")'), [])


if __name__ == '__main__':
    unittest.main()
