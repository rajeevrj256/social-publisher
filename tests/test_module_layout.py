"""Guards against a bug class that importing tests cannot see.

`src/scheduler.py` once had four functions appended *after* its
`if __name__ == "__main__": main()` block. Every test passed, because a
test imports the module and so executes every definition before calling
anything. Production ran `python -m src.scheduler`, which reached the
guard first, blocked inside `main()`, and never defined those functions
-- every tick died with `NameError: name 'run_schedule' is not defined`.
"""

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
ENTRYPOINTS = sorted(
    p for p in SRC.glob("*.py")
    if 'if __name__ == "__main__":' in p.read_text()
)


def test_entrypoints_discovered():
    # A rename must not silently empty the parametrised test below.
    assert {p.name for p in ENTRYPOINTS} >= {"scheduler.py", "worker.py"}


@pytest.mark.parametrize("path", ENTRYPOINTS, ids=lambda p: p.name)
def test_nothing_defined_after_main_guard(path):
    tree = ast.parse(path.read_text(), filename=str(path))
    guards = [
        node for node in tree.body
        if isinstance(node, ast.If)
        and ast.dump(node.test).find("__main__") != -1
    ]
    assert len(guards) == 1, f"{path.name}: expected one __main__ guard"
    guard = guards[0]

    trailing = [
        node for node in tree.body
        if node.lineno > guard.lineno
        and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef, ast.Assign, ast.Import,
                              ast.ImportFrom))
    ]
    names = [getattr(n, "name", type(n).__name__) for n in trailing]
    assert not trailing, (
        f"{path.name}: {names} defined after the __main__ guard on line "
        f"{guard.lineno}. Running as `python -m` never reaches them, so "
        f"they are NameErrors at runtime even though imports work."
    )
