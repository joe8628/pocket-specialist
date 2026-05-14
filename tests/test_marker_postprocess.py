"""Unit tests for src/marker_postprocess.py."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from marker_postprocess import clean


def test_clean_is_callable():
    assert callable(clean)


def test_clean_passthrough():
    text = "hello world"
    assert clean(text) == text
