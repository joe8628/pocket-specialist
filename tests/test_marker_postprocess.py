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


def test_fix_double_sup_removes_marker():
    inp = 'text<sup>&</sup>lt;sup>3</sup>more'
    assert clean(inp) == 'textmore'

def test_fix_double_sup_mid_sentence():
    inp = 'See footnote<sup>&</sup>lt;sup>12</sup> for details.'
    assert clean(inp) == 'See footnote for details.'

def test_fix_residual_lt_sup():
    inp = 'textlt;sup>5</sup>more'
    assert clean(inp) == 'textmore'

def test_fix_residual_lt_sup_no_false_positive():
    assert clean('the result lt x is valid') == 'the result lt x is valid'


def test_fix_math_sup_letter():
    assert clean('Q<sup>T</sup> Q') == 'Q$^{T}$ Q'

def test_fix_math_sup_symbol():
    assert clean('order *<sup>N</sup>*') == 'order *$^{N}$*'

def test_fix_math_sup_minus():
    assert clean('*p*(*x*) of order *<sup>−</sup> 1') == '*p*(*x*) of order *$^{−}$ 1'

def test_fix_math_sub_letter():
    assert clean('w<sub>i</sub>') == 'w$_{i}$'

def test_fix_math_sup_skips_digits():
    # Digit-only content is a footnote — handled by fixer 5, not fixer 4
    inp = 'word<sup>2</sup>'
    assert clean(inp) == 'word'  # fixer 5 strips it

def test_fix_math_sup_multi_char():
    assert clean('<sup>-1</sup>') == '$^{-1}$'

def test_fix_math_sup_greek():
    assert clean('<sup>λ</sup>') == '$^{λ}$'


def test_strip_footnote_sup_single_digit():
    assert clean('word<sup>1</sup>') == 'word'

def test_strip_footnote_sup_two_digits():
    assert clean('word<sup>42</sup>') == 'word'

def test_strip_footnote_sup_preserves_surrounding_text():
    assert clean('before<sup>7</sup> after') == 'before after'


def test_strip_image_ref_jpeg():
    assert clean('![](_page_3_Figure_1.jpeg)') == ''

def test_strip_image_ref_png():
    assert clean('![](_page_5_Picture_0.png)') == ''

def test_strip_image_ref_preserves_caption():
    inp = '![](_page_27_Figure_1.jpeg)\nFig. 1.1 Normalized machine numbers.'
    assert clean(inp) == '\nFig. 1.1 Normalized machine numbers.'

def test_strip_image_ref_case_insensitive():
    assert clean('![](_page_1_Figure_1.JPEG)') == ''

def test_strip_image_ref_no_false_positive():
    result = clean('![diagram](https://example.com/img.png)')
    assert result == '![diagram](https://example.com/img.png)'


def test_strip_br_tag():
    assert clean('cell one<br/>cell two') == 'cell one cell two'

def test_strip_nonbreaking_space():
    assert clean('word\xa0word') == 'word word'

def test_strip_invisible_unicode():
    assert clean('word\xadword') == 'wordword'


def test_end_to_end_realistic_marker_output():
    """Simulate a realistic Marker output chunk with all five artifact classes."""
    raw = (
        # span tag (fixer 1)
        '<span class="bold">Introduction</span>\n\n'
        # double-encoded footnote (fixers 2+3)
        'See netbeans<sup>&</sup>lt;sup>1</sup>www.netbeans.org.\n\n'
        # math-variable sup (fixer 4)
        'The matrix *Q<sup>T</sup>* satisfies *Q<sub>i</sub>* = 1.\n\n'
        # footnote-number sup (fixer 5)
        'As shown elsewhere<sup>42</sup>, this holds.\n\n'
        # image ref (fixer 6)
        '![](_page_12_Figure_3.jpeg)\nFig. 2.1 Normalized machine numbers.\n\n'
        # br tag (fixer 7a via _strip_ocr_artifacts)
        'cell one<br/>cell two\n'
    )
    result = clean(raw)

    assert '<span' not in result
    assert 'Introduction' in result

    assert 'lt;sup>' not in result
    assert '<sup>&</sup>' not in result

    assert '$^{T}$' in result
    assert '$_{i}$' in result
    assert '<sup>T</sup>' not in result

    assert '<sup>42</sup>' not in result

    assert '![](' not in result
    assert 'Fig. 2.1 Normalized machine numbers.' in result

    assert '<br' not in result
    assert 'cell one cell two' in result
