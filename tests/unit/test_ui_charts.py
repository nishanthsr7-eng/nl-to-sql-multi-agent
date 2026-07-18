"""Tests for UI text rendering helpers.

The metric cards and comparison callout inject their text into raw HTML rather
than letting Streamlit parse it as markdown, so the escaping/bold helper is the
only thing standing between synthesis output and the rendered page.
"""

from __future__ import annotations

from semantic_query_engine.ui.charts import inline_markdown_to_html


def test_bold_spans_become_strong_tags():
    assert inline_markdown_to_html("**SnBrand2** leads") == "<strong>SnBrand2</strong> leads"


def test_multiple_bold_spans_are_each_converted():
    result = inline_markdown_to_html("**A** beat **B** by **12%**")
    assert result == "<strong>A</strong> beat <strong>B</strong> by <strong>12%</strong>"


def test_plain_text_is_unchanged():
    assert inline_markdown_to_html("2.86M Total Revenue") == "2.86M Total Revenue"


def test_unmatched_asterisks_are_left_alone():
    assert inline_markdown_to_html("**dangling") == "**dangling"


def test_markup_in_model_output_is_escaped_not_executed():
    """Synthesis text and SQL-derived column aliases must not inject HTML."""
    result = inline_markdown_to_html("<script>alert('x')</script>")
    assert "<script>" not in result
    assert "&lt;script&gt;" in result


def test_escaping_happens_before_bold_substitution():
    """A bolded span containing markup is still escaped inside the <strong>."""
    result = inline_markdown_to_html("**<b>x</b>**")
    assert result == "<strong>&lt;b&gt;x&lt;/b&gt;</strong>"
