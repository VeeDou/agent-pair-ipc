"""
Unit tests for agent_pair_ipc.common
"""

import tempfile
import unittest
from pathlib import Path
from agent_pair_ipc.common import (
    TailReader,
    attach_request_header,
    extract_nonce,
    extract_verdict,
    generate_nonce,
    load_project_bindings,
    resolve_message_payload,
    save_project_binding,
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

    def test_tail_reader_incremental_and_utf8_split(self):
        with tempfile.NamedTemporaryFile("wb", delete=False) as tf:
            tf_path = tf.name

        try:
            reader = TailReader(tf_path, start_offset=0)
            self.assertEqual(reader.read_new_lines(), [])

            # Write first line
            with open(tf_path, "ab") as f:
                f.write(b'{"turn": 1, "msg": "First"}\n')

            lines = reader.read_new_lines()
            self.assertEqual(len(lines), 1)
            self.assertIn("First", lines[0])

            # Write partial UTF-8 multi-byte chunk (Chinese character: 3 bytes: e6 b5 8b)
            chinese_char = "测".encode("utf-8")
            with open(tf_path, "ab") as f:
                f.write(b'{"turn": 2, "msg": "' + chinese_char[:1])

            # Incomplete line should not be returned yet
            lines = reader.read_new_lines()
            self.assertEqual(len(lines), 0)

            # Write remaining bytes and complete the line
            with open(tf_path, "ab") as f:
                f.write(chinese_char[1:] + b'"}\n')

            lines = reader.read_new_lines()
            self.assertEqual(len(lines), 1)
            self.assertIn("测", lines[0])
        finally:
            Path(tf_path).unlink(missing_ok=True)

    def test_project_bindings_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bindings = load_project_bindings(tmpdir)
            self.assertEqual(bindings, {})

            save_project_binding("codex", "sess_codex_123", cwd=tmpdir)
            save_project_binding("claude", "sess_claude_456", cwd=tmpdir)

            loaded = load_project_bindings(tmpdir)
            self.assertEqual(loaded.get("codex"), "sess_codex_123")
            self.assertEqual(loaded.get("claude"), "sess_claude_456")

    def test_bindings_search_stops_at_git_boundary(self):
        from agent_pair_ipc.common import find_project_bindings_file, unbind_project_binding
        with tempfile.TemporaryDirectory() as outer:
            outer_p = Path(outer)
            # Create a binding in outer directory
            save_project_binding("codex", "outer_sess", cwd=outer)

            # Create an inner repository with a .git boundary
            repo_root = outer_p / "my_project"
            repo_root.mkdir()
            (repo_root / ".git").mkdir()

            sub_dir = repo_root / "src" / "pkg"
            sub_dir.mkdir(parents=True)

            # Searching from sub_dir must NOT escape my_project past .git to outer directory!
            found = find_project_bindings_file(cwd=str(sub_dir))
            self.assertIsNone(found)

            # Once a binding is saved in repo_root, searching from sub_dir finds it
            save_project_binding("codex", "inner_sess", cwd=str(sub_dir))
            found = find_project_bindings_file(cwd=str(sub_dir))
            self.assertIsNotNone(found)
            self.assertEqual(found.resolve(), (repo_root / ".agent-pair" / "bindings.json").resolve())

            # Test unbind
            ok = unbind_project_binding("codex", cwd=str(sub_dir))
            self.assertTrue(ok)
            loaded = load_project_bindings(cwd=str(sub_dir))
            self.assertNotIn("codex", loaded)


if __name__ == "__main__":
    unittest.main()
