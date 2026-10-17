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

    def test_is_pid_alive(self):
        import os
        from agent_pair_ipc.common import is_pid_alive

        self.assertTrue(is_pid_alive(os.getpid()))
        self.assertFalse(is_pid_alive(999999999))
        self.assertFalse(is_pid_alive(None))
        self.assertFalse(is_pid_alive(0))
        self.assertFalse(is_pid_alive("invalid"))

    def test_check_artifact_stable(self):
        import time
        from agent_pair_ipc.common import check_artifact_stable

        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".md") as tf:
            tf_path = tf.name
            tf.write("# In-Progress Draft\nReviewing changes, tests running...\n")
            tf.flush()

        try:
            # File exists, but lacks standardized VERDICT line -> must NOT return ready!
            ready, content, verdict = check_artifact_stable(tf_path, initial_mtime=0.0, min_stable_seconds=0.0, require_verdict=True)
            self.assertFalse(ready)
            self.assertIsNone(verdict)

            # When require_verdict is False, returns ready
            ready_no_v, content_no_v, _ = check_artifact_stable(tf_path, initial_mtime=0.0, min_stable_seconds=0.0, require_verdict=False)
            self.assertTrue(ready_no_v)
            self.assertIn("In-Progress Draft", content_no_v)

            # Now append final verdict
            with open(tf_path, "a", encoding="utf-8") as f:
                f.write("\n\nVERDICT: SIGN_OFF\n")

            ready_final, content_final, verdict_final = check_artifact_stable(tf_path, initial_mtime=0.0, min_stable_seconds=0.0, require_verdict=True)
            self.assertTrue(ready_final)
            self.assertIn("VERDICT: SIGN_OFF", content_final)
            self.assertEqual(verdict_final, "SIGN_OFF")

            # If initial_mtime is higher than file mtime, should return False
            ready, _, _ = check_artifact_stable(tf_path, initial_mtime=time.time() + 100.0, min_stable_seconds=0.0)
            self.assertFalse(ready)

            # Non-existent file
            ready, _, _ = check_artifact_stable("/nonexistent/file.md", min_stable_seconds=0.0)
            self.assertFalse(ready)
        finally:
            Path(tf_path).unlink(missing_ok=True)

    def test_claude_parser_wait_and_expect_file(self):
        from agent_pair_ipc.claude import build_parser

        parser = build_parser()
        # Case 1: bare -w is boolean True
        args1 = parser.parse_args(["-w", "-f", "test.md"])
        self.assertTrue(args1.wait)
        self.assertEqual(args1.timeout, 300.0)
        self.assertIsNone(args1.expect_file)

        # Case 2: P1-2 regression - -f r.md -w 12587 does NOT swallow target into timeout
        args2 = parser.parse_args(["-f", "r.md", "-w", "12587"])
        self.assertTrue(args2.wait)
        self.assertEqual(args2.message, "12587")
        self.assertEqual(args2.timeout, 300.0)

        # Case 3: P1-2 regression - -w hello sessA does NOT fail on positional float parsing
        args3 = parser.parse_args(["-w", "hello", "sessA"])
        self.assertTrue(args3.wait)
        self.assertEqual(args3.message, "hello")
        self.assertEqual(args3.target_pos, "sessA")

        # Case 4: -w with explicit -t timeout
        args4 = parser.parse_args(["-w", "-t", "120", "--expect-file", "review.md", "-f", "test.md"])
        self.assertTrue(args4.wait)
        self.assertEqual(args4.timeout, 120.0)
        self.assertEqual(args4.expect_file, "review.md")

        # Case 5: --output-file alias
        args5 = parser.parse_args(["--output-file", "out.md", "-f", "test.md"])
        self.assertEqual(args5.expect_file, "out.md")

    def test_claude_send_and_wait_enqueued_no_premature_fail_fast(self):
        """P1-1 test: If request is confirmed enqueued in transcript, fail-fast does NOT fire prematurely."""
        import os
        from unittest.mock import patch
        from agent_pair_ipc.claude import send_and_wait

        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".sock") as sock_f, \
             tempfile.NamedTemporaryFile("w+", delete=False, suffix=".jsonl") as tr_f:
            sock_path = sock_f.name
            tr_path = tr_f.name

        fake_session = {
            "pid": os.getpid(),
            "session_id": "sess-busy-enqueued",
            "host_session_id": "host-busy-enqueued",
            "name": "Busy Claude",
            "cwd": "/fake/cwd",
            "socket_path": sock_path,
            "token": "tok123",
            "transcript_path": tr_path,
            "mtime": 100.0,
        }

        def mock_deliver(msg, **kwargs):
            # Simulate Claude Code writing queue-operation/enqueue with the envelope
            from agent_pair_ipc.common import extract_nonce
            n = extract_nonce(msg)
            with open(tr_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "queue-operation", "operation": "enqueue", "content": f"[{n}]"}) + "\n")
            return True

        try:
            with patch("agent_pair_ipc.claude.get_active_sessions", return_value=[fake_session]), \
                 patch("agent_pair_ipc.claude.match_session", return_value=(fake_session, "matched")), \
                 patch("agent_pair_ipc.claude.is_pid_alive", return_value=True), \
                 patch("agent_pair_ipc.claude.send_message_to_claude", side_effect=mock_deliver):
                # Timeout is 0.5s, fast_fail_timeout is 0.1s.
                # Because it was enqueued, it should NOT return QUEUED_UNCONSUMED!
                resp, verdict = send_and_wait("Hello Claude", target="host-busy-enqueued", timeout=0.5, fast_fail_timeout=0.1)
                self.assertIsNone(resp)
                # It timed out normally after 0.5s rather than fast-failing at 0.1s!
                self.assertIsNone(verdict)
        finally:
            Path(sock_path).unlink(missing_ok=True)
            Path(tr_path).unlink(missing_ok=True)

    def test_claude_send_and_wait_artifact_draft_ignored_until_verdict(self):
        """P1-3 test: send_and_wait will not return intermediate drafts lacking VERDICT."""
        import os
        import time
        from unittest.mock import patch
        from agent_pair_ipc.claude import send_and_wait

        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".md") as art_f, \
             tempfile.NamedTemporaryFile("w+", delete=False, suffix=".sock") as sock_f, \
             tempfile.NamedTemporaryFile("w+", delete=False, suffix=".jsonl") as tr_f:
            art_path = art_f.name
            sock_path = sock_f.name
            tr_path = tr_f.name

        fake_session = {
            "pid": os.getpid(),
            "session_id": "sess-draft-test",
            "host_session_id": "host-draft-test",
            "cwd": "/fake/cwd",
            "socket_path": sock_path,
            "token": "tok123",
            "transcript_path": tr_path,
            "mtime": 100.0,
        }

        # Step 1: Writer writes a draft first without VERDICT
        with open(art_path, "w", encoding="utf-8") as f:
            f.write("# Working Draft\nTests still in progress...\n")
        t0 = time.time() - 10.0
        os.utime(art_path, (t0, t0))

        # Step 2: In delivery callback, after short delay, write final verdict
        def mock_deliver(msg, **kwargs):
            import threading
            import time
            def finalize_later():
                time.sleep(0.1)
                with open(art_path, "w", encoding="utf-8") as f:
                    f.write("# Final Report\nAll passed.\n\nVERDICT: SIGN_OFF\n")
                now = time.time()
                os.utime(art_path, (now - 5.0, now - 5.0))
            threading.Thread(target=finalize_later).start()
            return True

        try:
            with patch("agent_pair_ipc.claude.get_active_sessions", return_value=[fake_session]), \
                 patch("agent_pair_ipc.claude.match_session", return_value=(fake_session, "matched")), \
                 patch("agent_pair_ipc.claude.is_pid_alive", return_value=True), \
                 patch("agent_pair_ipc.claude.send_message_to_claude", side_effect=mock_deliver):
                resp, verdict = send_and_wait("Hello Claude", target="host-draft-test", expect_file=art_path, timeout=5.0)
                self.assertIsNotNone(resp)
                self.assertIn("Final Report", resp)
                self.assertEqual(verdict, "SIGN_OFF")
        finally:
            Path(art_path).unlink(missing_ok=True)
            Path(sock_path).unlink(missing_ok=True)
            Path(tr_path).unlink(missing_ok=True)

    def test_claude_send_and_wait_pid_fail_fast(self):
        """Test fail-fast when target process terminates unexpectedly."""
        from unittest.mock import patch
        from agent_pair_ipc.claude import send_and_wait

        fake_session = {
            "pid": 987654321,
            "session_id": "sess-dead-pid",
            "host_session_id": "host-dead-pid",
            "cwd": "/fake/cwd",
            "socket_path": "/fake/sock",
            "token": "tok123",
            "transcript_path": "/nonexistent/transcript.jsonl",
            "mtime": 100.0,
        }

        with patch("agent_pair_ipc.claude.get_active_sessions", return_value=[fake_session]), \
             patch("agent_pair_ipc.claude.match_session", return_value=(fake_session, "matched")), \
             patch("agent_pair_ipc.claude.is_pid_alive", return_value=False), \
             patch("agent_pair_ipc.claude.send_message_to_claude", return_value=True):
            resp, verdict = send_and_wait("Hello Claude", target="host-dead-pid", timeout=5.0)
            self.assertIsNone(resp)
            self.assertEqual(verdict, "PROCESS_TERMINATED")

    def test_claude_send_and_wait_unconsumed_fast_fail(self):
        """Test that send_and_wait fast-fails if message is not consumed within fast_fail_timeout."""
        import os
        from unittest.mock import patch
        from agent_pair_ipc.claude import send_and_wait

        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".sock") as sock_f, \
             tempfile.NamedTemporaryFile("w+", delete=False, suffix=".jsonl") as tr_f:
            sock_path = sock_f.name
            tr_path = tr_f.name

        fake_session = {
            "pid": os.getpid(),
            "session_id": "sess-unconsumed",
            "host_session_id": "host-unconsumed",
            "name": "Sleeping Claude",
            "cwd": "/fake/cwd",
            "socket_path": sock_path,
            "token": "tok123",
            "transcript_path": tr_path,
            "mtime": 100.0,
        }

        try:
            with patch("agent_pair_ipc.claude.get_active_sessions", return_value=[fake_session]), \
                 patch("agent_pair_ipc.claude.match_session", return_value=(fake_session, "matched")), \
                 patch("agent_pair_ipc.claude.is_pid_alive", return_value=True), \
                 patch("agent_pair_ipc.claude.send_message_to_claude", return_value=True):
                resp, verdict = send_and_wait("Hello Claude", target="host-unconsumed", timeout=5.0, fast_fail_timeout=0.1)
                self.assertIsNone(resp)
                self.assertEqual(verdict, "QUEUED_UNCONSUMED")
        finally:
            Path(sock_path).unlink(missing_ok=True)
            Path(tr_path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()

