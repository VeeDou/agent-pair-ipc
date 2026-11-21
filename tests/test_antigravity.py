"""
Unit tests for agent_pair_ipc.antigravity
"""

import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_pair_ipc.antigravity import (
    build_agentapi_env,
    discover_active_daemon,
    get_all_sessions,
    main as antigravity_main,
    match_session,
    read_transcript_messages,
    resolve_target_session,
    send_and_wait,
    send_message_to_antigravity,
)
from agent_pair_ipc.common import (
    EXIT_NEEDS_NEW_THREAD,
    EXIT_QUEUED_UNCONSUMED,
    EXIT_SIGN_OFF,
    save_project_binding,
    send_reply,
)


def _create_test_db(db_path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE conversation_summaries (
            conversation_id text PRIMARY KEY,
            title text NOT NULL DEFAULT "",
            preview text NOT NULL DEFAULT "",
            step_count integer NOT NULL DEFAULT 0,
            last_modified_time datetime NOT NULL,
            workspace_uris text NOT NULL,
            status text NOT NULL DEFAULT "",
            source text NOT NULL DEFAULT "",
            project_id text NOT NULL DEFAULT "",
            agent_name text NOT NULL DEFAULT "",
            parent_conversation_id text NOT NULL DEFAULT "",
            nesting_depth integer NOT NULL DEFAULT 0,
            not_fully_idle numeric NOT NULL DEFAULT false,
            killed numeric NOT NULL DEFAULT false,
            last_user_input_time datetime NOT NULL DEFAULT ""
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO conversation_summaries (
            conversation_id, title, workspace_uris, status, not_fully_idle, killed,
            step_count, last_modified_time, last_user_input_time, project_id,
            parent_conversation_id, nesting_depth
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    conn.close()


class TestAntigravity(unittest.TestCase):
    def test_get_all_sessions_filters_killed_and_subagents(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app_dir = Path(tmpdir)
            db_path = app_dir / "conversation_summaries.db"
            _create_test_db(
                db_path,
                [
                    (
                        "conv-top-idle",
                        "Main Review",
                        json.dumps(["file:///Users/test/AI%E4%BA%A7%E5%93%81"]),
                        "CASCADE_RUN_STATUS_IDLE",
                        0,
                        0,
                        4,
                        "2026-11-12 08:00:00+00:00",
                        "2026-11-12 08:00:00+00:00",
                        "proj-1",
                        "",
                        0,
                    ),
                    (
                        "conv-top-busy",
                        "Busy Session",
                        json.dumps(["file:///Users/test/repo"]),
                        "CASCADE_RUN_STATUS_RUNNING",
                        1,
                        0,
                        10,
                        "2026-11-12 09:00:00+00:00",
                        "2026-11-12 09:00:00+00:00",
                        "proj-2",
                        "",
                        0,
                    ),
                    (
                        "conv-subagent",
                        "Background Subagent",
                        json.dumps(["file:///Users/test/repo"]),
                        "CASCADE_RUN_STATUS_IDLE",
                        0,
                        0,
                        2,
                        "2026-11-12 09:30:00+00:00",
                        "2026-11-12 09:30:00+00:00",
                        "proj-2",
                        "conv-top-busy",
                        1,
                    ),
                    (
                        "conv-killed",
                        "Killed Session",
                        json.dumps(["file:///Users/test/repo"]),
                        "CASCADE_RUN_STATUS_IDLE",
                        0,
                        1,
                        1,
                        "2026-11-12 09:45:00+00:00",
                        "2026-11-12 09:45:00+00:00",
                        "proj-2",
                        "",
                        0,
                    ),
                ],
            )
            with patch.dict(os.environ, {"ANTIGRAVITY_APP_DATA_DIR": str(app_dir)}):
                sessions = get_all_sessions(limit=20)
            self.assertEqual([s["id"] for s in sessions], ["conv-top-busy", "conv-top-idle"])
            self.assertEqual(sessions[0]["status"], "busy")
            self.assertEqual(sessions[1]["status"], "idle")
            self.assertEqual(sessions[1]["cwd"], "/Users/test/AI产品")

    def test_discover_active_daemon_and_env_stripping(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            daemon_dir = Path(tmpdir)
            (daemon_dir / "hub_11111.json").write_text(
                json.dumps({"pid": 99999999, "httpPort": 11111, "csrfToken": "dead-token"}),
                encoding="utf-8",
            )
            alive_file = daemon_dir / "hub_50325.json"
            alive_file.write_text(
                json.dumps({"pid": os.getpid(), "httpPort": 50325, "csrfToken": "live-token"}),
                encoding="utf-8",
            )

            with patch("agent_pair_ipc.antigravity.is_pid_alive", side_effect=lambda p: p == os.getpid()):
                daemon = discover_active_daemon(daemon_dir)
            self.assertIsNotNone(daemon)
            self.assertEqual(daemon["httpPort"], 50325)
            self.assertEqual(daemon["csrfToken"], "live-token")

            with patch.dict(
                os.environ,
                {
                    "ANTIGRAVITY_PROJECT_ID": "caller-proj",
                    "ANTIGRAVITY_CONVERSATION_ID": "caller-conv",
                    "ANTIGRAVITY_TRAJECTORY_ID": "caller-traj",
                    "ANTIGRAVITY_SOURCE_METADATA": "{}",
                },
            ):
                env = build_agentapi_env(daemon)

            self.assertEqual(env["ANTIGRAVITY_LS_ADDRESS"], "localhost:50325")
            self.assertEqual(env["ANTIGRAVITY_CSRF_TOKEN"], "live-token")
            for stripped in (
                "ANTIGRAVITY_PROJECT_ID",
                "ANTIGRAVITY_CONVERSATION_ID",
                "ANTIGRAVITY_TRAJECTORY_ID",
                "ANTIGRAVITY_SOURCE_METADATA",
            ):
                self.assertNotIn(stripped, env)

    def test_resolve_target_session_precedence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            save_project_binding("antigravity", "bound_conv_111", cwd=tmpdir)

            target, src = resolve_target_session(target=None, cwd=tmpdir)
            self.assertEqual((target, src), ("bound_conv_111", "bindings_file"))

            with patch.dict(os.environ, {"AGENT_PAIR_ANTIGRAVITY_SESSION": "env_conv_222"}):
                target, src = resolve_target_session(target=None, cwd=tmpdir)
                self.assertEqual((target, src), ("env_conv_222", "env_var"))

                target, src = resolve_target_session(target="flag_conv_333", cwd=tmpdir)
                self.assertEqual((target, src), ("flag_conv_333", "cli_flag"))

    def test_match_session_ambiguous_and_pick_latest(self):
        cur_cwd = os.getcwd()
        candidates = [
            {"id": "conv_old_0001", "name": "Old Conv", "cwd": cur_cwd, "cwds": [cur_cwd], "mtime": 100.0, "status": "idle"},
            {"id": "conv_new_0002", "name": "New Conv", "cwd": cur_cwd, "cwds": [cur_cwd], "mtime": 200.0, "status": "idle"},
        ]
        matched, status = match_session(target=None, candidates=candidates, pick_latest=False)
        self.assertIsNone(matched)
        self.assertEqual(status, "ambiguous")

        matched, status = match_session(target=None, candidates=candidates, pick_latest=True)
        self.assertEqual(status, "matched")
        self.assertEqual(matched["id"], "conv_new_0002")

    def test_match_session_path_guard_cross_project_and_dirty_binding(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            foreign = os.path.realpath(tmpdir)
            candidates = [
                {
                    "id": "conv_foreign_01",
                    "name": "Foreign Task",
                    "cwd": foreign,
                    "cwds": [foreign],
                    "mtime": time.time(),
                    "status": "idle",
                },
                {
                    "id": "conv_local_02",
                    "name": "Local Task",
                    "cwd": os.getcwd(),
                    "cwds": [os.getcwd()],
                    "mtime": time.time(),
                    "status": "idle",
                },
            ]

            matched, status = match_session(target="/tmp/some_path", candidates=candidates)
            self.assertIsNone(matched)
            self.assertEqual(status, "invalid_target")

            matched, status = match_session(target="conv_foreign_01", candidates=candidates, allow_cross_project=False)
            self.assertIsNone(matched)
            self.assertEqual(status, "cross_project_rejected")

            matched, status = match_session(target="conv_foreign_01", candidates=candidates, allow_cross_project=foreign)
            self.assertEqual(status, "matched")
            self.assertEqual(matched["id"], "conv_foreign_01")

            # Dirty env binding should fall back to current project session
            with patch.dict(os.environ, {"AGENT_PAIR_ANTIGRAVITY_SESSION": "conv_foreign_01"}):
                matched, status = match_session(target=None, candidates=candidates)
                self.assertEqual(status, "matched")
                self.assertEqual(matched["id"], "conv_local_02")

    def test_read_transcript_messages_with_truncated_full_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logs_dir = Path(tmpdir)
            t_path = logs_dir / "transcript.jsonl"
            full_path = logs_dir / "transcript_full.jsonl"

            t_path.write_text(
                "\n".join([
                    json.dumps({"step_index": 0, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", "status": "DONE", "content": "hi"}),
                    json.dumps({
                        "step_index": 1,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "content": "Running check",
                        "tool_calls": [{"name": "run_command"}],
                    }),
                    json.dumps({
                        "step_index": 2,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "created_at": "2026-11-12T08:00:00Z",
                        "content": "Truncated...",
                        "truncated_fields": ["content"],
                    }),
                ]) + "\n",
                encoding="utf-8",
            )
            full_path.write_text(
                json.dumps({
                    "step_index": 2,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-11-12T08:00:00Z",
                    "content": "Full detailed review.\nVERDICT: SIGN_OFF",
                }) + "\n",
                encoding="utf-8",
            )

            turns = read_transcript_messages(str(t_path), last_n=5, only_final=True)
            self.assertEqual(len(turns), 1)
            self.assertEqual(turns[0]["text"], "Full detailed review.\nVERDICT: SIGN_OFF")

            all_turns = read_transcript_messages(str(t_path), last_n=5, only_final=False)
            self.assertEqual(len(all_turns), 2)

    def test_send_message_to_antigravity_invokes_agentapi(self):
        session = {
            "id": "53eed744-a64b-4f7c-8009-3720c6c1e0a8",
            "name": "Test Conv",
            "cwd": os.getcwd(),
            "cwds": [os.getcwd()],
            "status": "idle",
            "mtime": time.time(),
        }
        proc_ok = subprocess.CompletedProcess(
            args=["agentapi"],
            returncode=0,
            stdout=json.dumps({"response": {"sendMessage": {"recipientId": session["id"]}}}),
            stderr="",
        )
        with patch("agent_pair_ipc.antigravity.match_session", return_value=(session, "matched")), \
             patch("agent_pair_ipc.antigravity.find_agentapi_command", return_value=["/usr/local/bin/agentapi"]), \
             patch("agent_pair_ipc.antigravity.route_session_daemons", return_value=[{"pid": os.getpid(), "httpPort": 50325, "csrfToken": "tok"}]), \
             patch("agent_pair_ipc.antigravity.subprocess.run", return_value=proc_ok) as mock_run:
            ok = send_message_to_antigravity("Review this change", sender="Codex")

        self.assertTrue(ok)
        mock_run.assert_called_once()
        cmd = mock_run.call_args.args[0]
        env = mock_run.call_args.kwargs["env"]
        self.assertEqual(cmd[0], "/usr/local/bin/agentapi")
        self.assertEqual(cmd[1], "send-message")
        self.assertEqual(cmd[3], session["id"])
        self.assertIn("From: Codex", cmd[4])
        self.assertEqual(env["ANTIGRAVITY_LS_ADDRESS"], "localhost:50325")
        self.assertEqual(env["ANTIGRAVITY_CSRF_TOKEN"], "tok")

    def test_send_retries_only_missing_trajectory(self):
        session = {"id": "desktop-id", "cwd": os.getcwd()}
        routes = [{"httpPort": 1, "csrfToken": "one"}, {"httpPort": 2, "csrfToken": "two"}]
        for error, expected in [("trajectory not found", 2), ("connection timeout", 1)]:
            with self.subTest(error=error), \
                 patch("agent_pair_ipc.antigravity.match_session", return_value=(session, "matched")), \
                 patch("agent_pair_ipc.antigravity.find_agentapi_command", return_value=["agentapi"]), \
                 patch("agent_pair_ipc.antigravity.route_session_daemons", return_value=routes), \
                 patch("agent_pair_ipc.antigravity.subprocess.run", side_effect=[
                     subprocess.CompletedProcess([], 1, json.dumps({"error": error}), ""),
                     subprocess.CompletedProcess([], 0, '{"response": {}}', ""),
                 ]) as run:
                self.assertEqual(send_message_to_antigravity("ping"), expected == 2)
                self.assertEqual(run.call_count, expected)
                if expected == 2:
                    self.assertEqual(run.call_args.kwargs["env"]["ANTIGRAVITY_LS_ADDRESS"], "localhost:2")

    def test_explicit_endpoint_is_not_overridden(self):
        from agent_pair_ipc.antigravity import route_session_daemons
        with patch.dict(os.environ, {"ANTIGRAVITY_LS_ADDRESS": "localhost:123"}), \
             patch("agent_pair_ipc.antigravity.discover_active_daemons") as discover:
            self.assertEqual(route_session_daemons(["agentapi"], "sid"), [None])
            discover.assert_not_called()

    def test_metadata_probe_prioritizes_visible_daemon(self):
        from agent_pair_ipc.antigravity import route_session_daemons
        routes = [{"httpPort": 1}, {"httpPort": 2}]
        with patch.dict(os.environ, {}, clear=True), \
             patch("agent_pair_ipc.antigravity.discover_active_daemons", return_value=routes), \
             patch("agent_pair_ipc.antigravity.subprocess.run", side_effect=[
                 subprocess.CompletedProcess([], 1, '{"error": "trajectory not found"}', ""),
                 subprocess.CompletedProcess([], 0, '{"response": {"title": "Desktop"}}', ""),
             ]):
            self.assertEqual(route_session_daemons(["agentapi"], "sid"), list(reversed(routes)))

    def test_database_snapshot_preserves_uncheckpointed_wal(self):
        from agent_pair_ipc.antigravity import conversation_database
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "live.db"
            writer = sqlite3.connect(str(db))
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("CREATE TABLE probe (value TEXT)")
            writer.execute("INSERT INTO probe VALUES ('desktop-session')")
            writer.commit()
            real_connect = sqlite3.connect
            def connect(path, *args, **kwargs):
                if kwargs.get("uri"):
                    raise sqlite3.OperationalError("unable to open database file")
                return real_connect(path, *args, **kwargs)
            try:
                with patch("agent_pair_ipc.antigravity.sqlite3.connect", side_effect=connect):
                    with conversation_database(db) as reader:
                        self.assertEqual(reader.execute("SELECT value FROM probe").fetchone()[0], "desktop-session")
            finally:
                writer.close()

    def test_send_and_wait_transcript_completion(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            t_path = Path(tmpdir) / "transcript.jsonl"
            t_path.write_text("", encoding="utf-8")

            session = {
                "id": "conv-wait-001",
                "name": "Wait Session",
                "cwd": os.getcwd(),
                "cwds": [os.getcwd()],
                "status": "idle",
                "jsonl_path": str(t_path),
                "mtime": time.time(),
            }

            captured_payload = {}

            def fake_send(full_msg, **kwargs):
                captured_payload["msg"] = full_msg
                return True

            def writer():
                time.sleep(0.15)
                # Extract nonce from the enveloped payload
                nonce = captured_payload["msg"].split("[AgentPairIPC-Req-ID: ")[1].split(" ")[0]
                with open(t_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "step_index": 0,
                        "source": "SYSTEM",
                        "type": "SYSTEM_MESSAGE",
                        "status": "DONE",
                        "content": f"[AgentPairIPC-Req-ID: {nonce} | From: Claude Code]",
                    }) + "\n")
                    f.flush()
                time.sleep(0.15)
                with open(t_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "step_index": 1,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "tool_calls": [{"name": "run_command"}],
                    }) + "\n")
                    f.flush()
                time.sleep(0.15)
                with open(t_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({
                        "step_index": 2,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "content": "Looks solid.\nVERDICT: SIGN_OFF",
                    }) + "\n")
                    f.flush()

            t = threading.Thread(target=writer, daemon=True)
            with patch("agent_pair_ipc.antigravity.match_session", return_value=(session, "matched")), \
                 patch("agent_pair_ipc.antigravity.send_message_to_antigravity", side_effect=fake_send):
                t.start()
                resp, verdict = send_and_wait("Check PR", timeout=5.0, fast_fail_timeout=3.0, sender="Claude Code")
                t.join(timeout=2.0)

            self.assertEqual(verdict, "SIGN_OFF")
            self.assertEqual(resp, "Looks solid.\nVERDICT: SIGN_OFF")

    def test_send_and_wait_fast_fail_queued_unconsumed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            t_path = Path(tmpdir) / "transcript.jsonl"
            t_path.write_text("", encoding="utf-8")
            session = {
                "id": "conv-stuck-001",
                "name": "Busy Conv",
                "cwd": os.getcwd(),
                "cwds": [os.getcwd()],
                "status": "busy",
                "jsonl_path": str(t_path),
                "mtime": time.time(),
            }
            with patch("agent_pair_ipc.antigravity.match_session", return_value=(session, "matched")), \
                 patch("agent_pair_ipc.antigravity.send_message_to_antigravity", return_value=True):
                resp, verdict = send_and_wait("Ping", timeout=5.0, fast_fail_timeout=0.3, sender="Codex")

            self.assertIsNone(resp)
            self.assertEqual(verdict, "QUEUED_UNCONSUMED")

    def test_send_and_wait_duplex_reply_wins(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            t_path = Path(tmpdir) / "transcript.jsonl"
            t_path.write_text("", encoding="utf-8")
            session = {
                "id": "conv-duplex-001",
                "name": "Duplex Conv",
                "cwd": os.getcwd(),
                "cwds": [os.getcwd()],
                "status": "idle",
                "jsonl_path": str(t_path),
                "mtime": time.time(),
            }

            def fake_send(full_msg, **kwargs):
                nonce = full_msg.split("[AgentPairIPC-Req-ID: ")[1].split(" ")[0]
                def reply_later():
                    time.sleep(0.15)
                    send_reply(nonce, "Direct duplex verdict.\nVERDICT: CHANGES_REQUESTED", sender="Antigravity")
                threading.Thread(target=reply_later, daemon=True).start()
                return True

            with patch("agent_pair_ipc.antigravity.match_session", return_value=(session, "matched")), \
                 patch("agent_pair_ipc.antigravity.send_message_to_antigravity", side_effect=fake_send):
                resp, verdict = send_and_wait("Audit code", timeout=5.0, fast_fail_timeout=3.0, sender="Codex")

            self.assertEqual(verdict, "CHANGES_REQUESTED")
            self.assertIn("Direct duplex verdict.", resp)

    def test_non_tty_auto_await_exits_needs_new_thread(self):
        with patch("agent_pair_ipc.antigravity.get_all_sessions", return_value=[]), \
             patch("agent_pair_ipc.antigravity.sys.stderr.isatty", return_value=False):
            rc = antigravity_main(["-m", "hello"])
        self.assertEqual(rc, EXIT_NEEDS_NEW_THREAD)

    def test_cli_list_and_subcommand_dispatch(self):
        from agent_pair_ipc.cli import main as cli_main

        with patch("agent_pair_ipc.claude.list_sessions") as mock_claude, \
             patch("agent_pair_ipc.codex.list_sessions") as mock_codex, \
             patch("agent_pair_ipc.antigravity.list_sessions") as mock_ag:
            rc = cli_main(["list"])
        self.assertEqual(rc, EXIT_SIGN_OFF)
        mock_claude.assert_called_once()
        mock_codex.assert_called_once()
        mock_ag.assert_called_once()

        with patch("agent_pair_ipc.antigravity.main", return_value=EXIT_SIGN_OFF) as mock_ag_main:
            rc = cli_main(["antigravity", "-l"])
        self.assertEqual(rc, EXIT_SIGN_OFF)
        mock_ag_main.assert_called_once_with(["-l"])


if __name__ == "__main__":
    unittest.main()
