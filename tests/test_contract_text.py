from __future__ import annotations

import unittest

from recallweave.contract_text import (
    MAX_PASSAGE_CHARACTERS,
    MAX_STATEMENT_CHARACTERS,
    bounded,
    collapse,
    sanitize,
)


class SanitizeTest(unittest.TestCase):
    def test_crlf_normalized_to_lf(self) -> None:
        self.assertEqual(sanitize("a\r\nb"), "a\nb")

    def test_lone_cr_normalized_to_lf(self) -> None:
        self.assertEqual(sanitize("a\rb"), "a\nb")

    def test_c0_controls_removed_except_newline_tab(self) -> None:
        self.assertEqual(sanitize("a\x00b\x01c"), "abc")
        self.assertEqual(sanitize("a\tb\nc"), "a\tb\nc")

    def test_c1_controls_removed(self) -> None:
        self.assertEqual(sanitize("a\x85b\x9fc"), "abc")

    def test_del_removed(self) -> None:
        self.assertEqual(sanitize("a\x7fb"), "ab")

    def test_bidi_overrides_removed(self) -> None:
        self.assertEqual(sanitize("a\u202eb\u202cc"), "abc")

    def test_zero_width_removed(self) -> None:
        self.assertEqual(sanitize("a\u200bb\u200cc"), "abc")

    def test_line_paragraph_separators_removed(self) -> None:
        self.assertEqual(sanitize("a\u2028b\u2029c"), "abc")

    def test_bidi_isolate_removed(self) -> None:
        self.assertEqual(sanitize("a\u2066b\u2069c"), "abc")

    def test_featured_byte_order_mark_removed(self) -> None:
        self.assertEqual(sanitize("a\ufeffb"), "ab")

    def test_never_raises_on_any_str(self) -> None:
        cases = ["", "plain", "\x00\x1f\x7f\x80\x9f\u200b\u202e\ufeff", "\r\n\r"]
        for case in cases:
            sanitize(case)

    def test_idempotent(self) -> None:
        fixture = [
            "a\r\nb\rc",
            "x\x00y\x7f\x85\u200b\u202e\ufeffz",
            "plain text",
            "\tkeep\ntab",
            "mixed\u2028sep\u2066run",
        ]
        for case in fixture:
            once = sanitize(case)
            self.assertEqual(sanitize(once), once)


class CollapseTest(unittest.TestCase):
    def test_collapses_whitespace_runs(self) -> None:
        self.assertEqual(collapse("a   b\t\t c"), "a b c")

    def test_strips_edges(self) -> None:
        self.assertEqual(collapse("  a b  "), "a b")

    def test_never_emits_newline(self) -> None:
        self.assertNotIn("\n", collapse("a\r\nb\nc\rd"))
        self.assertNotIn("\r", collapse("a\r\nb\nc\rd"))
        self.assertNotIn("\t", collapse("a\tb"))

    def test_sanitizes_first(self) -> None:
        self.assertEqual(collapse("a\x00b\u202ec d"), "abc d")


class BoundedTest(unittest.TestCase):
    def test_raises_on_non_positive_limit(self) -> None:
        for limit in (0, -1, -10):
            with self.assertRaises(ValueError):
                bounded("text", limit)

    def test_shorter_than_limit_unchanged_not_truncated(self) -> None:
        text, truncated = bounded("hello", 10)
        self.assertEqual(text, "hello")
        self.assertFalse(truncated)

    def test_never_exceeds_limit_at_boundary(self) -> None:
        for limit in (1, 2, 5, 50, MAX_STATEMENT_CHARACTERS, MAX_PASSAGE_CHARACTERS):
            for length in (limit - 1, limit, limit + 1):
                value = "x" * length
                text, truncated = bounded(value, limit)
                self.assertLessEqual(len(text), limit)
                if length <= limit:
                    self.assertFalse(truncated)
                    self.assertEqual(text, value)
                else:
                    self.assertTrue(truncated)

    def test_truncation_marker_appended(self) -> None:
        text, truncated = bounded("abcdefgh", 4)
        self.assertTrue(truncated)
        self.assertEqual(text, "abc\u2026")

    def test_truncation_rstrips_before_marker(self) -> None:
        text, truncated = bounded("abcd  ", 5)
        self.assertTrue(truncated)
        self.assertEqual(text, "abcd\u2026")

    def test_truncation_of_non_ascii_counts_characters(self) -> None:
        value = "é" * 10
        text, truncated = bounded(value, 5)
        self.assertTrue(truncated)
        self.assertEqual(len(text), 5)

    def test_sanitizes_before_truncation(self) -> None:
        text, truncated = bounded("ab\x00cdef", 4)
        self.assertTrue(truncated)
        self.assertEqual(text, "abc\u2026")


class ConstantsTest(unittest.TestCase):
    def test_constant_values(self) -> None:
        self.assertEqual(MAX_STATEMENT_CHARACTERS, 500)
        self.assertEqual(MAX_PASSAGE_CHARACTERS, 500)


if __name__ == "__main__":
    unittest.main()
