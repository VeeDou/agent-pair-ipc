"""
Unit tests for agent_pair_ipc.codex
"""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_pair_ipc.codex import (
    match_session,
    read_transcript_messages,
    resolve_target_session,
    send_and_wait,
    send_message_to_codex,
)
from agent_pair_ipc.common import save_project_binding


class TestCodex(unittest.TestCase):
    def test_codex_read_transcript_messages(self):
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".jsonl") as tf:
            tf_path = tf.name
            tf.write(json.dumps({"type": "session_meta", "payload": {"cwd": "/tmp/test"}}) + "\n")
            tf.write(json.dumps({"type": "response_item", "payload": {"type": "custom_tool_call", "name": "bash"}}) + "\n")
            tf.write(
                json.dumps({
                    "type": "event_msg",
                    "timestamp": "2026-10-15T12:00:00Z",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "turn_123",
                        "last_agent_message": "VERDICT: SIGN_OFF\nAll gate tests verified.",
                    },
                })
                + "\n"
            )

        try:
            turns = read_transcript_messages(tf_path, last_n=5, only_final=True)
            self.assertEqual(len(turns), 1)
            self.assertIn("VERDICT: SIGN_OFF", turns[0]["text"])
            self.assertEqual(turns[0]["turn_id"], "turn_123")
        finally:
            Path(tf_path).unlink(missing_ok=True)

    def test_resolve_target_session_precedence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            save_project_binding("codex", "bound_sess_777", cwd=tmpdir)

            # Case 1: All unset -> uses bindings
            target, src = resolve_target_session(target=None, cwd=tmpdir)
            self.assertEqual(target, "bound_sess_777")
            self.assertEqual(src, "bindings_file")

            # Case 2: Env var set -> env var overrides bindings
            with patch.dict(os.environ, {"AGENT_PAIR_CODEX_THREAD": "env_sess_888"}):
                target, src = resolve_target_session(target=None, cwd=tmpdir)
                self.assertEqual(target, "env_sess_888")
                self.assertEqual(src, "env_var")

                # Case 3: Explicit flag set -> overrides env var and bindings
                target, src = resolve_target_session(target="flag_sess_999", cwd=tmpdir)
                self.assertEqual(target, "flag_sess_999")
                self.assertEqual(src, "cli_flag")

    def test_match_session_excludes_exec_source(self):
        cur_cwd = os.getcwd()
        candidates = [
            {
                "id": "sess_cli_001",
                "name": "CLI Task",
                "cwd": cur_cwd,
                "source": "exec",
                "originator": "codex_exec",
                "mtime": 100.0,
                "jsonl_path": "/fake/path1.jsonl",
            },
            {
                "id": "sess_gui_002",
                "name": "Desktop Thread",
                "cwd": cur_cwd,
                "source": "vscode",
                "originator": "Codex Desktop",
                "mtime": 200.0,
                "jsonl_path": "/fake/path2.jsonl",
            },
        ]

        with patch("agent_pair_ipc.codex.enrich_session_info", side_effect=lambda x: x):
            matched, status = match_session(target=None, candidates=candidates, allow_cwd_fallback=True)
            self.assertEqual(status, "matched")
            self.assertIsNotNone(matched)
            self.assertEqual(matched["id"], "sess_gui_002")

    def test_match_session_ambiguous_and_pick_latest(self):
        cur_cwd = os.getcwd()
        candidates = [
            {
                "id": "sess_old_001",
                "name": "Yesterday Thread",
                "cwd": cur_cwd,
                "source": "vscode",
                "originator": "Codex Desktop",
                "mtime": 1000.0,
                "jsonl_path": "/fake/old.jsonl",
            },
            {
                "id": "sess_new_002",
                "name": "Today Thread",
                "cwd": cur_cwd,
                "source": "vscode",
                "originator": "Codex Desktop",
                "mtime": 2000.0,
                "jsonl_path": "/fake/new.jsonl",
            },
        ]

        with patch("agent_pair_ipc.codex.enrich_session_info", side_effect=lambda x: x):
            # Without pick_latest: ambiguous -> returns (None, "ambiguous") to refuse guessing
            matched, status = match_session(target=None, candidates=candidates, allow_cwd_fallback=True, pick_latest=False)
            self.assertIsNone(matched)
            self.assertEqual(status, "ambiguous")

            # With pick_latest: picks sess_new_002 (mtime 2000.0)
            matched, status = match_session(target=None, candidates=candidates, allow_cwd_fallback=True, pick_latest=True)
            self.assertEqual(status, "matched")
            self.assertIsNotNone(matched)
            self.assertEqual(matched["id"], "sess_new_002")

    def test_fast_fail_queued_unconsumed(self):
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".jsonl") as tf:
            tf_path = tf.name
            tf.write(json.dumps({"type": "session_meta", "payload": {"id": "sess_fast_fail", "cwd": os.getcwd()}}) + "\n")

        session_mock = {
            "id": "sess_fast_fail",
            "name": "Sleeping Thread",
            "jsonl_path": tf_path,
            "cwd": os.getcwd(),
            "source": "vscode",
            "originator": "Codex Desktop",
            "mtime": time.time(),
        }

        try:
            with patch("agent_pair_ipc.codex.match_session", return_value=(session_mock, "matched")), \
                 patch("agent_pair_ipc.codex.send_message_to_codex", return_value=True):
                # Run with 0.2s fast fail timeout
                resp, verdict = send_and_wait(
                    message="Ping",
                    target="sess_fast_fail",
                    timeout=5.0,
                    fast_fail_timeout=0.2,
                    show_progress=False,
                )
                self.assertIsNone(resp)
                self.assertEqual(verdict, "QUEUED_UNCONSUMED")
        finally:
            Path(tf_path).unlink(missing_ok=True)

    def test_new_task_auto_await_behavior(self):
        with patch("agent_pair_ipc.codex.match_session", return_value=(None, "none")), \
             patch("agent_pair_ipc.codex.await_new_thread") as mock_await, \
             patch("agent_pair_ipc.codex.resolve_target_session", return_value=(None, "auto")):
            mock_await.return_value = {
                "id": "new_detected_123",
                "name": "New Task",
                "cwd": "/tmp/new-task",
                "jsonl_path": "/fake/new.jsonl",
            }
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stderr = ""
                # auto_await_new=True triggers await_new_thread and succeeds
                ok = send_message_to_codex("Hello", target=None, auto_await_new=True)
                self.assertTrue(ok)
                self.assertTrue(mock_await.called)

            mock_await.reset_mock()
            # auto_await_new=False does not trigger await_new_thread and returns False
            ok = send_message_to_codex("Hello", target=None, auto_await_new=False)
            self.assertFalse(ok)
            self.assertFalse(mock_await.called)

    def test_ambiguity_never_falls_through_to_await_new_thread(self):
        """P0 regression: When match_session is ambiguous, NEVER call await_new_thread."""
        with patch("agent_pair_ipc.codex.match_session", return_value=(None, "ambiguous")), \
             patch("agent_pair_ipc.codex.await_new_thread") as mock_await:
            ok = send_message_to_codex("Hello", target=None, auto_await_new=True)
            self.assertFalse(ok)
            self.assertFalse(mock_await.called)

            resp, verdict = send_and_wait("Hello", target=None, auto_await_new=True)
            self.assertIsNone(resp)
            self.assertIsNone(verdict)
            self.assertFalse(mock_await.called)

    def test_non_tty_auto_await_exits_with_needs_new_thread(self):
        """P1 regression: In non-TTY environments, auto-await must not block; exits with code 5."""
        from agent_pair_ipc.common import EXIT_NEEDS_NEW_THREAD, NeedsNewSessionError
        from agent_pair_ipc.codex import await_new_thread, main

        with patch("sys.stderr.isatty", return_value=False):
            # await_new_thread raises NeedsNewSessionError
            with self.assertRaises(NeedsNewSessionError):
                await_new_thread(timeout=5.0, require_tty=True)

            # send_and_wait returns verdict NEEDS_NEW_THREAD
            with patch("agent_pair_ipc.codex.match_session", return_value=(None, "none")), \
                 patch("agent_pair_ipc.codex.resolve_target_session", return_value=(None, "auto")):
                resp, verdict = send_and_wait("Hello", target=None, auto_await_new=True)
                self.assertIsNone(resp)
                self.assertEqual(verdict, "NEEDS_NEW_THREAD")

            # CLI main exits with code 5 (EXIT_NEEDS_NEW_THREAD)
            with patch("agent_pair_ipc.codex.match_session", return_value=(None, "none")), \
                 patch("agent_pair_ipc.codex.resolve_target_session", return_value=(None, "auto")):
                exit_code = main(["-w", "-m", "Hello"])
                self.assertEqual(exit_code, EXIT_NEEDS_NEW_THREAD)

    def test_await_new_thread_snapshot_ignores_preexisting_and_touched_files(self):
        """P0 regression: await_new_thread must snapshot initial sessions and ignore touched/modified old files."""
        import datetime
        from agent_pair_ipc.codex import await_new_thread

        with tempfile.TemporaryDirectory() as tmp_home:
            today_str = datetime.date.today().strftime("%Y/%m/%d")
            today_dir = Path(tmp_home) / ".codex" / "sessions" / today_str
            today_dir.mkdir(parents=True)
            tmp_proj = str(Path(tmp_home) / "mock_project")
            os.makedirs(tmp_proj, exist_ok=True)

            # Pre-existing old file
            old_file = today_dir / "rollout-2026-10-15T10-00-00-old_sess_001.jsonl"
            with open(old_file, "w") as f:
                f.write(json.dumps({"type": "session_meta", "payload": {"id": "old_sess_001", "cwd": tmp_proj, "source": "vscode"}}) + "\n")

            with patch("pathlib.Path.home", return_value=Path(tmp_home)), \
                 patch("sys.stderr.isatty", return_value=True):
                # When await_new_thread starts, touch the old file (simulating adversarial file touch)
                os.utime(old_file, None)

                # Calling await_new_thread with small timeout: must NOT return old_sess_001!
                res = await_new_thread(timeout=0.3, cwd=tmp_proj, require_tty=True)
                self.assertIsNone(res)

                # Now simulate user clicking 'New Thread' 0.2s after watch starts
                import threading
                def create_new_thread_later():
                    time.sleep(0.2)
                    new_file = today_dir / "rollout-2026-10-15T12-00-00-new_sess_002.jsonl"
                    with open(new_file, "w") as f:
                        f.write(json.dumps({"type": "session_meta", "payload": {"id": "new_sess_002", "cwd": tmp_proj, "source": "vscode"}}) + "\n")

                t = threading.Thread(target=create_new_thread_later)
                t.start()
                res = await_new_thread(timeout=2.0, cwd=tmp_proj, require_tty=True)
                t.join()
                self.assertIsNotNone(res)
                self.assertEqual(res["id"], "new_sess_002")


if __name__ == "__main__":
    unittest.main()
