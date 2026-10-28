"""Build hook: ship the committed ``schema/rig.schema.json`` inside the wheel as ``riglib/rig.schema.json``.

The wheel packages only ``riglib*``, so ``rig schema --check`` on an installed rig would have no file to compare. The
canonical file stays the one committed under ``schema/`` (no second tracked copy, no symlink); it is copied byte-for-byte
at build time and a build without it fails instead of producing a wheel that cannot verify itself. ``MANIFEST.in`` puts
the canonical file into the sdist, so sdist -> wheel builds work too.
"""

from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

ROOT = Path(__file__).resolve().parent


class BuildPyWithSchema(build_py):
    def run(self):
        super().run()
        source = ROOT / "schema" / "rig.schema.json"
        if not source.is_file():
            raise FileNotFoundError(f"{source} is missing: cannot package the schema into the wheel")
        if not self.dry_run:
            target = Path(self.build_lib) / "riglib" / "rig.schema.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())


setup(cmdclass={"build_py": BuildPyWithSchema})
