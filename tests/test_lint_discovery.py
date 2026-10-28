"""Behavior of ``riglib.lint_discovery.discover`` on real throwaway git repositories."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from riglib import lint_discovery, lint_discovery_io
from riglib.lint_check_scope import ScopeError
from riglib.lint_discovery import discover, parse_jsonc
from tests.lint_discovery_helpers import make_repo, pkg, scan, scope_of, tool_row, tool_rows


def test_mixed_nested_repo(tmp_path):
    d = scan(tmp_path, {
        "package.json": pkg(name="root", private=True, devDependencies={"eslint": "^9"}),
        "pnpm-workspace.yaml": "packages:\n  - packages/*\n",
        "pnpm-lock.yaml": "lockfileVersion: 9\n",
        "packages/a/package.json": pkg(name="a", devDependencies={"typescript": "^5.4.0"}),
        "packages/a/tsconfig.json": "{}",
        "packages/a/src/index.ts": "export const a = 1\n",
        "packages/b/package.json": pkg(name="b"),
        "py/pyproject.toml": '[project]\nname = "p"\n[tool.uv]\ndev-dependencies = ["ruff"]\n',
        "py/uv.lock": "version = 1\n",
        "py/app.py": "x = 1\n",
        "rs/Cargo.toml": '[workspace]\nmembers = ["crates/m"]\n',
        "rs/crates/m/Cargo.toml": '[package]\nname = "m"\n',
        "rs/crates/m/src/lib.rs": "pub fn f() {}\n",
        "svc/go.mod": "module example.com/svc\n\ngo 1.22\n",
        "svc/main.go": "package main\n",
        "scripts/run.sh": "#!/bin/sh\necho hi\n",
        ".github/workflows/ci.yml": "name: ci\n",
    })
    assert [s.rel for s in d.scopes] == [".", "packages/a", "packages/b", "py", "rs", "svc"]
    assert scope_of(d, "packages/a").languages == ("typescript",)
    assert scope_of(d, "packages/b").languages == ("javascript",)
    assert scope_of(d, "py").languages == ("python",)
    assert scope_of(d, "rs").languages == ("rust",)
    assert scope_of(d, "svc").languages == ("go",)
    assert scope_of(d, "svc").files["go"] == ("svc/main.go",)
    root = scope_of(d, ".")
    assert root.files["shell"] == ("scripts/run.sh",)
    assert root.files["github-workflows"] == (".github/workflows/ci.yml",)
    assert scope_of(d, "py").package_manager.name == "uv"
    assert scope_of(d, "packages/a").package_manager.root == "."
    assert d.source_files["rust"] == ("rs/crates/m/src/lib.rs",)
    assert d.file_count == 17
    assert d.problems == ()


@pytest.mark.parametrize(("files", "name", "source", "lockfile"), [
    ({"package.json": pkg(packageManager="pnpm@9.1.0"), "package-lock.json": "{}"}, "pnpm", "packageManager field", None),
    ({"package.json": pkg(), "package-lock.json": "{}"}, "npm", "lockfile", "package-lock.json"),
    ({"package.json": pkg(), "pnpm-lock.yaml": ""}, "pnpm", "lockfile", "pnpm-lock.yaml"),
    ({"package.json": pkg(), "yarn.lock": ""}, "yarn", "lockfile", "yarn.lock"),
    ({"package.json": pkg(), "yarn.lock": "", ".yarnrc.yml": "x: 1\n"}, "yarn-berry", "lockfile", "yarn.lock"),
    ({"package.json": pkg(packageManager="yarn@4.2.1")}, "yarn-berry", "packageManager field", None),
    ({"package.json": pkg(packageManager="yarn@1.22.0")}, "yarn", "packageManager field", None),
    ({"package.json": pkg(), "bun.lock": ""}, "bun", "lockfile", "bun.lock"),
    ({"package.json": pkg(), "bun.lockb": ""}, "bun", "lockfile", "bun.lockb"),
    ({"package.json": pkg(), "pnpm-workspace.yaml": "packages: []\n"}, "pnpm", "workspace marker", None),
    ({"package.json": pkg()}, "npm", "default", None),
])
def test_js_manager_selection(tmp_path, files, name, source, lockfile):
    manager = scope_of(scan(tmp_path, files), ".").package_manager
    assert (manager.ecosystem, manager.name, manager.source, manager.lockfile, manager.root) == ("js", name, source, lockfile, ".")


def test_field_wins_and_lockfile_is_found_when_it_matches(tmp_path):
    manager = scope_of(scan(tmp_path, {"package.json": pkg(packageManager="pnpm@9.0.0"), "pnpm-lock.yaml": ""}), ".").package_manager
    assert (manager.name, manager.source, manager.lockfile) == ("pnpm", "packageManager field", "pnpm-lock.yaml")


def test_two_lockfiles_without_field_are_ambiguous(tmp_path):
    manager = scope_of(scan(tmp_path, {"package.json": pkg(), "yarn.lock": "", "pnpm-lock.yaml": ""}), ".").package_manager
    assert (manager.name, manager.source, manager.ambiguous, manager.lockfile) == ("", "ambiguous", ("pnpm", "yarn"), None)


@pytest.mark.parametrize(("files", "name", "source", "lockfile"), [
    ({"pyproject.toml": "[tool.uv]\n", "uv.lock": ""}, "uv", "lockfile", "uv.lock"),
    ({"pyproject.toml": "[tool.uv]\n"}, "uv", "workspace marker", None),
    ({"pyproject.toml": "[project]\nname='x'\n", "poetry.lock": ""}, "poetry", "lockfile", "poetry.lock"),
    ({"pyproject.toml": "[tool.poetry]\nname='x'\n"}, "poetry", "workspace marker", None),
    ({"pyproject.toml": "[project]\nname='x'\n", "pdm.lock": ""}, "pdm", "lockfile", "pdm.lock"),
    ({"pyproject.toml": "[tool.pdm]\n"}, "pdm", "workspace marker", None),
    ({"pyproject.toml": "[project]\nname='x'\n"}, "pip", "default", None),
    ({"requirements.txt": "requests\n"}, "pip", "default", None),
    ({"requirements-dev.txt": "pytest\n"}, "pip", "default", None),
    ({"setup.py": "from setuptools import setup\nsetup()\n"}, "pip", "default", None),
    ({"setup.cfg": "[metadata]\nname = x\n"}, "pip", "default", None),
])
def test_python_manager_selection(tmp_path, files, name, source, lockfile):
    manager = scope_of(scan(tmp_path, files), ".").package_manager
    assert (manager.ecosystem, manager.name, manager.source, manager.lockfile) == ("python", name, source, lockfile)


def test_child_without_lock_inherits_lock_owning_ancestor(tmp_path):
    d = scan(tmp_path, {
        "package.json": pkg(), "pnpm-lock.yaml": "", "pnpm-workspace.yaml": "",
        "packages/x/package.json": pkg(),
        "py/pyproject.toml": "[tool.uv]\n", "py/uv.lock": "",
        "py/sub/pyproject.toml": "[project]\nname='s'\n",
    })
    child = scope_of(d, "packages/x").package_manager
    assert (child.name, child.root, child.lockfile) == ("pnpm", ".", "pnpm-lock.yaml")
    sub = scope_of(d, "py/sub").package_manager
    assert (sub.name, sub.root, sub.lockfile) == ("uv", "py", "py/uv.lock")


def test_dir_with_both_ecosystems_keeps_js_manager_and_other_python(tmp_path):
    d = scan(tmp_path, {"package.json": pkg(), "package-lock.json": "{}", "pyproject.toml": "[tool.uv]\n", "uv.lock": ""})
    scope = scope_of(d, ".")
    assert scope.package_manager.name == "npm"
    assert [(m.ecosystem, m.name) for m in scope.other_managers] == [("python", "uv")]
    assert set(scope.languages) == {"javascript", "python"}


@pytest.mark.parametrize(("files", "tool", "config"), [
    ({"package.json": pkg(), "eslint.config.js": "export default []\n"}, "eslint", "eslint.config.js"),
    ({"package.json": pkg(), "eslint.config.mts": ""}, "eslint", "eslint.config.mts"),
    ({"package.json": pkg(), ".eslintrc.json": "{}"}, "eslint", ".eslintrc.json"),
    ({"package.json": pkg(), ".eslintrc": "{}"}, "eslint", ".eslintrc"),
    ({"package.json": pkg(eslintConfig={"rules": {}})}, "eslint", "package.json"),
    ({"package.json": pkg(), "biome.jsonc": "{ // c\n}"}, "biome", "biome.jsonc"),
    ({"package.json": pkg(prettier={"semi": False})}, "prettier", "package.json"),
    ({"package.json": pkg(), ".prettierrc.yaml": "semi: false\n"}, "prettier", ".prettierrc.yaml"),
    ({"package.json": pkg(), "prettier.config.mjs": ""}, "prettier", "prettier.config.mjs"),
    ({"package.json": pkg(), ".oxlintrc.jsonc": "{}"}, "oxlint", ".oxlintrc.jsonc"),
    ({"package.json": pkg(), "oxlint.config.ts": "export default {}\n"}, "oxlint", "oxlint.config.ts"),
    ({"package.json": pkg(), ".oxfmtrc.json": "{}"}, "oxfmt", ".oxfmtrc.json"),
    ({"package.json": pkg(), "dprint.json": "{}"}, "dprint", "dprint.json"),
    ({"package.json": pkg(), "tslint.json": "{}"}, "tslint", "tslint.json"),
    ({"pyproject.toml": "[tool.ruff]\nline-length = 100\n"}, "ruff", "pyproject.toml"),
    ({"pyproject.toml": "[project]\nname='x'\n", "ruff.toml": ""}, "ruff", "ruff.toml"),
    ({"pyproject.toml": "[project]\nname='x'\n", ".ruff.toml": ""}, "ruff", ".ruff.toml"),
    ({"pyproject.toml": "[tool.black]\n"}, "black", "pyproject.toml"),
    ({"setup.cfg": "[flake8]\nmax-line-length = 100\n"}, "flake8", "setup.cfg"),
    ({"setup.py": "", ".flake8": ""}, "flake8", ".flake8"),
    ({"setup.py": "", "tox.ini": "[flake8]\nignore = E1\n"}, "flake8", "tox.ini"),
    ({"pyproject.toml": "[tool.pylint.main]\njobs = 1\n"}, "pylint", "pyproject.toml"),
    ({"setup.py": "", ".pylintrc": ""}, "pylint", ".pylintrc"),
    ({"setup.py": "", "mypy.ini": ""}, "mypy", "mypy.ini"),
    ({"setup.cfg": "[mypy]\nstrict = true\n"}, "mypy", "setup.cfg"),
    ({"pyproject.toml": "[tool.mypy]\nstrict = true\n"}, "mypy", "pyproject.toml"),
    ({"pyproject.toml": "[tool.pyright]\n"}, "pyright", "pyproject.toml"),
    ({"setup.py": "", "pyrightconfig.json": "{}"}, "pyright", "pyrightconfig.json"),
])
def test_existing_configs_are_detected(tmp_path, files, tool, config):
    scope = scope_of(scan(tmp_path, files), ".")
    rows = tool_rows(scope, tool)
    assert rows and all(config in row.config_files for row in rows)


def test_biome_and_ruff_report_both_roles(tmp_path):
    d = scan(tmp_path, {"package.json": pkg(devDependencies={"@biomejs/biome": "1.9.0"}), "ruff.toml": ""})
    scope = scope_of(d, ".")
    assert {r.role for r in tool_rows(scope, "biome")} == {"lint", "format"}
    assert tool_row(scope, "biome", "lint").declared
    assert {r.role for r in tool_rows(scope, "ruff")} == {"lint", "format"}


def test_dependency_declarations(tmp_path):
    d = scan(tmp_path, {
        "package.json": pkg(devDependencies={"oxlint": "1", "oxlint-tsgolint": "1", "prettier": "3"}, peerDependencies={"eslint": "9"}),
        "pyproject.toml": (
            '[project]\nname = "x"\ndependencies = ["Black[jupyter]>=24", "requests"]\n'
            '[project.optional-dependencies]\nlint = ["flake8 ; python_version > \'3.8\'"]\n'
            '[dependency-groups]\ndev = ["mypy==1.10", {include-group = "lint"}]\n'
            '[tool.poetry.group.dev.dependencies]\npylint = "^3"\n'
            '[tool.pdm.dev-dependencies]\ntest = ["pyright"]\n'
            '[tool.uv]\ndev-dependencies = ["ruff>=0.5"]\n'
        ),
        "requirements-dev.txt": "# c\n-r base.txt\n-e .\npytest==8 # note\nruff[extra]>=0.5\n",
    })
    scope = scope_of(d, ".")
    for tool in ("oxlint", "oxlint-tsgolint", "prettier", "eslint", "black", "flake8", "mypy", "pylint", "pyright", "ruff"):
        assert tool_rows(scope, tool), tool
        assert all(row.declared for row in tool_rows(scope, tool)), tool
    assert tool_row(scope, "oxlint-tsgolint", "lint").declared


def test_requirements_only_declares_tool(tmp_path):
    scope = scope_of(scan(tmp_path, {"requirements.txt": "black==24.1\n--index-url https://x\n"}), ".")
    assert tool_row(scope, "black", "format").declared


def test_oxlint_type_check_option(tmp_path):
    d = scan(tmp_path, {"package.json": pkg(), ".oxlintrc.jsonc": '{ // x\n "options": { "typeCheck": true, },\n}'})
    assert tool_row(scope_of(d, "."), "oxlint", "lint").options == {"type_check": "true"}
    plain = scan(tmp_path, {"package.json": pkg(), ".oxlintrc.json": '{"options": {"typeAware": true}}'}, "plain")
    assert tool_row(scope_of(plain, "."), "oxlint", "lint").options == {}


def test_script_tokens_feed_tools_and_allow_list_feeds_scripts(tmp_path):
    d = scan(tmp_path, {"package.json": pkg(scripts={
        "lint": "eslint . && prettier --check .",
        "release": "standard-version",
        "ci:eslint": "eslint --fix src",
        "build": "tsc -p .",
        "check:format": "prettier --write .",
        "lint:fix": "eslint --fix .",
    })})
    scope = scope_of(d, ".")
    assert tool_row(scope, "eslint", "lint").scripts == ("ci:eslint", "lint", "lint:fix")
    assert tool_row(scope, "prettier", "format").scripts == ("check:format", "lint")
    assert not tool_rows(scope, "standard")
    assert not tool_rows(scope, "tsc")  # no tsconfig, typescript undeclared
    assert [s.name for s in scope.scripts] == ["check:format", "lint"]
    script = scope.scripts[1]
    assert (script.runner, script.roles, script.writes_files) == ("package.json", ("lint",), False)


def test_tsc_row_needs_tsconfig_and_evidence(tmp_path):
    d = scan(tmp_path, {"package.json": pkg(scripts={"typecheck": "tsc --noEmit"}), "tsconfig.json": "{}"})
    row = tool_row(scope_of(d, "."), "tsc", "typecheck")
    assert row.scripts == ("typecheck",) and row.config_files == ("tsconfig.json",)
    d = scan(tmp_path, {"package.json": pkg(devDependencies={"typescript": "5"}), "tsconfig.json": "{}"}, "declared")
    assert tool_row(scope_of(d, "."), "tsc", "typecheck").declared


def test_makefile_targets_become_scripts(tmp_path):
    d = scan(tmp_path, {"pyproject.toml": "[project]\nname='x'\n", "Makefile": "lint:\n\truff check .\n\nbuild:\n\ttrue\nlint-fix:\n\truff --fix .\n"})
    scope = scope_of(d, ".")
    assert [(s.runner, s.name) for s in scope.scripts] == [("Makefile", "lint")]
    assert tool_row(scope, "ruff", "lint") is not None


def test_typescript_info_reads_installed_version_from_json_only(tmp_path):
    root = make_repo(tmp_path, {
        "package.json": pkg(devDependencies={"typescript": "~5.4.2"}),
        "tsconfig.json": "{}", "tsconfig.build.json": "{}", "docs/tsconfig.json": "{}",
        "node_modules/typescript/package.json": pkg(name="typescript", version="5.4.5"),
        ".gitignore": "node_modules\n",
    })
    info = scope_of(discover(root), ".").typescript
    assert (info.declared_spec, info.installed_version, info.major) == ("~5.4.2", "5.4.5", 5)
    assert info.tsconfigs == ("tsconfig.build.json", "tsconfig.json")


def test_typescript_info_falls_back_to_manager_root_and_ignores_symlinked_dirs(tmp_path):
    root = make_repo(tmp_path, {
        "package.json": pkg(), "pnpm-lock.yaml": "", "packages/a/package.json": pkg(devDependencies={"typescript": "^7.0.0"}),
        "node_modules/typescript/package.json": pkg(version="7.0.1"), ".gitignore": "node_modules\n",
    })
    assert scope_of(discover(root), "packages/a").typescript.major == 7
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "typescript").mkdir(parents=True)
    (elsewhere / "typescript" / "package.json").write_text(pkg(version="9.9.9"), encoding="utf-8")
    other = make_repo(tmp_path, {"package.json": pkg(devDependencies={"typescript": "5"}), ".gitignore": "node_modules\n"}, "other")
    (other / "node_modules").symlink_to(elsewhere)
    info = scope_of(discover(other), ".").typescript
    assert info.installed_version is None and info.major is None


def test_no_typescript_info_for_plain_js(tmp_path):
    assert scope_of(scan(tmp_path, {"package.json": pkg()}), ".").typescript is None


def test_languages_follow_evidence(tmp_path):
    d = scan(tmp_path, {
        "js/package.json": pkg(), "js/a.js": "",
        "mixed/package.json": pkg(devDependencies={"typescript": "5"}), "mixed/a.js": "", "mixed/b.ts": "",
        "tsonly/package.json": pkg(), "tsonly/x.mts": "",
        "tsconfig/package.json": pkg(), "tsconfig/tsconfig.json": "{}",
        "plain/package.json": pkg(),
    })
    assert scope_of(d, "js").languages == ("javascript",)
    assert scope_of(d, "mixed").languages == ("javascript", "typescript")
    assert scope_of(d, "tsonly").languages == ("typescript",)
    assert scope_of(d, "tsconfig").languages == ("typescript",)
    assert scope_of(d, "plain").languages == ("javascript",)


def test_shell_detection_by_extension_and_shebang(tmp_path):
    root = make_repo(tmp_path, {
        "a.sh": "", "lib/b.bash": "", "bin/tool": "#!/usr/bin/env bash\necho\n", "bin/dash": "#!/bin/dash\n", "bin/ksh": "#!/bin/ksh -e\n",
        "bin/py": "#!/usr/bin/env python3\n", "bin/plain": "hello\n", "bin/zsh": "#!/bin/zsh\n", "LICENSE": "MIT\n",
    })
    d = discover(root)
    assert d.source_files["shell"] == ("a.sh", "bin/dash", "bin/ksh", "bin/tool", "lib/b.bash")
    assert scope_of(d, ".").files["shell"] == d.source_files["shell"]


def test_workflows_only_at_repo_root(tmp_path):
    d = scan(tmp_path, {
        ".github/workflows/ci.yml": "", ".github/workflows/rel.yaml": "", ".github/workflows/readme.md": "",
        "sub/.github/workflows/x.yml": "", "package.json": pkg(),
    })
    assert scope_of(d, ".").files["github-workflows"] == (".github/workflows/ci.yml", ".github/workflows/rel.yaml")


def test_unknown_languages_and_deno(tmp_path):
    d = scan(tmp_path, {
        "a.kt": "", "b.kt": "", "c.kts": "", "d.kt": "", "x.java": "", "r.rb": "", "s.sql": "", "doc.md": "", "data.json": "{}",
        "conf.yaml": "", "logo.svg": "", "notes.txt": "", "deno.json": "{}", "mod/deno.jsonc": "{}", "package.json": pkg(),
    })
    langs = {u.language: u for u in d.unknown}
    assert set(langs) == {"kotlin", "java", "ruby", "sql", "deno"}
    assert langs["kotlin"].files == 4 and len(langs["kotlin"].examples) == 3
    assert langs["deno"].files == 2
    assert [u.language for u in d.unknown] == sorted(langs)


def test_orphan_python_script_in_js_repo_adds_python_without_manager(tmp_path):
    d = scan(tmp_path, {"package.json": pkg(), "package-lock.json": "{}", "scripts/x.py": "print(1)\n"})
    scope = scope_of(d, ".")
    assert scope.languages == ("javascript", "python")
    assert scope.package_manager.ecosystem == "js" and scope.other_managers == ()
    evidence = [e for e in scope.evidence if e.kind == "source"]
    assert [(e.path, e.detail != "") for e in evidence] == [("scripts/x.py", True)]
    assert d.source_files["python"] == ("scripts/x.py",)


def test_lone_python_file_gets_synthetic_scope(tmp_path):
    d = scan(tmp_path, {"tool.py": "print(1)\n"})
    assert [s.rel for s in d.scopes] == ["."]
    scope = d.scopes[0]
    assert scope.languages == ("python",) and scope.package_manager is None
    assert d.source_files == {"python": ("tool.py",)}


def test_manifestless_js_gets_synthetic_scope(tmp_path):
    d = scan(tmp_path, {"src/a.js": "", "src/b.tsx": ""})
    assert d.scopes[0].rel == "." and d.scopes[0].languages == ("javascript", "typescript")
    assert d.scopes[0].package_manager is None
    assert d.source_files["typescript"] == ("src/b.tsx",)


def test_orphan_go_and_rust_are_unknown_not_scoped(tmp_path):
    d = scan(tmp_path, {"package.json": pkg(), "main.go": "package main\n", "a/b.go": "", "x/lib.rs": ""})
    langs = {u.language: u.files for u in d.unknown}
    assert langs == {"go": 2, "rust": 1}
    assert scope_of(d, ".").languages == ("javascript",)
    assert d.source_files["go"] == ("a/b.go", "main.go") and d.source_files["rust"] == ("x/lib.rs",)


def test_nested_non_workspace_cargo_dir_is_a_scope_but_members_are_not(tmp_path):
    d = scan(tmp_path, {"solo/Cargo.toml": '[package]\nname = "s"\n', "ws/Cargo.toml": "[workspace]\nmembers = ['m']\n", "ws/m/Cargo.toml": "[package]\nname='m'\n"})
    assert [s.rel for s in d.scopes] == ["solo", "ws"]


def test_requirements_only_scope(tmp_path):
    scope = scope_of(scan(tmp_path, {"requirements.txt": "flask\n", "app.py": ""}), ".")
    assert scope.package_manager.name == "pip" and scope.package_manager.lockfile is None
    assert scope.languages == ("python",)


@pytest.mark.parametrize(("files", "frameworks"), [
    ({"package.json": pkg(dependencies={"react": "18", "react-dom": "18"})}, ("react",)),
    ({"package.json": pkg(), "src/App.tsx": ""}, ("react",)),
    ({"package.json": pkg(), "src/App.jsx": ""}, ("react",)),
    ({"package.json": pkg(dependencies={"vue": "3"})}, ("vue",)),
    ({"package.json": pkg(), "src/A.vue": ""}, ("vue",)),
    ({"package.json": pkg(devDependencies={"next": "14"})}, ("next", "react")),
    ({"package.json": pkg(dependencies={"nuxt": "3"})}, ("nuxt", "vue")),
    ({"package.json": pkg(dependencies={"@sveltejs/kit": "2"})}, ("svelte",)),
    ({"package.json": pkg(), "src/A.svelte": ""}, ("svelte",)),
    ({"package.json": pkg(devDependencies={"typescript": "5"}), "src/a.ts": ""}, ()),
])
def test_frameworks(tmp_path, files, frameworks):
    assert scope_of(scan(tmp_path, files), ".").frameworks == frameworks


def test_monorepo_ancestor_config_is_reported_once_at_the_root(tmp_path):
    d = scan(tmp_path, {
        "package.json": pkg(devDependencies={"eslint": "9"}), "eslint.config.js": "export default []\n",
        "packages/a/package.json": pkg(devDependencies={"eslint": "9"}),
        "packages/b/package.json": pkg(devDependencies={"eslint": "9"}),
        "packages/c/package.json": pkg(scripts={"lint": "eslint ."}), "packages/c/eslint.config.js": "",
    })
    rows = [(s.rel, t.config_files) for s in d.scopes for t in s.tools if t.tool == "eslint"]
    assert rows == [(".", ("eslint.config.js",)), ("packages/c", ("packages/c/eslint.config.js",))]


def test_malformed_content_becomes_problems_and_scopes_survive(tmp_path):
    d = scan(tmp_path, {
        "bad/package.json": "{nope", "badpy/pyproject.toml": "[tool\nx",
        "cfg/package.json": pkg(), "cfg/.oxlintrc.json": "{ broken", "cfg/biome.json": "{",
    })
    kinds = {(p.path, p.kind) for p in d.problems}
    assert ("bad/package.json", "malformed-manifest") in kinds
    assert ("badpy/pyproject.toml", "malformed-manifest") in kinds
    assert ("cfg/.oxlintrc.json", "malformed-config") in kinds
    assert {s.rel for s in d.scopes} == {"bad", "badpy", "cfg"}
    assert tool_row(scope_of(d, "cfg"), "oxlint", "lint").config_files == ("cfg/.oxlintrc.json",)


def test_discovery_never_executes_repo_code(tmp_path, monkeypatch):
    shim = tmp_path / "shim"
    shim.mkdir()
    log = tmp_path / "shim.log"
    marker = tmp_path / "marker"
    for name in ("node", "bun", "npx", "python3", "deno"):
        path = shim / name
        path.write_text(f"#!/bin/sh\necho {name} >> {log}\n", encoding="utf-8")
        path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")
    root = make_repo(tmp_path, {
        "package.json": pkg(scripts={"lint": f"touch {marker}"}),
        "eslint.config.js": f"require('fs').writeFileSync({str(marker)!r}, 'x')\n",
        "oxlint.config.ts": f"import fs from 'fs'; fs.writeFileSync({str(marker)!r}, 'x')\n",
        "setup.py": f"open({str(marker)!r}, 'w').write('x')\n",
        "prettier.config.js": "", "Makefile": f"lint:\n\ttouch {marker}\n",
    })
    discover(root)
    assert not log.exists() and not marker.exists()


def test_symlinked_manifest_is_not_read_and_escape_is_a_problem(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text(pkg(name="evil"), encoding="utf-8")
    root = make_repo(tmp_path, {"real/package.json": pkg(), "inner/target.json": pkg(), "src/a.js": ""})
    (root / "escape").mkdir()
    (root / "escape" / "package.json").symlink_to(outside)
    (root / "inner" / "package.json").symlink_to("target.json")
    d = discover(root)
    assert any(p.kind == "symlink-escape" and p.path == "escape/package.json" for p in d.problems)
    assert "inner/package.json" in d.skipped_symlinks and "escape/package.json" in d.skipped_symlinks
    assert d.scope("escape") is None and d.scope("inner") is None
    assert not any(p.path == "inner/package.json" for p in d.problems)


def test_symlinked_config_is_not_read(tmp_path):
    outside = tmp_path / "eslint.cfg"
    outside.write_text("{}", encoding="utf-8")
    root = make_repo(tmp_path, {"package.json": pkg()})
    (root / ".eslintrc.json").symlink_to(outside)
    d = discover(root)
    assert not tool_rows(scope_of(d, "."), "eslint")
    assert [p.kind for p in d.problems] == ["symlink-escape"]


def _no_toml_parser(monkeypatch):
    monkeypatch.setattr(lint_discovery_io, "_import_toml_parser", lambda: None)


def test_missing_toml_parser_keeps_tool_facts_but_is_reported_as_a_problem(tmp_path, monkeypatch):
    _no_toml_parser(monkeypatch)
    d = scan(tmp_path, {"pyproject.toml": "[project]\nname='x'\n[tool.ruff.lint]\nselect=['E']\n[tool.uv]\n", "rs/Cargo.toml": "[workspace]\n"})
    assert d.toml_parser is False
    scope = scope_of(d, ".")
    assert tool_row(scope, "ruff", "lint").config_files == ("pyproject.toml",)
    assert scope.package_manager.name == "uv"
    assert scope_of(d, "rs").languages == ("rust",)
    # never silent: one actionable, non-green problem per TOML file that could not be parsed properly
    assert sorted((p.path, p.kind) for p in d.problems) == [("pyproject.toml", "unreadable"), ("rs/Cargo.toml", "unreadable")]
    assert all("pip install tomli" in p.detail and "reinstall rig" in p.detail for p in d.problems)


def test_missing_toml_parser_makes_the_run_incomplete_not_green(tmp_path, monkeypatch):
    from riglib.lint_run_verdict import INCOMPLETE_PROBLEMS

    _no_toml_parser(monkeypatch)
    d = scan(tmp_path, {"pyproject.toml": "[tool.ruff]\nline-length = 100\n"})
    assert d.problems and all(p.kind in INCOMPLETE_PROBLEMS for p in d.problems)


def test_a_present_parser_reports_nothing(tmp_path):
    d = scan(tmp_path, {"pyproject.toml": "[tool.ruff]\nline-length = 100\n"})
    assert d.toml_parser is True and d.problems == ()


def test_importing_discovery_does_not_import_a_toml_parser():
    code = (
        "import sys\n"
        "import riglib.lint_discovery, riglib.linter_environment, riglib.lint_providers\n"
        "loaded = [m for m in ('tomllib', 'tomli') if m in sys.modules]\n"
        "assert not loaded, loaded\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False,
                          cwd=Path(lint_discovery.__file__).resolve().parents[1])
    assert done.returncode == 0, done.stderr


def test_toml_parser_flag_is_true_with_parser(tmp_path):
    assert scan(tmp_path, {"package.json": pkg()}).toml_parser is True


def test_not_a_git_repo_raises_scope_error(tmp_path):
    with pytest.raises(ScopeError):
        discover(tmp_path)


def test_scopes_argument_restricts_enumeration(tmp_path):
    root = make_repo(tmp_path, {"a/package.json": pkg(), "b/pyproject.toml": "[project]\nname='b'\n"})
    assert [s.rel for s in discover(root, scopes=["b"]).scopes] == ["b"]
    assert [s.rel for s in discover(root, scopes=[]).scopes] == ["a", "b"]


def test_discovery_is_deterministic_and_evidence_is_relative(tmp_path):
    root = make_repo(tmp_path, {
        "package.json": pkg(scripts={"lint": "eslint ."}, devDependencies={"eslint": "9"}), "yarn.lock": "", "eslint.config.js": "",
        "py/pyproject.toml": "[tool.ruff]\n", "x.py": "", "run.sh": "#!/bin/sh\n",
    })
    assert discover(root) == discover(root)
    text = repr(discover(root).scopes)
    assert str(root) not in text


def test_parse_jsonc_is_reexported():
    assert parse_jsonc('{"a": 1, // c\n}') == {"a": 1}
    for name in ("Discovery", "ProjectScope", "ConfiguredTool", "TypeScriptInfo", "Evidence", "PackageManager", "UnknownLanguage",
                 "DiscoveryProblem", "ProjectScript", "TOOL_ROLES", "LANG_JS", "TRAIT_GH_WORKFLOWS", "depth", "contains", "join"):
        assert hasattr(lint_discovery, name), name


def test_non_regular_files_are_ignored(tmp_path):
    root = make_repo(tmp_path, {"package.json": pkg()})
    os.mkfifo(root / "fifo.py")
    assert stat.S_ISFIFO(os.lstat(root / "fifo.py").st_mode)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    d = discover(root)
    assert d.source_files.get("python", ()) == ()


def test_orphan_typescript_in_python_scope_adds_typescript_without_js_manager(tmp_path):
    d = scan(tmp_path, {"pyproject.toml": "[project]\nname='x'\n", "tools/gen.ts": "export {}\n"})
    scope = scope_of(d, ".")
    assert scope.languages == ("python", "typescript")
    assert scope.package_manager.ecosystem == "python" and scope.typescript is None
    assert [e.path for e in scope.evidence if e.kind == "source"] == ["tools/gen.ts"]


def test_python_below_a_nested_js_scope_is_covered_by_a_python_ancestor(tmp_path):
    d = scan(tmp_path, {"pyproject.toml": "[project]\nname='x'\n", "web/package.json": pkg(), "web/gen.py": ""})
    assert scope_of(d, "web").languages == ("javascript",)
    assert not [e for s in d.scopes for e in s.evidence if e.kind == "source"]


def test_root_makefile_with_lint_target_is_a_scope_of_its_own(tmp_path):
    d = scan(tmp_path, {"Makefile": "lint:\n\ttrue\n"})
    assert [s.rel for s in d.scopes] == ["."]
    assert d.scopes[0].languages == () and d.scopes[0].scripts[0].name == "lint"
    assert scan(tmp_path, {"Makefile": "build:\n\ttrue\n"}, "other").scopes == ()


def test_ignored_dirs_and_enumerated_files(tmp_path):
    root = make_repo(tmp_path, {
        ".gitignore": ".worktrees/\ndist/\nbuild/\n", "package.json": pkg(), "src/a.js": "",
        ".worktrees/x/biome.json": "{}", "dist/out.js": "", "packages/p/package.json": pkg(), "packages/p/build/gen.js": "",
    })
    d = discover(root)
    assert d.ignored_dirs == (".worktrees", "dist", "packages/p/build")
    assert d.enumerated_files == tuple(sorted(d.enumerated_files)) and len(d.enumerated_files) == d.file_count
    assert "src/a.js" in d.enumerated_files and ".gitignore" in d.enumerated_files
    assert not [f for f in d.enumerated_files if f.startswith((".worktrees", "dist", "packages/p/build"))]
    assert d.scope(".worktrees/x") is None


def test_typescript_version_is_read_through_an_in_repo_pnpm_link(tmp_path):
    root = make_repo(tmp_path, {
        "package.json": pkg(devDependencies={"typescript": "^7.0.0"}), ".gitignore": "node_modules\n",
        "node_modules/.pnpm/typescript@7.0.2/node_modules/typescript/package.json": pkg(version="7.0.2"),
    })
    (root / "node_modules" / "typescript").symlink_to(".pnpm/typescript@7.0.2/node_modules/typescript")
    info = scope_of(discover(root), ".").typescript
    assert (info.installed_version, info.major) == ("7.0.2", 7)


def test_typescript_link_leaving_the_repository_is_not_read(tmp_path):
    outside = tmp_path / "store" / "typescript"
    outside.mkdir(parents=True)
    (outside / "package.json").write_text(pkg(version="9.0.0"), encoding="utf-8")
    root = make_repo(tmp_path, {"package.json": pkg(devDependencies={"typescript": "5"}), ".gitignore": "node_modules\n", "node_modules/x": ""})
    (root / "node_modules" / "typescript").symlink_to(outside)
    assert scope_of(discover(root), ".").typescript.installed_version is None
