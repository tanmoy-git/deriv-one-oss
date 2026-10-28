"""_strip_html removes tags and stays linear on hostile input."""

from __future__ import annotations

import time


def test_strip_html_removes_tags():
    from api_feedback import _strip_html

    assert _strip_html("<b>bold</b> and <a href='x'>link</a>") == "bold and link"


def test_strip_html_matches_the_old_pattern_on_ordinary_text():
    import re

    from api_feedback import _strip_html

    old = re.compile(r"<[^>]+>")
    for text in [
        "Button <span class='x'>Save</span> does nothing",
        "<p>Line one</p><p>Line two</p>",
        "a < b",
        "no tags at all",
        "<img src=x onerror=alert(1)>after",
    ]:
        assert _strip_html(text) == old.sub("", text)


def test_strip_html_is_fast_on_many_unclosed_brackets():
    from api_feedback import _strip_html

    hostile = "<" * 50_000
    start = time.perf_counter()
    assert _strip_html(hostile) == hostile
    # The old pattern took seconds here (quadratic); linear takes milliseconds.
    assert time.perf_counter() - start < 0.5
