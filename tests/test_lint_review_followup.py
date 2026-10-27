"""Small final-review regressions, separate from the integration implementation."""
import json

import pytest

from riglib import lint_check, lint_check_analyzer as analyzer, lint_fix_else
from riglib.lint_check_scope import Scope


@pytest.mark.parametrize("value,files", [(True, [{}]), (False, []), (1.0, [{}])])
def test_summary_file_count_rejects_non_integer_values(value, files):
    raw = json.dumps({"files": files, "summary": {"file_count": value}}).encode()
    with pytest.raises(analyzer._Malformed):
        analyzer._file_records(raw)


def test_same_line_name_does_not_claim_ambiguous_function_is_fixable(tmp_path, monkeypatch):
    functions = [analyzer.FunctionMetric((parent, "same"), "function", 1, 20, 2)
                 for parent in ("left", "right")]
    monkeypatch.setattr(lint_fix_else, "probe", lambda *args: [1, 0])
    result = lint_check._fixable_functions(Scope(str(tmp_path), str(tmp_path)), "a.ts", functions,
                                          [(1, "same"), (1, "same")], True)
    assert result == set()


def test_unique_function_keeps_its_own_probe_count(tmp_path, monkeypatch):
    functions = [analyzer.FunctionMetric((name,), "function", line, 20, 2)
                 for line, name in [(1, "first"), (8, "second")]]
    monkeypatch.setattr(lint_fix_else, "probe", lambda *args: [0, 1])
    result = lint_check._fixable_functions(Scope(str(tmp_path), str(tmp_path)), "a.ts", functions,
                                          [(1, "first"), (8, "second")], True)
    assert result == {(("second",), 8)}
