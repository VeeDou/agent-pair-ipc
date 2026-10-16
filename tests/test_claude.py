"""
Unit tests for agent_pair_ipc.claude
"""

import json
import re
import tempfile
import unittest
from pathlib import Path
from agent_pair_ipc.claude import (
    find_transcript_path,
    match_session,
    read_transcript_messages,
)


class TestClaude(unittest.TestCase):
    def test_find_transcript_path_sanitization(self):
        cwd = "/Users/developer/projects/AI产品经理"
        sanitized = re.sub(r"[^a-zA-Z0-9]", "-", cwd)
        self.assertEqual(sanitized, "-Users-developer-projects-AI----")

    def test_read_transcript_messages_filters_tool_use(self):
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".jsonl") as tf:
            tf_path = tf.name
            tf.write(json.dumps({"type": "user", "message": {"role": "user", "content": "hello"}}) + "\n")
            tf.write(
                json.dumps({
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "stop_reason": "tool_use",
                        "content": [{"type": "tool_use", "name": "bash"}],
                    },
                })
                + "\n"
            )
            tf.write(
                json.dumps({
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "VERDICT: SIGN_OFF"}],
                    },
                })
                + "\n"
            )

        try:
            turns = read_transcript_messages(tf_path, last_n=5, only_final=True)
            self.assertEqual(len(turns), 1)
            self.assertIn("VERDICT: SIGN_OFF", turns[0]["text"])
            self.assertEqual(turns[0]["stop_reason"], "end_turn")
        finally:
            Path(tf_path).unlink(missing_ok=True)

    def test_incremental_reading_split_across_chunks(self):
        # P0 regression test: Real multi-byte UTF-8 split across chunk boundaries must not duplicate or corrupt
        line1 = json.dumps({"text": "测试多字节中文字符边界"}, ensure_ascii=False).encode("utf-8") + b"\n"
        line2 = json.dumps({"text": "第二行完全独立数据"}, ensure_ascii=False).encode("utf-8") + b"\n"

        # Split line 1 across arbitrary byte boundary inside a Chinese character
        partA = line1[:19]
        partB = line1[19:] + line2

        current_offset = 0
        byte_buffer = b""
        decoded_messages = []

        # Chunk 1 (partial line)
        current_offset += len(partA)
        combined = byte_buffer + partA
        raw_lines = combined.split(b"\n")
        byte_buffer = raw_lines[-1]
        for rline in raw_lines[:-1]:
            if rline.strip():
                decoded_messages.append(json.loads(rline.decode("utf-8")))

        self.assertEqual(len(decoded_messages), 0)
        self.assertEqual(byte_buffer, partA)

        # Chunk 2 (remainder of line 1 + line 2)
        current_offset += len(partB)
        combined = byte_buffer + partB
        raw_lines = combined.split(b"\n")
        byte_buffer = raw_lines[-1]
        for rline in raw_lines[:-1]:
            if rline.strip():
                decoded_messages.append(json.loads(rline.decode("utf-8")))

        # Reconstructed lines must parse without duplicate corruption or decode errors
        self.assertEqual(len(decoded_messages), 2)
        self.assertEqual(decoded_messages[0]["text"], "测试多字节中文字符边界")
        self.assertEqual(decoded_messages[1]["text"], "第二行完全独立数据")
        self.assertEqual(byte_buffer, b"")

    def test_match_session_no_cwd_match_returns_none(self):
        # Must not guess an arbitrary project session
        candidates = [
            {
                "pid": 1234,
                "session_id": "sess_other",
                "host_session_id": "host_other",
                "name": "other_project",
                "cwd": "/some/completely/unrelated/path",
                "kind": "interactive",
            }
        ]
        matched, status = match_session(target=None, candidates=candidates)
        self.assertIsNone(matched)
        self.assertEqual(status, "none")

    def test_match_session_ambiguity_and_pick_latest(self):
        import os
        candidates = [
            {
                "pid": 1001,
                "session_id": "sess_old",
                "host_session_id": "host_old",
                "name": "Old Claude Session",
                "cwd": os.getcwd(),
                "mtime": 100.0,
            },
            {
                "pid": 1002,
                "session_id": "sess_new",
                "host_session_id": "host_new",
                "name": "New Claude Session",
                "cwd": os.getcwd(),
                "mtime": 200.0,
            },
        ]
        # Strict disambiguation: without pick_latest, returns ambiguous
        matched, status = match_session(target=None, candidates=candidates, pick_latest=False)
        self.assertIsNone(matched)
        self.assertEqual(status, "ambiguous")

        # With pick_latest: picks sess_new
        matched, status = match_session(target=None, candidates=candidates, pick_latest=True)
        self.assertEqual(status, "matched")
        self.assertIsNotNone(matched)
        self.assertEqual(matched["session_id"], "sess_new")

    def test_claude_never_binds_raw_pid(self):
        """P1 parity: await_new_session must bind hostSessionId or sessionId, never raw PID."""
        import os
        from unittest.mock import patch
        from agent_pair_ipc.claude import await_new_session

        fake_session = {
            "pid": 99999,
            "session_id": "uuid-sess-1234",
            "host_session_id": "local_mock_session_id_12345",
            "cwd": os.getcwd(),
        }

        with patch("agent_pair_ipc.claude.get_active_sessions", side_effect=[[], [fake_session]]), \
             patch("agent_pair_ipc.claude.save_project_binding") as mock_save, \
             patch("sys.stderr.isatty", return_value=True):
            res = await_new_session(timeout=2.0)
            self.assertIsNotNone(res)
            self.assertTrue(mock_save.called)
            # Bound value must be host_session_id, NOT raw PID 99999
            agent_arg, bound_arg = mock_save.call_args[0][:2]
            self.assertEqual(agent_arg, "claude")
            self.assertEqual(bound_arg, "local_mock_session_id_12345")
            self.assertNotEqual(bound_arg, "99999")

    def test_claude_non_tty_auto_await_exits_with_needs_new_thread(self):
        """P1 parity: In non-TTY environment, auto-await exits with code 5."""
        from unittest.mock import patch
        from agent_pair_ipc.common import EXIT_NEEDS_NEW_THREAD, NeedsNewSessionError
        from agent_pair_ipc.claude import await_new_session, main, send_and_wait

        with patch("sys.stderr.isatty", return_value=False):
            with self.assertRaises(NeedsNewSessionError):
                await_new_session(timeout=5.0, require_tty=True)

            with patch("agent_pair_ipc.claude.get_active_sessions", return_value=[]), \
                 patch("agent_pair_ipc.claude.resolve_target_session", return_value=(None, "auto")):
                resp, verdict = send_and_wait("Hello", target=None, auto_await_new=True)
                self.assertIsNone(resp)
                self.assertEqual(verdict, "NEEDS_NEW_THREAD")

            with patch("agent_pair_ipc.claude.get_active_sessions", return_value=[]), \
                 patch("agent_pair_ipc.claude.resolve_target_session", return_value=(None, "auto")):
                code = main(["-w", "-m", "Hello"])
                self.assertEqual(code, EXIT_NEEDS_NEW_THREAD)

    def test_claude_send_file(self):
        import tempfile
        from unittest.mock import patch
        from agent_pair_ipc.claude import send_file

        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".md") as tf:
            tf.write("# Test Review File\nWith code block `let x = 1;`")
            tf_path = tf.name

        try:
            with patch("agent_pair_ipc.claude.send_message_to_claude", return_value=True) as mock_send:
                ok = send_file(tf_path, target="mock_sess")
                self.assertTrue(ok)
                self.assertTrue(mock_send.called)
                sent_msg = mock_send.call_args[0][0]
                self.assertIn("# Test Review File", sent_msg)
                self.assertIn("let x = 1;", sent_msg)
        finally:
            Path(tf_path).unlink(missing_ok=True)

    def test_claude_dual_channel_reply_socket(self):
        """Optimization 2 test: Ephemeral reply socket delivers response directly without transcript."""
        import json
        import socket
        from unittest.mock import patch
        from agent_pair_ipc.claude import send_and_wait
        from agent_pair_ipc.common import extract_peer_envelope

        fake_session = {
            "pid": 55555,
            "session_id": "sess-socket-reply",
            "host_session_id": "host-socket-reply",
            "cwd": "/fake/cwd",
            "socket_path": "/fake/sock",
            "token": "tok123",
            "transcript_path": "/nonexistent/transcript.jsonl",
            "mtime": 100.0,
        }

        def mock_deliver(msg, **kwargs):
            # Inspect envelope and reply directly via the advertised reply_socket
            parsed = extract_peer_envelope(msg)
            reply_sock = parsed.get("reply_socket")
            if reply_sock:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(reply_sock)
                payload = json.dumps({
                    "type": "response",
                    "content": "Peer review approved directly via socket.\n\nVERDICT: SIGN_OFF",
                    "verdict": "SIGN_OFF",
                })
                s.sendall(payload.encode("utf-8"))
                s.close()
            return True

        with patch("agent_pair_ipc.claude.get_active_sessions", return_value=[fake_session]), \
             patch("agent_pair_ipc.claude.match_session", return_value=(fake_session, "matched")), \
             patch("agent_pair_ipc.claude.send_message_to_claude", side_effect=mock_deliver):
            resp, verdict = send_and_wait("Hello Claude", target="host-socket-reply", timeout=5.0)
            self.assertIsNotNone(resp)
            self.assertIn("Peer review approved directly via socket", resp)
            self.assertEqual(verdict, "SIGN_OFF")


if __name__ == "__main__":
    unittest.main()
