"""Tests for the Content-Disposition header builder used by the file
download endpoint.

The helper has to be safe for filenames sourced from agent or skill
output. The biggest concern is header injection via CR/LF — a filename
containing ``\\r\\n`` could otherwise add a new header to the response.
"""

from rhiza_agents.routes.conversations import _content_disposition_attachment


def test_simple_ascii_filename():
    h = _content_disposition_attachment("hello.txt")
    assert h == "attachment; filename=\"hello.txt\"; filename*=UTF-8''hello.txt"


def test_double_quote_in_filename_is_escaped_in_ascii_part():
    h = _content_disposition_attachment('say "hi".txt')
    # The ASCII filename="..." form must escape the embedded quote so
    # the value cannot end early.
    assert 'filename="say \\"hi\\".txt"' in h
    # The pct-encoded form encodes the quote.
    assert "filename*=UTF-8''say%20%22hi%22.txt" in h


def test_backslash_in_filename_is_escaped():
    h = _content_disposition_attachment("path\\to\\thing.txt")
    assert 'filename="path\\\\to\\\\thing.txt"' in h


def test_cr_lf_stripped_to_prevent_header_injection():
    # The injection vector: a filename containing CRLF could break
    # out of the Content-Disposition value into a new header line.
    h = _content_disposition_attachment("foo\r\nX-Injected: bar.txt")
    # Neither raw CR nor LF may appear in the final header value.
    assert "\r" not in h
    assert "\n" not in h
    # The injected token survives as inert bytes within the filename;
    # it just can't form a new header.
    assert "X-Injected:" in h


def test_control_characters_stripped():
    # NUL and other ASCII control chars in 0x00-0x1F + 0x7F range.
    h = _content_disposition_attachment("a\x00b\x07c\x1fd\x7fe.txt")
    # Result should contain only the printable letters.
    assert 'filename="abcde.txt"' in h


def test_unicode_filename_uses_pct_encoded_form():
    h = _content_disposition_attachment("café.csv")
    # Non-ASCII becomes ? in the ascii fallback.
    assert 'filename="caf?.csv"' in h
    # And percent-encoded UTF-8 in the modern form.
    assert "filename*=UTF-8''caf%C3%A9.csv" in h


def test_empty_filename_falls_back_to_default():
    h = _content_disposition_attachment("")
    assert 'filename="file"' in h
    assert "filename*=UTF-8''file" in h


def test_only_control_chars_falls_back_to_default():
    h = _content_disposition_attachment("\r\n\x00\x07")
    assert 'filename="file"' in h


def test_starts_with_attachment_disposition():
    # Always sets attachment disposition (download), never inline.
    assert _content_disposition_attachment("x.txt").startswith("attachment;")


def test_no_unescaped_quote_can_terminate_filename_value():
    """Header parsers locate the end of the filename="..." value at the
    first unescaped double quote. Ensure escaping is tight enough that a
    pathological input can't terminate early.
    """
    pathological = 'a"; attachment; filename="b'
    h = _content_disposition_attachment(pathological)
    # Find the filename= token and verify the next " is escaped (i.e.
    # preceded by a backslash) until the actual closing quote.
    start = h.index('filename="') + len('filename="')
    # Walk the value byte by byte; the only unescaped " must be at the
    # very end of the filename token before the trailing semicolon.
    i = start
    while i < len(h):
        if h[i] == '"' and h[i - 1] != "\\":
            break
        i += 1
    # After that closing quote we expect "; filename*=" — the modern
    # form follows. If pathological input had broken out, we'd find a
    # bare "; attachment;" sequence inside the ASCII form.
    rest = h[i + 1 :]
    assert rest.startswith("; filename*="), f"unexpected suffix: {rest!r}"
