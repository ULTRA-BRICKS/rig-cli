"""config-web's HTML rendering: the self-contained page, tab bar, and per-area fragments.

Split out of :mod:`riglib.config_web`. Pure string building over the view model
(:mod:`riglib.config_web_model`) — no I/O and no ``http.server``, so the tests render a page
directly without a socket. The page is one inline-CSS/vanilla-JS document: no CDN, no framework.
"""

from __future__ import annotations

import html
import json
from typing import Any

from . import schema
from .config_web_model import AreaView, ConfigModel, FieldView
from .layers import REPO
from .provenance import KNOWN_NAMES_SHOWN

# The rendered title is shared by the page and the tests, so it is defined once.
PAGE_TITLE = "rig config-web"

# ── HTML rendering (self-contained, no external assets) ─────────────────────────────────────
def _fmt_value(value: Any) -> str:
    """Render a value in YAML/CLI casing (true/false/null), not Python repr — matches `config get`."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (list, dict)):
        # a non-scalar default/value: emit inline YAML (``[mon, tue]``), not a Python repr
        # (``['mon', 'tue']`` with single quotes) — keeps the "matches `config get`" invariant.
        # The wizard registry exposes only scalar options today, so this is defense-in-depth for
        # any future list/dict-valued field rather than a path hit in practice.
        import yaml  # lazy, like the rest of the module

        return yaml.safe_dump(value, default_flow_style=True).strip()
    return str(value)


def _control_value(key: str, value: Any) -> str:
    """The HTML control value for a committed config value."""
    option = schema.option_for_key(key)
    if option is not None and option.kind == schema.KIND_ENUM and option.default is None and value is None:
        return ""
    return _fmt_value(value)


def _bool_control(f: FieldView, key_attr: str) -> str:
    """The toggle for a bool field (plus the UNMANAGED badge when rig does not know the value)."""
    checked = " checked" if f.value is True else ""
    # UNMANAGED (schema.effective_value's sentinel for a mode-gated option outside its
    # gating mode — e.g. github.ruleset's unattributed-approval knob on a non-autonomous
    # repo): rig never asked GitHub for the live value, so an unchecked toggle here is NOT
    # "off" — it is "unknown". A bare checkbox can't show a third state, so mark it visibly
    # instead of silently rendering as an ordinary false. The control stays editable: a click
    # still writes an explicit true/false override, which is a valid design (an explicit
    # value always wins over the mode-gated default).
    unmanaged_badge = (
        ' <span class="badge unmanaged" title="rig has not asked GitHub for the live value '
        'here — the toggle above does NOT reflect it">unmanaged</span>'
        if f.value == schema.UNMANAGED
        else ""
    )
    return (
        f'<label class="switch"><input type="checkbox" data-key="{key_attr}" '
        f'data-kind="bool"{checked} onchange="edit(this)"><span class="slider"></span></label>'
        f'{unmanaged_badge}'
    )


def _enum_control(f: FieldView, key_attr: str, val: str) -> str:
    """The ``<select>`` for an enum field; a default-less enum also offers the unpinned null."""
    if f.default is None:
        selected = " selected" if f.value is None else ""
        null_option = f'<option value=""{selected}>(fan-out / unpinned)</option>'
    else:
        null_option = ""
    opts = "".join(
        f'<option value="{html.escape(c, quote=True)}"'
        f'{" selected" if c == val else ""}>{html.escape(c)}</option>'
        for c in f.choices
    )
    return (
        f'<select data-key="{key_attr}" data-kind="enum" onchange="edit(this)">'
        f"{null_option}{opts}</select>"
    )


def _field_control(f: FieldView) -> str:
    """The input control for one field, keyed by kind (bool→toggle, enum→select, else→text)."""
    val = _fmt_value(f.value)
    key_attr = html.escape(f.key, quote=True)
    if f.kind == schema.KIND_BOOL:
        return _bool_control(f, key_attr)
    if f.kind == schema.KIND_ENUM:
        return _enum_control(f, key_attr, val)
    input_type = "number" if f.kind == schema.KIND_INT else "text"
    return (
        f'<input type="{input_type}" class="txt" value="{html.escape(val, quote=True)}" '
        f'data-key="{key_attr}" data-kind="{html.escape(f.kind, quote=True)}" '
        f'onchange="edit(this)">'
    )


def _layer_badge(layer: str) -> str:
    cls = "repo" if layer == REPO else "global"
    label = "repo" if layer == REPO else "global"
    return f'<span class="badge {cls}" title="edits land in this layer">{label}</span>'


def _field_row(f: FieldView) -> str:
    is_default = f.value == f.default
    # An UNMANAGED value is never "the default" — showing "· default false" next to it would
    # read as confirmation the live value IS false, when rig genuinely doesn't know it.
    default_note = (
        ""
        if is_default or f.value == schema.UNMANAGED
        else f' · default <code>{html.escape(_fmt_value(f.default))}</code>'
    )
    unmanaged_note = (
        '<div class="hint unmanaged">unmanaged: this key is absent and rig\'s mode-gating did '
        "not inject a default here, so the toggle above does not reflect the live GitHub value — "
        "toggling it writes an explicit override.</div>"
        if f.value == schema.UNMANAGED
        else ""
    )
    shadow_note = (
        f'<div class="hint shadow">shadowed: <code>{html.escape(f.shadowed_by)}</code> also sets '
        f'this key (<code>{html.escape(_fmt_value(f.value))}</code>), and a repo value REPLACES '
        'the global one for this repo — an edit here lands in the global file and takes effect '
        'here only once that repo entry is removed by hand.</div>'
        if f.shadowed_by
        else ""
    )
    return (
        '<div class="field">'
        f'<div class="field-head"><code class="key">{html.escape(f.key)}</code>'
        f'{_layer_badge(f.layer)}'
        f'<span class="ctl">{_field_control(f)}</span></div>'
        f'<div class="hint">{html.escape(f.hint)}{default_note}</div>'
        f'{unmanaged_note}'
        f'{shadow_note}'
        '</div>'
    )


def _area_section(a: AreaView) -> str:
    rows = "".join(_field_row(f) for f in a.fields)
    return (
        '<section class="area">'
        f'<h2>{html.escape(a.title)} {_layer_badge(a.layer)}</h2>'
        f'<p class="blurb">{html.escape(a.blurb)}</p>'
        f'{rows}</section>'
    )


def _subtitle(model: ConfigModel) -> str:
    """The provenance line under the tab bar: which files this scope reads/writes."""
    if model.global_only:
        global_state = "present" if model.global_present else "absent (created on first edit)"
        return (
            f'global-only scope · <code>{html.escape(str(model.global_path))}</code> '
            f'({global_state}) · no repo overlay<br>'
            'edits route here directly; reconcile with <code>rig apply commit</code>'
        )
    repo_state = "present" if model.repo_present else "absent (run `rig init`)"
    global_state = "present" if model.global_present else "absent (created on first global edit)"
    return (
        f'checkout <code>{html.escape(str(model.repo_root))}</code><br>'
        f'rig.yaml <code>{html.escape(str(model.repo_path))}</code> ({repo_state}) · '
        f'global <code>{html.escape(str(model.global_path))}</code> ({global_state})<br>'
        'edits route to the owning layer; reconcile with <code>rig apply commit</code>'
    )


def areas_fragment(model: ConfigModel) -> str:
    """Just the area sections + subtitle — the HTML swapped in by a JS tab switch (no page reload)."""
    sections = "".join(_area_section(a) for a in model.areas)
    return f'<p class="sub">{_subtitle(model)}</p>{sections}'


def _tab_bar(scopes: list, active_id: str) -> str:
    """The scope tab bar (rig-cli#310) — one tab per rig-managed repo + the Global tab.

    Each tab is a real ``<a href>`` (a bare page still works with JS disabled / for curl) AND
    carries ``data-scope`` for the JS-driven no-reload switch (:func:`areas_fragment` fetched via
    ``/api/scope``).
    """
    from urllib.parse import quote as _urlquote

    tabs = []
    for scope in scopes:
        cls = "tab active" if scope.id == active_id else "tab"
        # A repo path can legally contain &, #, %, +, ? (all valid in a Unix directory name).
        # html.escape alone is only ATTRIBUTE-safe, not QUERY-safe — url-quote the id first (a
        # `&`-containing path would otherwise silently truncate the query / route to the wrong
        # scope for the no-JS/copy-paste/middle-click path — found in review, independently by
        # two reviewers), THEN html.escape the already-percent-encoded result for the attribute.
        href = f"/?scope={html.escape(_urlquote(scope.id, safe=''), quote=True)}"
        label = "Global" if scope.is_global else scope.label
        tabs.append(
            f'<a class="{cls}" href="{href}" data-scope="{html.escape(scope.id, quote=True)}" '
            f'onclick="return switchScope(event, this)">{html.escape(label)}</a>'
        )
    return f'<nav class="tabs">{"".join(tabs)}</nav>'


def build_html(
    model: ConfigModel, scopes: list | None = None, active_scope: Any | None = None
) -> str:
    """The whole page as one string (tested directly, no socket — mirrors stats.render.web).

    ``scopes``/``active_scope`` (rig-cli#310) render the machine-wide tab bar; omitted (the
    single-repo call some direct tests still use), the page renders exactly as before — no tab
    bar, one scope's areas.
    """
    active_id = active_scope.id if active_scope is not None else ""
    tab_bar = _tab_bar(scopes, active_id) if scopes else ""
    body = areas_fragment(model)
    # Precomputed OUTSIDE the f-string: Python <3.12 forbids a backslash inside an f-string
    # ``{expression}`` part (PEP 701 only lifts this in 3.12+), and this repo supports 3.10+
    # (pyproject.toml requires-python) — the escaped-JSON expression below needs a literal
    # ``\\u003c`` (see the CURRENT_SCOPE comment further down for WHY), which broke CI on
    # 3.10/3.11 while passing locally on a newer interpreter (found by CI, not local pytest).
    current_scope_json = json.dumps(active_id).replace("<", "\\u003c")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(PAGE_TITLE)}</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ font: 14px/1.5 -apple-system, system-ui, sans-serif; background:#16181c; color:#e8eaed;
          margin:0; padding:24px; max-width:920px; }}
  h1 {{ font-size:20px; margin:0 0 4px; }}
  h2 {{ font-size:15px; margin:0 0 4px; display:flex; align-items:center; gap:8px; }}
  .sub {{ color:#9aa0a6; margin:0 0 20px; }}
  .sub code {{ color:#cdd1d6; }}
  .area {{ background:#202124; border:1px solid #2d2f34; border-radius:10px; padding:16px 18px;
           margin-bottom:16px; }}
  .blurb {{ color:#9aa0a6; margin:0 0 12px; font-size:13px; }}
  .field {{ padding:10px 0; border-top:1px solid #2a2c31; }}
  .field:first-of-type {{ border-top:none; }}
  .field-head {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}
  code.key {{ color:#8ab4f8; font-size:13px; }}
  .ctl {{ margin-left:auto; }}
  .hint {{ color:#9aa0a6; font-size:12px; margin-top:4px; }}
  .hint code {{ color:#cdd1d6; }}
  .badge {{ font-size:10px; text-transform:uppercase; letter-spacing:.5px; padding:2px 6px;
            border-radius:4px; font-weight:600; }}
  .badge.repo {{ background:#1e3a5f; color:#8ab4f8; }}
  .badge.global {{ background:#3d2f1e; color:#fbbc04; }}
  .badge.unmanaged {{ background:#3a1e1e; color:#f28b82; margin-left:6px; }}
  input.txt, select {{ background:#16181c; color:#e8eaed; border:1px solid #3c4043;
            border-radius:6px; padding:5px 8px; font:13px system-ui; min-width:120px; }}
  .switch {{ position:relative; display:inline-block; width:40px; height:22px; }}
  .switch input {{ opacity:0; width:0; height:0; }}
  .slider {{ position:absolute; cursor:pointer; inset:0; background:#3c4043; border-radius:22px;
             transition:.15s; }}
  .slider:before {{ position:absolute; content:""; height:16px; width:16px; left:3px; bottom:3px;
             background:#e8eaed; border-radius:50%; transition:.15s; }}
  .switch input:checked + .slider {{ background:#34a853; }}
  .switch input:checked + .slider:before {{ transform:translateX(18px); }}
  #toast {{ position:fixed; bottom:20px; left:50%; transform:translateX(-50%); padding:10px 18px;
            border-radius:8px; font-size:13px; opacity:0; transition:.2s; pointer-events:none;
            max-width:80vw; z-index:50; }}
  #toast.ok {{ background:#1e3a2a; color:#81c995; border:1px solid #34a853; }}
  #toast.err {{ background:#3a1e1e; color:#f28b82; border:1px solid #ea4335; }}
  #toast.show {{ opacity:1; }}
  .tabs {{ display:flex; gap:4px; flex-wrap:wrap; margin-bottom:10px; border-bottom:1px solid #2d2f34;
           padding-bottom:0; }}
  .tab {{ color:#9aa0a6; text-decoration:none; padding:8px 14px; border-radius:8px 8px 0 0;
          font-size:13px; border:1px solid transparent; border-bottom:none; }}
  .tab.active {{ color:#e8eaed; background:#202124; border-color:#2d2f34; font-weight:600; }}
  .tab:hover {{ color:#e8eaed; }}
  .layout {{ display:flex; gap:20px; align-items:flex-start; }}
  #areas {{ flex:1 1 auto; min-width:0; }}
  #side {{ flex:0 0 300px; position:sticky; top:16px; }}
  .panel {{ background:#202124; border:1px solid #2d2f34; border-radius:10px; padding:14px 16px;
            margin-bottom:16px; font-size:13px; }}
  .panel h2 {{ margin:0 0 8px; }}
  .panel button {{ background:#2d2f34; color:#e8eaed; border:1px solid #3c4043; border-radius:6px;
                   padding:4px 10px; font-size:12px; cursor:pointer; }}
  .panel button:hover {{ background:#3c4043; }}
  .drift-item {{ padding:6px 0; border-top:1px solid #2a2c31; font-size:12px; }}
  .drift-item:first-child {{ border-top:none; }}
  .drift-dir {{ font-size:9px; text-transform:uppercase; padding:1px 5px; border-radius:4px;
                margin-right:6px; }}
  .drift-dir.missing {{ background:#3d2f1e; color:#fbbc04; }}
  .drift-dir.extra {{ background:#1e3a5f; color:#8ab4f8; }}
  .drift-dir.modified {{ background:#3a1e1e; color:#f28b82; }}
  .in-sync {{ color:#81c995; }}
  #apply-cta {{ position:fixed; bottom:20px; right:20px; background:#8ab4f8; color:#16181c;
                border:none; border-radius:8px; padding:10px 18px; font-size:13px; font-weight:600;
                cursor:pointer; box-shadow:0 2px 10px rgba(0,0,0,.4); z-index:40; }}
  #apply-cta[hidden] {{ display:none; }}
  #plan-modal {{ position:fixed; inset:0; background:rgba(0,0,0,.6); display:flex;
                 align-items:center; justify-content:center; z-index:60; }}
  #plan-modal[hidden] {{ display:none; }}
  #plan-card {{ background:#202124; border:1px solid #2d2f34; border-radius:12px; padding:20px;
                width:min(720px, 92vw); max-height:82vh; overflow:auto; }}
  .modal-header {{ display:flex; align-items:center; justify-content:space-between; }}
  #plan-close-x {{ background:none; border:none; color:#9aa0a6; font-size:22px; line-height:1;
                    cursor:pointer; padding:0 4px; }}
  #plan-close-x:hover {{ color:#e8eaed; }}
  .plan-row {{ display:flex; align-items:flex-start; gap:10px; padding:10px 0;
               border-top:1px solid #2a2c31; }}
  .plan-row:first-of-type {{ border-top:none; }}
  .plan-tag {{ font-size:10px; text-transform:uppercase; letter-spacing:.3px; padding:2px 6px;
               border-radius:4px; white-space:nowrap; background:#1e3a5f; color:#8ab4f8; }}
  .plan-audience {{ font-size:10px; color:#9aa0a6; }}
  .plan-target {{ font-size:11px; color:#9aa0a6; font-family:ui-monospace,monospace; word-break:break-all; }}
  .plan-status {{ font-size:11px; font-weight:600; }}
  .plan-status.queued {{ color:#9aa0a6; }}
  .plan-status.running {{ color:#fbbc04; }}
  .plan-status.created,.plan-status.updated,.plan-status.backed_up {{ color:#81c995; }}
  .plan-status.skipped {{ color:#9aa0a6; }}
  .plan-status.error {{ color:#f28b82; }}
  #plan-actions {{ display:flex; gap:10px; margin-top:16px; }}
  #plan-actions button {{ flex:1; padding:10px; border-radius:8px; border:none; font-size:13px;
                          font-weight:600; cursor:pointer; }}
  #plan-apply-btn {{ background:#34a853; color:#0b1f12; }}
  #plan-cancel-btn {{ background:#2d2f34; color:#e8eaed; }}
</style></head><body>
<h1>{html.escape(PAGE_TITLE)}</h1>
{tab_bar}
<div class="layout">
<div id="areas">{body}</div>
<div id="side">
  <section class="panel" id="drift-panel">
    <h2>Drift <button onclick="loadDrift()">refresh</button></h2>
    <div id="drift-body">loading…</div>
  </section>
</div>
</div>
<button id="apply-cta" hidden onclick="openPlan()">Review &amp; apply changes</button>
<div id="plan-modal" hidden>
  <div id="plan-card">
    <div class="modal-header"><h2>Plan preview</h2>
      <button id="plan-close-x" onclick="closePlan()" aria-label="Close">&times;</button></div>
    <div id="plan-body">loading…</div>
    <div id="plan-actions" hidden>
      <button id="plan-cancel-btn" onclick="closePlan()">Cancel</button>
      <button id="plan-apply-btn" onclick="submitApply()">Apply selected</button>
    </div>
  </div>
</div>
<div id="toast"></div>
<script>
// json.dumps() does not escape "<", so a scope id containing "</script>" would otherwise
// terminate this element early on EVERY page render. An earlier version only replaced "</" —
// insufficient: a directory name containing "<!--<script>" moves the HTML tokenizer through
// script-data-escaped into script-data-DOUBLE-escaped state, where this template's own literal
// "</script>" no longer closes the element either (it only toggles state) — not exploitable as
// XSS (every "</" is still escaped), but it silently kills ALL page JS on that scope's render
// (found in review, a second pass). Escaping EVERY "<" closes both the plain and the
// double-escape path in one rule.
var CURRENT_SCOPE = {current_scope_json};
var PENDING_PLAN = null;  // {{fingerprint, actions}} from the last /api/plan fetch
var APPLY_POLL = null;

// Every drift/plan field below comes from the SERVER (repo directory names, catalog item names,
// action targets) — NOT trusted input. innerHTML-interpolate ONLY through this escaper (found in
// review: an unescaped repo/target string could inject a <script> that runs at this origin and
// drives same-origin /edit or /api/apply, which the CSRF guard does NOT catch since it only
// rejects CROSS-site requests).
function esc(s) {{
  return String(s).replace(/[&<>"']/g, function(c) {{
    return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c];
  }});
}}

function toast(msg, ok) {{
  var t = document.getElementById('toast');
  t.textContent = msg; t.className = (ok ? 'ok' : 'err') + ' show';
  setTimeout(function() {{ t.className = t.className.replace(' show', ''); }}, 2600);
}}
function seedCommitted(root) {{
  root.querySelectorAll('[data-key]').forEach(function(el) {{
    el.dataset.committed = (el.getAttribute('data-kind') === 'bool') ? (el.checked ? 'true' : 'false') : el.value;
  }});
}}
// remember each control's last-committed state so a REJECTED edit can revert the visible value
// (the server rolled the file back, so the page must not keep showing the unsaved value).
function revert(el) {{
  if (el.getAttribute('data-kind') === 'bool') {{ el.checked = (el.dataset.committed === 'true'); }}
  else {{ el.value = (el.dataset.committed === undefined ? el.value : el.dataset.committed); }}
}}
function markPending() {{
  document.getElementById('apply-cta').hidden = false;
}}
async function edit(el) {{
  var key = el.getAttribute('data-key');
  var kind = el.getAttribute('data-kind');
  var value = (kind === 'bool') ? (el.checked ? 'true' : 'false') : el.value;
  try {{
    var r = await fetch('/edit', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{key: key, value: value, scope: CURRENT_SCOPE}})
    }});
    var data = await r.json();
    if (r.ok && data.ok) {{
      el.dataset.committed = (kind === 'bool') ? value : (data.control_value ?? data.value);  // new committed state
      toast(key + ' = ' + data.value + '  →  ' + data.file, true);
      markPending();  // the write landed but is NOT yet applied to disk -- offer to review+apply
    }} else {{
      revert(el);  // server rejected + rolled back → restore the control to its committed value
      toast('rejected: ' + (data.error || r.status), false);
    }}
  }} catch (e) {{ revert(el); toast('request failed: ' + e, false); }}
}}
seedCommitted(document);

// -- scope tab switching (no page reload) ------------------------------------------------------
async function switchScope(evt, a) {{
  evt.preventDefault();
  var scope = a.getAttribute('data-scope');
  try {{
    var r = await fetch('/api/scope?scope=' + encodeURIComponent(scope));
    var data = await r.json();
    if (!r.ok || !data.ok) {{ toast('could not load scope: ' + (data.error || r.status), false); return; }}
    document.getElementById('areas').innerHTML = data.html;
    seedCommitted(document.getElementById('areas'));
    document.querySelectorAll('.tab').forEach(function(t) {{ t.classList.remove('active'); }});
    a.classList.add('active');
    CURRENT_SCOPE = scope;
    history.pushState({{}}, '', '/?scope=' + encodeURIComponent(scope));
    document.getElementById('apply-cta').hidden = true;
    loadDrift();
  }} catch (e) {{ toast('request failed: ' + e, false); }}
  return false;
}}

// -- drift panel ---------------------------------------------------------------------------------
async function loadDrift() {{
  var body = document.getElementById('drift-body');
  body.textContent = 'loading…';
  try {{
    var r = await fetch('/api/drift?scope=' + encodeURIComponent(CURRENT_SCOPE));
    var data = await r.json();
    if (!r.ok || data.ok === false) {{ body.textContent = 'drift check failed: ' + (data.error || r.status); return; }}
    var rows = data.items.map(function(i) {{
      return '<div class="drift-item"><span class="drift-dir ' + esc(i.direction) + '">' + esc(i.direction) +
        '</span>' + esc(i.category) + '/' + esc(i.item) + '<div class="plan-target">' + esc(i.target) + '</div>' +
        '<div>' + esc(i.detail) + '</div></div>';
    }}).join('');
    // missing_targets (a dead hook reference) is a SEPARATE, higher-severity class from ordinary
    // config<->disk drift -- rendering only `items` would silently say "in sync" while a hook
    // command points at a file that's gone (found in review: rig status shows this as a distinct
    // "missing targets" block for exactly this reason).
    var missing = (data.missing_targets || []).map(function(m) {{
      return '<div class="drift-item"><span class="drift-dir missing">missing target</span>' +
        esc(m.what) + '<div>' + esc(m.why) + '</div><div class="plan-target">fix: ' + esc(m.fix) + '</div></div>';
    }}).join('');
    // known items (rig-cli#357): on disk, not rig-managed, origin named -- informational, never
    // drift. Rendered under their own heading so a clean scope with tool-installed skills still
    // reads as in sync (the payload's `in_sync` ignores them, and so must this panel).
    // One row per (container-or-category, origin label) with the member names joined and capped
    // -- the SAME shape the CLI prints (`_print_known_groups`, `_KNOWN_NAMES_SHOWN`): a kept
    // allowlist runs to hundreds of entries, and one <div> per entry would flood the panel.
    var KNOWN_NAMES_SHOWN = {KNOWN_NAMES_SHOWN};
    function knownName(k) {{
      return !k.kept && k.by && k.by !== k.name ? k.name + ' (by ' + k.by + ')' : k.name;
    }}
    function knownGroups(list) {{
      var groups = {{}}, order = [];
      list.forEach(function(k) {{
        var where = k.kept ? k.container : k.category;
        var key = where + ' :: ' + k.label;
        if (!groups[key]) {{ groups[key] = {{where: where, label: k.label, kept: k.kept, names: []}}; order.push(key); }}
        groups[key].names.push(knownName(k));
      }});
      return order.map(function(key) {{
        var g = groups[key];
        var shown = g.names.slice(0, KNOWN_NAMES_SHOWN).join(', ');
        if (g.names.length > KNOWN_NAMES_SHOWN) {{ shown += ' … and ' + (g.names.length - KNOWN_NAMES_SHOWN) + ' more'; }}
        return '<div class="drift-item"><span class="drift-dir known">' + (g.kept ? 'kept' : 'known') + '</span>' +
          esc(g.where) + ' (' + g.names.length + ')<div class="plan-target">' + esc(g.label) + '</div><div>' + esc(shown) + '</div></div>';
      }}).join('');
    }}
    // the CLI's two headings: placed items vs permission additions (never removed)
    var placed = (data.known || []).filter(function(k) {{ return !k.kept; }});
    var kept = (data.known || []).filter(function(k) {{ return k.kept; }});
    var knownBlock = '';
    if (placed.length) {{
      knownBlock += '<div class="in-sync">known, not managed by rig (' + placed.length + ') — informational, not drift</div>' + knownGroups(placed);
    }}
    if (kept.length) {{
      knownBlock += '<div class="in-sync">your additions, kept (' + kept.length + ') — beyond the rig baseline; rig never removes them</div>' + knownGroups(kept);
    }}
    // informational notes (rig-cli#375): not drift, but never dropped — the CLI prints the same
    // text beside the area state (e.g. Spotlight dirs created since the last sweep).
    (data.notes || []).forEach(function(n) {{
      knownBlock += '<div class="in-sync">' + esc(n.category + '/' + n.item) + ': ' + esc(n.detail) + '</div>';
    }});
    if (!rows && !missing) {{ body.innerHTML = '<div class="in-sync">✓ in sync — config and disk agree</div>' + knownBlock; return; }}
    body.innerHTML = missing + rows + knownBlock;
    // pre-existing drift (declared but never applied, or applied then hand-edited away) must
    // offer the SAME apply entry point an edit does -- previously the CTA only appeared right
    // after a successful edit, so a scope with drift on first load / tab switch had no way to
    // reach the plan/apply flow without making an unrelated change first (found in review).
    document.getElementById('apply-cta').hidden = false;
  }} catch (e) {{ body.textContent = 'drift check failed: ' + e; }}
}}
loadDrift();

// -- plan preview / confirm-skip / live apply -----------------------------------------------------
function closePlan() {{
  document.getElementById('plan-modal').hidden = true;
  if (APPLY_POLL) {{ clearInterval(APPLY_POLL); APPLY_POLL = null; }}
}}
async function openPlan() {{
  var modal = document.getElementById('plan-modal');
  var bodyEl = document.getElementById('plan-body');
  var actionsEl = document.getElementById('plan-actions');
  modal.hidden = false;
  actionsEl.hidden = true;
  bodyEl.textContent = 'building plan…';
  try {{
    var r = await fetch('/api/plan?scope=' + encodeURIComponent(CURRENT_SCOPE), {{method: 'POST'}});
    var data = await r.json();
    if (!r.ok || data.ok === false) {{ bodyEl.textContent = 'could not build plan: ' + (data.error || r.status); return; }}
    PENDING_PLAN = data;
    if (!data.actions.length) {{
      bodyEl.innerHTML = '<div class="in-sync">nothing to apply — already in sync</div>';
      return;
    }}
    bodyEl.innerHTML = data.actions.map(function(a) {{
      return '<div class="plan-row" data-key="' + esc(a.key) + '">' +
        '<input type="checkbox" checked data-skip-toggle>' +
        '<div style="flex:1"><div><span class="plan-tag">' + esc(a.tag.label) + '</span> ' +
        '<span class="plan-audience">(' + esc(a.tag.audience) + ')</span></div>' +
        '<div>' + esc(a.describe) + '</div>' +
        '<div class="plan-target">' + esc(a.target) + '</div>' +
        '<div class="plan-target">' + esc(a.tag.detail) + '</div>' +
        '<div class="plan-status queued" data-status>queued</div></div></div>';
    }}).join('');
    actionsEl.hidden = false;
  }} catch (e) {{ bodyEl.textContent = 'could not build plan: ' + e; }}
}}
async function submitApply() {{
  if (!PENDING_PLAN) return;
  var skip = [];
  document.querySelectorAll('.plan-row').forEach(function(row) {{
    var cb = row.querySelector('[data-skip-toggle]');
    if (!cb.checked) skip.push(row.getAttribute('data-key'));
  }});
  document.getElementById('plan-actions').hidden = true;
  try {{
    var r = await fetch('/api/apply', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{scope: CURRENT_SCOPE, fingerprint: PENDING_PLAN.fingerprint, skip_keys: skip}})
    }});
    var data = await r.json();
    if (!r.ok || data.ok === false) {{
      // a refusal (e.g. 409 stale plan) -- re-show the actions so the user isn't stuck behind a
      // dimmed overlay with only the header × to escape (found in review); the header × ALSO
      // always works regardless, as the reliable fallback for every other path.
      document.getElementById('plan-actions').hidden = false;
      toast('apply refused: ' + (data.error || r.status), false);
      return;
    }}
    pollApply(data.job_id);
  }} catch (e) {{
    document.getElementById('plan-actions').hidden = false;
    toast('apply failed: ' + e, false);
  }}
}}
function pollApply(jobId) {{
  APPLY_POLL = setInterval(async function() {{
    try {{
      var r = await fetch('/api/apply/status?job=' + encodeURIComponent(jobId));
      var data = await r.json();
      if (!r.ok) {{ clearInterval(APPLY_POLL); toast('lost apply progress: ' + r.status, false); return; }}
      data.actions.forEach(function(a) {{
        var row = document.querySelector('.plan-row[data-key="' + CSS.escape(a.key) + '"]');
        if (!row) return;
        var st = row.querySelector('[data-status]');
        st.textContent = a.status + (a.detail ? ' — ' + a.detail : '');
        st.className = 'plan-status ' + a.status;
      }});
      if (data.done) {{
        clearInterval(APPLY_POLL); APPLY_POLL = null;
        toast(data.error ? ('apply error: ' + data.error) : 'applied', !data.error);
        document.getElementById('apply-cta').hidden = true;
        loadDrift();
        // the modal must never stay open, dimmed, and unclosable after a run finishes (found in
        // review: submitApply hides #plan-actions, whose Cancel button is the only closer) -- on
        // success just close it; on a partial/total failure keep it open but re-show the actions
        // (with the header × always available too) so the user can see per-action detail.
        if (data.error) {{ document.getElementById('plan-actions').hidden = false; }}
        else {{ closePlan(); }}
      }}
    }} catch (e) {{ clearInterval(APPLY_POLL); toast('lost apply progress: ' + e, false); }}
  }}, 500);
}}
</script>
</body></html>"""
