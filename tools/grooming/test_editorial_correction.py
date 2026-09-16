import importlib.util,pathlib
import unittest
checks=unittest.TestCase()
s=importlib.util.spec_from_file_location('editorial',pathlib.Path(__file__).with_name('fix_editorial_issues.py'));m=importlib.util.module_from_spec(s);s.loader.exec_module(m)

def body(original='## Feature\nOriginal acceptance.\n',quote='> исходный запрос',kind='проверенная связь по требованию'):
 return m.START+'\n\n'+m.OWN+'\n\nРусский boilerplate\n\n'+quote+'\n\nИсточник **v4-42**, source; '+kind+'.\n\n'+m.END+'\n\n'+m.SCOPE+'\n\n'+original

def test_removes_owned_russian_prose_not_source_quote():
 result=m.editorial_body(body())
 assert 'Русский boilerplate'not in result and '> исходный запрос'in result and 'Source: [v4-42]'in result
 assert '## Feature\nOriginal acceptance.'in result

def test_preserves_whole_original_and_appended_human_notes():
 original='## Feature\n- [x] Old check\n\n## New human note\nSpecific requirement.'
 assert original in m.editorial_body(body(original))

def test_unrelated_and_hand_corrected_issues_untouched():
 for x in ['','A different issue',m.FIX+'\nAlready corrected',m.START+'\nNo owned marker']:
  assert m.editorial_body(x)is None

def test_idempotent():
 assert m.editorial_body(m.editorial_body(body()))is None

def test_shared_standing_instruction_is_not_feature_origin():
 r=m.editorial_body(body(kind='общее требование к механизму'))
 assert 'User request'not in r and 'Original acceptance.'in r

def test_missing_original_does_not_erase_issue():
 with checks.assertRaises(m.ManualReview):m.editorial_body(body(''))

def test_fenced_end_marker_remains_literal():
 original='## Scope\n```md\n'+m.END+'\n```\nMore requirements'
 assert original in m.editorial_body(body(original))

def test_duplicate_structural_boundary_requires_review():
 with checks.assertRaises(m.ManualReview):m.editorial_body(body()+m.END+'\n')

def test_independent_text_between_boundary_and_scope_not_dropped():
 with checks.assertRaises(m.ManualReview):m.editorial_body(body().replace(m.SCOPE,'Human note\n'+m.SCOPE))

def test_draw_is_excluded():
 assert 'draw-cli'not in m.REPOS
 with checks.assertRaises(ValueError):m.API('draw-cli','not-a-token')

if __name__ == "__main__":
    suite=unittest.TestSuite(unittest.FunctionTestCase(fn) for name,fn in sorted(globals().items()) if name.startswith("test_") and callable(fn))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
