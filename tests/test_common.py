"""
Unit tests for agent_pair_ipc.common
"""

import tempfile
import unittest
from pathlib import Path
from agent_pair_ipc.common import (
    resolve_message_payload,
    generate_nonce,
    attach_request_header,
    extract_nonce,
    extract_verdict,
)


class TestCommon(unittest.TestCase):
    def test_resolve_message_positional(self):
        msg, target = resolve_message_payload(message_arg="hello", target_pos="sessA")
        self.assertEqual(msg, "hello")
        self.assertEqual(target, "sessA")

    def test_resolve_message_m_flag_with_positional_target(self):
        # P0 regression test: tool -m "hello" sessA
        msg, target = resolve_message_payload(message_arg="sessA", msg_flag="hello")
        self.assertEqual(msg, "hello")
        self.assertEqual(target, "sessA")

    def test_resolve_message_file_with_positional_target(self):
        with tempfile.NamedTemporaryFile("w+", delete=False) as tf:
            tf.write("content from file")
            tf_path = tf.name

        try:
            msg, target = resolve_message_payload(message_arg="sessB", file_path=tf_path)
            self.assertEqual(msg, "content from file")
            self.assertEqual(target, "sessB")
        finally:
            Path(tf_path).unlink(missing_ok=True)

    def test_resolve_message_explicit_session_flag_precedence(self):
        msg, target = resolve_message_payload(
            message_arg="sessPos", msg_flag="hello", session_flag="sessExplicit"
        )
        self.assertEqual(msg, "hello")
        self.assertEqual(target, "sessExplicit")

    def test_nonce_generation_and_attachment(self):
        nonce = generate_nonce()
        self.assertTrue(len(nonce) > 5)
        wrapped = attach_request_header("Sample body", nonce=nonce, sender="Antigravity")
        self.assertIn(nonce, wrapped)
        self.assertIn("From: Antigravity", wrapped)
        extracted = extract_nonce(wrapped)
        self.assertEqual(extracted, nonce)

    def test_extract_verdict(self):
        text_pass = "Audit complete.\nAll tests pass.\n\nVERDICT: SIGN_OFF"
        self.assertEqual(extract_verdict(text_pass), "SIGN_OFF")

        text_fail = "Found issues.\nPlease fix P0.\n\nVERDICT: CHANGES_REQUESTED"
        self.assertEqual(extract_verdict(text_fail), "CHANGES_REQUESTED")

        text_none = "Just a regular comment.\nNo verdict here."
        self.assertIsNone(extract_verdict(text_none))

    def test_extract_verdict_quotes_prompt_takes_last(self):
        # P0-2 regression test: Quoting prompt spec earlier must not trigger false positive
        text = (
            'You instructed: "Please reply with VERDICT: SIGN_OFF or VERDICT: CHANGES_REQUESTED."\n'
            'We audited the code and found 2 critical bugs.\n\n'
            'VERDICT: CHANGES_REQUESTED'
        )
        self.assertEqual(extract_verdict(text), "CHANGES_REQUESTED")

    def test_extract_verdict_inline_unanchored(self):
        # Must be on its own line
        text = "I would normally write VERDICT: SIGN_OFF but here we have doubts."
        self.assertIsNone(extract_verdict(text))


if __name__ == "__main__":
    unittest.main()
