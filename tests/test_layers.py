"""The package's layers (docs/DESIGN.md, section 5). Each module imports only from its own layer and the layers
before it, and scoring and reporting import none of the code that acts on the live system, which is why a stored
run can be scored again on any machine. The imports are read from the syntax tree of every module."""
import ast
import os
import unittest

import stopkit

PKG = os.path.dirname(os.path.abspath(stopkit.__file__))

# The layers, in order. A module may import from its own layer and from the layers before it.
LAYERS = (
    ('', 'spec'),                 # the package itself, which holds only its version; the catalogue, as data
    ('system',),                  # the machine
    ('observe',),                 # the listings
    ('truth',),                   # the ground truth and the bench instruments
    ('stops', 'agents'),          # acting: the generic stops; the adapter interface and the stand-ins
    ('bindings',),                # the ready-made bindings
    ('score',),                   # scoring
    ('report',),                  # records and reports
    ('run',),                     # running
    ('cli', '__main__'),          # the commands
)
LAYER_OF = {name: i for i, names in enumerate(LAYERS) for name in names}
# What scoring and reporting must not import: everything that runs commands, reads the process table, acts on an
# account or starts the instruments. They may use the data types defined next to such code.
ACTING = ('stopkit.system.cmd', 'stopkit.system.procfs', 'stopkit.system.guards', 'stopkit.system.egress',
          'stopkit.truth.bench', 'stopkit.stops', 'stopkit.agents.delegation', 'stopkit.agents.model_standin',
          'stopkit.agents.mcp_server', 'stopkit.agents.worker_standin', 'stopkit.bindings', 'stopkit.run')


def modules() -> dict[str, str]:
    """Every module of the package: dotted name -> path."""
    out = {}
    for d, dirs, names in os.walk(PKG):
        dirs[:] = sorted(x for x in dirs if x != '__pycache__')
        for n in names:
            if n.endswith('.py'):
                rel = os.path.relpath(os.path.join(d, n), os.path.dirname(PKG))[:-3].replace(os.sep, '.')
                out[rel[:-len('.__init__')] if rel.endswith('.__init__') else rel] = os.path.join(d, n)
    return out


MODULES = modules()


def imports(mod: str, path: str) -> set[str]:
    """The package's modules that `mod` imports, by absolute name."""
    is_init = os.path.basename(path) == '__init__.py'
    out = set()
    with open(path, encoding='utf-8') as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names if a.name == 'stopkit' or a.name.startswith('stopkit.')}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = mod.split('.') if is_init else mod.split('.')[:-1]
                base = base[:len(base) - (node.level - 1)]
                target = '.'.join(base + ([node.module] if node.module else []))
            else:
                target = node.module or ''
            if target != 'stopkit' and not target.startswith('stopkit.'):
                continue
            for a in node.names:
                out.add(f'{target}.{a.name}' if f'{target}.{a.name}' in MODULES else target)
    return out - {mod}


def layer(mod: str) -> int:
    parts = mod.split('.')
    return LAYER_OF[parts[1] if len(parts) > 1 else '']


class LayerTest(unittest.TestCase):
    def test_every_module_belongs_to_a_layer(self):
        stray = [m for m in MODULES if (m.split('.')[1] if '.' in m else '') not in LAYER_OF]
        self.assertEqual([], stray, 'modules outside every layer of docs/DESIGN.md, section 5')

    def test_each_module_imports_only_its_own_layer_and_earlier_ones(self):
        wrong = sorted(f'{m} imports {t}' for m, p in MODULES.items() for t in imports(m, p) if layer(t) > layer(m))
        self.assertEqual([], wrong)

    def test_scoring_and_reporting_import_nothing_that_acts_on_the_live_system(self):
        wrong = sorted(f'{m} imports {t}' for m, p in MODULES.items() if m.split('.')[1:2] in (['score'], ['report'])
                       for t in imports(m, p) if any(t == a or t.startswith(a + '.') for a in ACTING))
        self.assertEqual([], wrong)


if __name__ == '__main__':
    unittest.main()
