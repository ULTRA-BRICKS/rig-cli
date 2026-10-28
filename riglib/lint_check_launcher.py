"""Trusted, cwd-independent entrypoint for the `rig-check` child of `rig lint run`.

Context: `rig lint run` executes the nested `rig lint check` with the linted repository as the
working directory. `python -m riglib` there would put the cwd first on `sys.path`, so a project that
ships its own `riglib.py` / `riglib/` would be IMPORTED instead of this Rig (arbitrary repository
code running with the user's rights, and a forged verdict). Running this file by path avoids that:
the interpreter never adds the cwd for a script, and the two path entries it does add are cleaned up
below before anything is imported.

Usage (argv built by the `rig-check` provider): `<python> <this file> lint check --json -C <root> ...`.
Only the standard library is imported before `sys.path` is repaired.
"""
from __future__ import annotations

import os
import sys


def _trusted_path(root: str, here: str) -> list[str]:
    """`sys.path` without this file's own directory and the cwd, with Rig's package root first.

    `riglib/` itself must not stay on the path: it holds modules named like stdlib ones (`profile`),
    and the cwd is the untrusted repository.
    """
    cwd = os.path.realpath(os.getcwd())
    kept = [entry for entry in sys.path
            if os.path.realpath(entry or ".") not in (here, cwd)]
    return [root, *kept]


def main() -> int:
    here = os.path.dirname(os.path.realpath(__file__))
    sys.path[:] = _trusted_path(os.path.dirname(here), here)
    from riglib.cli import main as cli_main

    return cli_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
