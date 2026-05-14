"""Unit tests for src/marker_postprocess.py."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from marker_postprocess import clean


def test_clean_is_callable():
    assert callable(clean)


def test_clean_passthrough():
    text = "hello world"
    assert clean(text) == text


def test_strip_spans_simple():
    assert clean('<span class="x">hello</span> world') == 'hello world'

def test_strip_spans_multiline():
    text = 'before\n<span style="color:red">\ninner\n</span>\nafter'
    assert clean(text) == 'before\n\ninner\n\nafter'

def test_strip_spans_no_spans():
    assert clean('no spans here') == 'no spans here'
