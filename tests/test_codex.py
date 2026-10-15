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
            matched = match_session(target=None, candidates=candidates, allow_cwd_fallback=True)
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
            # Without pick_latest: ambiguous -> returns None to refuse guessing
            matched = match_session(target=None, candidates=candidates, allow_cwd_fallback=True, pick_latest=False)
            self.assertIsNone(matched)

            # With pick_latest: picks sess_new_002 (mtime 2000.0)
            matched = match_session(target=None, candidates=candidates, allow_cwd_fallback=True, pick_latest=True)
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
            with patch("agent_pair_ipc.codex.match_session", return_value=session_mock), \
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


if __name__ == "__main__":
    unittest.main()
