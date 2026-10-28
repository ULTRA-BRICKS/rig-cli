"""Catalog scan and core plan resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from riglib.catalog import Catalog, CatalogError
from riglib.errors import UnknownItemError
from riglib.plan import build, resolve_category_target, resolve_category_targets
from tests.catalog_plan_helpers import _cfg


from riglib.plan import build, resolve_category_target, resolve_category_targets


def test_catalog_scan_finds_all_categories(fake_agent_tools):
    cat = Catalog.scan(str(fake_agent_tools))
    assert "shell-timeouts" in cat.names("skills")
    assert "by-type/cli/lazy-imports" in cat.names("skills")
    assert "block-no-verify" in cat.names("agent_hooks")
    assert "codeql" in cat.names("ci")
    assert "secret-scan" in cat.names("ci")
    assert "dispatcher" in cat.names("git_hooks")
    assert "fake-mcp" in cat.names("mcp")


def test_catalog_discovers_every_json_in_a_multi_descriptor_hook_dir(fake_agent_tools):
    """A hook dir shipping multiple ``*.json`` descriptors registers ALL of them (#184 gap).

    ``dual-guard`` ships both ``dual-guard.pre-bash.json`` and ``dual-guard.pre-write.json``
    (mirroring the real ``orchestrator-stays-thin``). The scanner used to take only the first
    sorted descriptor, silently dropping the ``pre-write`` guard — enforcement never fired.
    """
    cat = Catalog.scan(str(fake_agent_tools))
    item = cat.get("agent_hooks", "dual-guard")
    assert item is not None
    assert set(item.descriptors) == {"dual-guard.pre-bash.json", "dual-guard.pre-write.json"}


def test_plan_installs_every_descriptor_of_a_multi_descriptor_hook(fake_agent_tools, tmp_path, monkeypatch):
    """``rig apply`` must emit an install action for EVERY descriptor in a multi-json hook dir."""
    import os

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {
            "skills": {"enabled": False},
            "agent_hooks": {"all": True},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "harness": {"kind": "claude-code"},
        },
        tmp_path,
    )

    plan = build(cfg, cat, project_type="unknown")

    dual = [
        a
        for a in plan.actions
        if a.kind == "install_agent_hook" and a.item == "dual-guard"
    ]
    target = Path(os.path.expanduser("~/.claude/hooks"))
    assert {a.options.get("descriptor") for a in dual} == {
        "dual-guard.pre-bash.json",
        "dual-guard.pre-write.json",
    }
    assert {a.target for a in dual} == {target}


def test_plan_multi_descriptor_fans_out_across_targets_without_false_collision(
    fake_agent_tools, tmp_path, monkeypatch
):
    """N descriptors × M harness targets → N×M actions; same basename in DIFFERENT target
    dirs must NOT trip the collision guard (the collision key is scoped by full target path).
    """
    import os

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {
            "harness": {"kind": "claude-code", "kinds": ["codex"]},
            "skills": {"enabled": False},
            "agent_hooks": {"all": True},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
        },
        tmp_path,
    )

    plan = build(cfg, cat, project_type="unknown")

    dual = [a for a in plan.actions if a.kind == "install_agent_hook" and a.item == "dual-guard"]
    claude = Path(os.path.expanduser("~/.claude/hooks"))
    codex = Path(os.path.expanduser("~/.codex/hooks"))
    assert {(a.target, a.options["descriptor"]) for a in dual} == {
        (claude, "dual-guard.pre-bash.json"),
        (claude, "dual-guard.pre-write.json"),
        (codex, "dual-guard.pre-bash.json"),
        (codex, "dual-guard.pre-write.json"),
    }


def test_plan_fails_closed_on_descriptor_basename_collision(fake_agent_tools, tmp_path):
    """Two hooks shipping the same descriptor basename into one target dir must fail closed.

    Descriptors from different hooks share one flat destination namespace. If two collide on
    basename, one would silently clobber the other (a lost guard, drift never clean). The plan
    must refuse rather than build a self-conflicting install.
    """
    from riglib.catalog import Catalog as _Cat
    from riglib.catalog import Item
    from riglib.plan import PlanError

    src = Catalog.scan(str(fake_agent_tools)).source
    colliding = [
        Item("guard-a", "agent_hooks", "", "", src / "a", descriptors=("shared.pre-write.json",)),
        Item("guard-b", "agent_hooks", "", "", src / "b", descriptors=("shared.pre-write.json",)),
    ]
    cat = _Cat(source=src, items=colliding)
    cfg = _cfg(
        {
            "skills": {"enabled": False},
            "agent_hooks": {"all": True, "target": str(tmp_path / "hooks")},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
        },
        tmp_path,
    )
    with pytest.raises(PlanError, match="descriptor collision"):
        build(cfg, cat, project_type="unknown")


def test_fake_catalog_never_fabricates_a_removed_slot(fake_agent_tools):
    """Anti-masking guard: no item the fake catalog fabricates may be a ``_REMOVED_SLOTS`` entry.

    The fake catalog must MIRROR catalog reality. A fabricated slot that rig classifies as
    removed (the old ``mcp/review``) resolves VALID here but errors (exit 4) against the real
    catalog — the divergence that let a dead ``mcp.items.review`` ship green (issue #61). Every
    drift/plan test that needs a generic mcp item leans on this fixture, so a regression here
    would silently re-mask the whole class. This catches it in the unit suite, not only the
    real-catalog e2e.
    """
    from riglib.errors import removed_slot

    cat = Catalog.scan(str(fake_agent_tools))
    offenders = [
        f"{i.category}/{i.name}"
        for i in cat.items
        if removed_slot(i.category, i.name) is not None
    ]
    assert not offenders, (
        f"fake catalog fabricates removed slot(s) {offenders} — a fixture must never use a name "
        f"rig classifies as removed (see riglib.errors._REMOVED_SLOTS); pick a synthetic name "
        f"instead, else dead-slot bugs only surface against the real catalog"
    )


def test_catalog_situational_default_off(fake_agent_tools):
    cat = Catalog.scan(str(fake_agent_tools))
    push = cat.get("skills", "push-regularly")
    assert push is not None and push.default_enabled is False


def test_resolve_source_rejects_non_checkout(tmp_path):
    with pytest.raises(CatalogError):
        Catalog.scan(str(tmp_path))


def test_plan_universal_optout(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"skills": {"universal": {"all": True, "disable": ["naming"]}, "by_type": {"enable": []}}},
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    skills = {a.item for a in plan.actions if a.category == "skills"}
    assert "shell-timeouts" in skills
    assert "naming" not in skills  # disabled
    # situational stays off under all:true? all:true forces on — but push-regularly is universal
    assert "push-regularly" in skills


def test_plan_by_type_pulled_by_project_type(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"skills": {"by_type": {}}}, tmp_path)
    plan = build(cfg, cat, project_type="cli")
    skills = {a.item for a in plan.actions if a.category == "skills"}
    assert "by-type/cli/lazy-imports" in skills
    assert "by-type/backend/atomic-tx" not in skills  # different type


def test_plan_by_type_explicit_enable_overrides_type(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"skills": {"by_type": {"enable": ["backend"]}}}, tmp_path)
    plan = build(cfg, cat, project_type="frontend")
    skills = {a.item for a in plan.actions if a.category == "skills"}
    assert "by-type/backend/atomic-tx" in skills
    assert "by-type/frontend/tokens" not in skills


# ── stack presets: by-stack catalog scan + hierarchical plan selection ──────────────────
def test_catalog_scans_by_stack_skills(fake_agent_tools):
    cat = Catalog.scan(str(fake_agent_tools))
    names = cat.names("skills")
    assert "by-stack/mobile/swift/swiftui/swiftui-mvvm" in names
    assert "by-stack/mobile/swift/swift-concurrency" in names  # lang-level
    assert "by-stack/frontend/ts/react/vercel-react-patterns" in names
    # an l1-only SKILL.md (depth < 3, no lang) is IGNORED by the scanner
    assert not any(n.startswith("by-stack/mobile/ignored-l1-only") for n in names)
    assert not any("ignored-l1-only" in n for n in names)


def test_by_stack_items_carry_stack_meta_and_off_by_default(fake_agent_tools):
    cat = Catalog.scan(str(fake_agent_tools))
    item = cat.get("skills", "by-stack/mobile/swift/swiftui/swiftui-mvvm")
    assert item is not None
    assert item.default_enabled is False
    assert item.meta["stack"] == "mobile/swift/swiftui"
    assert item.group == "by-stack/mobile/swift/swiftui"


def test_plan_by_stack_selected_by_declared_stack_hierarchically(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"stack": "mobile/swift/swiftui", "skills": {"by_type": {}}}, tmp_path)
    plan = build(cfg, cat, project_type="unknown")
    skills = {a.item for a in plan.actions if a.category == "skills"}
    # framework-level AND the inherited lang-level skill are both pulled in
    assert "by-stack/mobile/swift/swiftui/swiftui-mvvm" in skills
    assert "by-stack/mobile/swift/swift-concurrency" in skills
    # a sibling stack's skills are NOT provisioned (no react in a swift repo)
    assert "by-stack/frontend/ts/react/vercel-react-patterns" not in skills
    assert "by-stack/frontend/ts/ts-strictness" not in skills


def test_plan_by_stack_lang_only_stack_excludes_framework(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"stack": "mobile/swift", "skills": {"by_type": {}}}, tmp_path)
    plan = build(cfg, cat, project_type="unknown")
    skills = {a.item for a in plan.actions if a.category == "skills"}
    assert "by-stack/mobile/swift/swift-concurrency" in skills  # lang-level matches
    # a deeper (framework) skill than the declared stack must NOT be pulled in
    assert "by-stack/mobile/swift/swiftui/swiftui-mvvm" not in skills


def test_plan_no_stack_selects_no_by_stack_skills(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"skills": {"by_type": {}}}, tmp_path)
    plan = build(cfg, cat, project_type="unknown")
    skills = {a.item for a in plan.actions if a.category == "skills"}
    assert not any(s.startswith("by-stack/") for s in skills)


# ── stack presets: auto-detection fallback (mirrors by-type's project_type auto-pull) ───
# The caller resolves the guess via `detect.detect_environment(...).stack_preset` (computed the
# same way/place as `env.project_type`) and hands it to `build(..., stack_preset=...)` — `build()`/
# `_skills_enabled` never touch the filesystem themselves, exactly mirroring by-type's design.
def test_plan_by_stack_auto_detected_from_swift_signature(fake_agent_tools, tmp_path):
    """No explicit ``stack:`` — a ``Package.swift`` signature auto-pulls the matching
    by-stack bundle, same as an undeclared ``project_type`` auto-pulls its by-type bundle."""
    from riglib.detect import detect_environment

    (tmp_path / "Package.swift").write_text("// swift-tools-version:5.9\n", encoding="utf-8")
    env = detect_environment(tmp_path)
    assert env.stack_preset == "mobile/swift"
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"skills": {"by_type": {}}}, tmp_path)
    plan = build(cfg, cat, project_type="unknown", stack_preset=env.stack_preset)
    skills = {a.item for a in plan.actions if a.category == "skills"}
    assert "by-stack/mobile/swift/swift-concurrency" in skills  # lang-level auto-detected
    # Swift detection (_detect_swift_stack) only ever guesses to lang level, no framework
    # heuristic — a deeper framework skill must NOT be pulled in from a bare Package.swift guess.
    # (Node detection DOES guess to framework level — see the react test below.)
    assert "by-stack/mobile/swift/swiftui/swiftui-mvvm" not in skills
    assert "by-stack/frontend/ts/react/vercel-react-patterns" not in skills


def test_plan_by_stack_auto_detected_from_node_framework_signature(fake_agent_tools, tmp_path):
    """Node detection (unlike Swift's) guesses all the way to FRAMEWORK level: a
    package.json + react + tsconfig.json signature auto-pulls both the framework-level item
    AND the inherited lang-level one, with no ``stack:`` declared."""
    import json

    from riglib.detect import detect_environment

    (tmp_path / "package.json").write_text(
        json.dumps({"name": "x", "dependencies": {"react": "^18"}}), encoding="utf-8"
    )
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    env = detect_environment(tmp_path)
    assert env.stack_preset == "frontend/ts/react"
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"skills": {"by_type": {}}}, tmp_path)
    plan = build(cfg, cat, project_type="unknown", stack_preset=env.stack_preset)
    skills = {a.item for a in plan.actions if a.category == "skills"}
    assert "by-stack/frontend/ts/react/vercel-react-patterns" in skills  # framework-level, exact
    assert "by-stack/frontend/ts/ts-strictness" in skills  # inherited lang-level
    assert "by-stack/mobile/swift/swift-concurrency" not in skills


def test_plan_by_stack_explicit_stack_overrides_detection(fake_agent_tools, tmp_path):
    """An explicit ``stack:`` config value wins even when the repo also carries a competing
    signature file — detection is only a fallback guess, never an override."""
    from riglib.detect import detect_environment

    (tmp_path / "Package.swift").write_text("// swift-tools-version:5.9\n", encoding="utf-8")
    env = detect_environment(tmp_path)
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"stack": "frontend/ts", "skills": {"by_type": {}}}, tmp_path)
    plan = build(cfg, cat, project_type="unknown", stack_preset=env.stack_preset)
    skills = {a.item for a in plan.actions if a.category == "skills"}
    assert "by-stack/frontend/ts/ts-strictness" in skills  # explicit stack, still selected
    assert "by-stack/mobile/swift/swift-concurrency" not in skills  # NOT auto-detected over it


def test_plan_by_stack_python_signature_detected_but_isolated_from_mobile(fake_agent_tools, tmp_path):
    """A plain Python repo (a bare ``pyproject.toml``, no Swift/Xcode/mobile markers — mirroring
    THIS actual rig-cli repo) is correctly detected as ``backend/python`` (NOT ``unknown``) and
    auto-pulls its own by-stack bundle, but never leaks mobile/frontend skills into it."""
    from riglib.detect import detect_environment

    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    env = detect_environment(tmp_path)
    assert env.stack_preset == "backend/python"
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"skills": {"by_type": {}}}, tmp_path)
    plan = build(cfg, cat, project_type="unknown", stack_preset=env.stack_preset)
    skills = {a.item for a in plan.actions if a.category == "skills"}
    assert "by-stack/backend/python/python-typing" in skills  # correctly detected + selected
    assert "by-stack/mobile/swift/swift-concurrency" not in skills
    assert "by-stack/frontend/ts/ts-strictness" not in skills


def test_plan_by_stack_build_ignores_filesystem_when_stack_preset_kwarg_omitted(fake_agent_tools, tmp_path):
    """``build()`` never probes the filesystem itself — a signature file on disk has NO effect
    unless the caller explicitly resolved and passed ``stack_preset=``. This is what keeps a
    non-repo scope (e.g. config-web's Global tab, anchored at $HOME) from auto-pulling by-stack
    skills from a stray file merely because nobody bothered to pass a guess in."""
    (tmp_path / "Package.swift").write_text("// swift-tools-version:5.9\n", encoding="utf-8")
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"skills": {"by_type": {}}}, tmp_path)
    plan = build(cfg, cat, project_type="unknown")  # no stack_preset= kwarg at all
    skills = {a.item for a in plan.actions if a.category == "skills"}
    assert not any(s.startswith("by-stack/") for s in skills)


def test_plan_by_stack_disable_beats_a_detected_stack(fake_agent_tools, tmp_path):
    """``by_stack.disable`` drops a skill selected via the DETECTED (not declared) stack too —
    the override axis is shared between the declared and auto-detected paths."""
    from riglib.detect import detect_environment

    (tmp_path / "Package.swift").write_text("// swift-tools-version:5.9\n", encoding="utf-8")
    env = detect_environment(tmp_path)
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {
            "skills": {
                "by_stack": {"disable": ["by-stack/mobile/swift/swift-concurrency"]},
                "by_type": {},
            }
        },
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown", stack_preset=env.stack_preset)
    skills = {a.item for a in plan.actions if a.category == "skills"}
    assert "by-stack/mobile/swift/swift-concurrency" not in skills  # disabled despite detection


def test_plan_by_stack_forced_item_wins_even_when_nothing_detected(fake_agent_tools, tmp_path):
    """``by_stack.items.<name>.enabled: true`` forces a skill in even when NEITHER a declared
    stack NOR detection produced anything (``stack_preset`` param falsy) — the escape-hatch override
    must not depend on there being a matching stack at all."""
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {
            "skills": {
                "by_stack": {"items": {"by-stack/mobile/swift/swift-concurrency": {"enabled": True}}},
                "by_type": {},
            }
        },
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown", stack_preset=None)
    skills = {a.item for a in plan.actions if a.category == "skills"}
    assert "by-stack/mobile/swift/swift-concurrency" in skills  # forced in despite no stack at all


def test_plan_by_stack_disable_drops_a_selected_skill(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {
            "stack": "mobile/swift/swiftui",
            "skills": {"by_stack": {"disable": ["by-stack/mobile/swift/swift-concurrency"]}, "by_type": {}},
        },
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    skills = {a.item for a in plan.actions if a.category == "skills"}
    assert "by-stack/mobile/swift/swift-concurrency" not in skills  # disabled
    assert "by-stack/mobile/swift/swiftui/swiftui-mvvm" in skills  # still selected


def test_plan_by_stack_items_force_add_off_stack_skill(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {
            "stack": "mobile/swift/swiftui",
            "skills": {
                "by_stack": {"items": {"by-stack/frontend/ts/react/vercel-react-patterns": {"enabled": True}}},
                "by_type": {},
            },
        },
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    skills = {a.item for a in plan.actions if a.category == "skills"}
    assert "by-stack/frontend/ts/react/vercel-react-patterns" in skills  # forced in


def test_plan_by_stack_unknown_item_name_fails_closed(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"stack": "mobile/swift", "skills": {"by_stack": {"disable": ["by-stack/nope/x/y"]}}},
        tmp_path,
    )
    with pytest.raises(UnknownItemError):
        build(cfg, cat, project_type="unknown")


def test_plan_ci_export_only_writes_nothing(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"skills": {"enabled": False}, "ci": {"target": "export-only", "items": {"codeql": {"enabled": True}}}},
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    assert not [a for a in plan.actions if a.category == "ci"]
    assert any("export-only" in n for n in plan.notes)


def test_plan_codeql_variant_selected(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"skills": {"enabled": False}, "ci": {"items": {"codeql": {"enabled": True, "variant": "selfgate"}}}},
        tmp_path,
    )
    plan = build(cfg, cat, project_type="unknown")
    codeql = next(a for a in plan.actions if a.item == "codeql")
    assert codeql.options["variant"] == "selfgate"


def test_plan_github_actions_provisioned_before_ghas(fake_agent_tools, tmp_path):
    """Enable-Actions must be ordered BEFORE GHAS in plan.actions (runner runs them in sequence).

    CodeQL default-setup (provisioned by the GHAS action) requires GitHub Actions to be enabled,
    so the actions action must run first or a brand-new repo's first apply hard-fails CodeQL and
    needs a second apply to converge. This pins the order by ACTION INDEX (what the runner actually
    executes), so a future re-sort/regroup of the _build_github_* calls that reintroduces the bug
    fails here instead of only surfacing on a fresh repo. See riglib/plan.py (github build order)."""
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"skills": {"enabled": False}, "github": {"enabled": True}}, tmp_path)
    plan = build(cfg, cat, project_type="unknown")
    kinds = [a.kind for a in plan.actions]
    assert "provision_github_actions" in kinds, "github.actions provisioning not planned"
    assert "provision_github_ghas" in kinds, "github.ghas provisioning not planned"
    assert kinds.index("provision_github_actions") < kinds.index("provision_github_ghas"), (
        "enable-Actions must be planned before GHAS (CodeQL default-setup needs Actions enabled)"
    )


def test_plan_unknown_universal_skill_fails_closed(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"skills": {"universal": {"disable": ["shell-timeout"]}}}, tmp_path)  # typo
    with pytest.raises(UnknownItemError) as exc:
        build(cfg, cat, project_type="unknown")
    # error-system v2: did-you-mean suggests the nearest valid skill
    assert "shell-timeouts" in exc.value.fix


def test_plan_unknown_agent_hook_fails_closed(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"skills": {"enabled": False}, "agent_hooks": {"items": {"block-no-verifyy": {"enabled": True}}}},
        tmp_path,
    )
    with pytest.raises(UnknownItemError) as exc:
        build(cfg, cat, project_type="unknown")
    assert "agent_hooks" in exc.value.what
    assert "block-no-verify" in exc.value.fix  # nearest valid


def test_plan_unknown_by_type_bundle_fails_closed(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"skills": {"by_type": {"enable": ["backendd"]}}}, tmp_path)  # typo
    with pytest.raises(UnknownItemError) as exc:
        build(cfg, cat, project_type="unknown")
    assert "backend" in exc.value.fix  # nearest valid bundle


def test_plan_unknown_ci_item_fails_closed(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"skills": {"enabled": False}, "ci": {"items": {"secret_scan": {"enabled": True}}}},
        tmp_path,
    )
    with pytest.raises(UnknownItemError) as exc:
        build(cfg, cat, project_type="unknown")
    assert "secret-scan" in exc.value.fix  # nearest valid


def test_plan_ci_all_true_enables_catalog(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"skills": {"enabled": False}, "ci": {"all": True}}, tmp_path)
    plan = build(cfg, cat, project_type="unknown")
    ci_items = {a.item for a in plan.actions if a.category == "ci"}
    # codeql + secret-scan are real workflow slots in the fake catalog; both should be on
    assert "codeql" in ci_items
    assert "secret-scan" in ci_items


def test_plan_unknown_ci_enable_name_fails_closed(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"skills": {"enabled": False}, "ci": {"all": False, "enable": ["secret_scan"]}},
        tmp_path,
    )
    with pytest.raises(UnknownItemError, match="unknown ci item"):
        build(cfg, cat, project_type="unknown")


def test_plan_ci_all_with_disable(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg({"skills": {"enabled": False}, "ci": {"all": True, "disable": ["codeql"]}}, tmp_path)
    plan = build(cfg, cat, project_type="unknown")
    ci_items = {a.item for a in plan.actions if a.category == "ci"}
    assert "codeql" not in ci_items
    assert "secret-scan" in ci_items


def test_plan_unknown_git_hooks_key_fails_closed(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    cfg = _cfg(
        {"skills": {"enabled": False}, "ci": {"enabled": False}, "mcp": {"enabled": False},
         "git_hooks": {"dispatcherr": {"enabled": True}}},  # typo
        tmp_path,
    )
    with pytest.raises(UnknownItemError) as exc:
        build(cfg, cat, project_type="unknown")
    assert "git_hooks" in exc.value.what
    assert "dispatcher" in exc.value.fix


def test_plan_ship_absent_from_catalog_fails_closed(fake_agent_tools, tmp_path):
    # remove ship from the catalog, then enabling it must fail closed (not silently drop)
    cat = Catalog.scan(str(fake_agent_tools))
    cat.items = [i for i in cat.items if not (i.category == "ci" and i.name == "ship")]
    cfg = _cfg(
        {"skills": {"enabled": False}, "ci": {"items": {"ship": {"enabled": True}}}},
        tmp_path,
    )
    with pytest.raises(UnknownItemError, match="unknown ci item"):
        build(cfg, cat, project_type="unknown")


def test_xdg_config_home_maps_dispatcher_dir(fake_agent_tools, tmp_path, monkeypatch):
    """A portable ~/.config dir resolves under $XDG_CONFIG_HOME at apply time when set."""
    from riglib.plan import _expand

    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    resolved = _expand("~/.config/git/global-hooks.d", tmp_path)
    assert resolved == xdg / "git" / "global-hooks.d"
    # without XDG it falls back to ~/.config
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    import os

    home = Path(os.path.expanduser("~"))
    assert _expand("~/.config/git", tmp_path) == home / ".config" / "git"


def test_expand_user_path_contract(tmp_path, monkeypatch):
    from riglib.paths import expand_user_path

    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("ROOT_WITH_TILDE", "~/from-var")

    assert expand_user_path("~/plain") == home / "plain"
    assert expand_user_path("$ROOT_WITH_TILDE/tool") == home / "from-var" / "tool"
    assert expand_user_path("~/.config/rig") == xdg / "rig"
    assert expand_user_path("relative/path") == Path("relative/path")


def test_plan_disabled_category(fake_agent_tools, tmp_path):
    cat = Catalog.scan(str(fake_agent_tools))
    # agents_md, the ship_delegator, every github sub-block (ruleset/merge/ghas/actions/browser),
    # the global-excludes block, tg_ctl, tcc_watch, tgctl_reaper, sysmon, and the permission allowlist are
    # default-ON, as are the harness's global reporting instructions, so turn them
    # off too to assert a truly empty plan.
    cfg = _cfg(
        {
            "skills": {"enabled": False},
            "agent_hooks": {"enabled": False},
            "ci": {"enabled": False},
            "mcp": {"enabled": False},
            "agents_md": {"enabled": False},
            "ship_delegator": {"enabled": False},
            "github": {
                "ruleset": {"enabled": False},
                "merge": {"enabled": False},
                "ghas": {"enabled": False},
                "actions": {"enabled": False},
                "browser": {"enabled": False},
            },
            "gitignore": {"enabled": False},
            "tg_ctl": {"enabled": False},
            "tcc_watch": {"enabled": False},
            "tgctl_reaper": {"enabled": False},
            "sysmon": {"enabled": False},
            "permissions": {"enabled": False},
            "lint_checks": {"enabled": False},  # default-ON: analyzer install (+ hook/workflow)
            "harness": {"enabled": False},  # default-ON: global instruction files
        },
        tmp_path,
    )
    # the analyzer + hook are machine-wide: only the GLOBAL layer's opt-out removes them
    cfg.global_data["lint_checks"] = {"enabled": False}
    plan = build(cfg, cat, project_type="cli")
    assert len(plan) == 0
