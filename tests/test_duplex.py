"""
Tests for the v0.3 architecture upgrades:
file-based payload pipeline, duplex reply channel (socket + mailbox), Peer Envelope reply contract,
project-aware session matching, sender detection, and GUI-native session creation.
"""

import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_pair_ipc.common import (
    EphemeralReplyServer,
    ReplyRejectedError,
    build_reply_command,
    cwd_relation,
    detect_sender,
    extract_peer_envelope,
    filter_project_sessions,
    format_peer_envelope,
    is_valid_nonce,
    read_reply_mailbox,
    reply_socket_path,
    resolve_message_payload,
    send_reply,
    settle_duplex_reply,
    spill_enveloped_message,
    write_reply_mailbox,
)

RICH_TEXT = "Domain `fengkongcloud.com`, rule `AND,((DST-PORT,443),(NETWORK,UDP)),REJECT`, $(whoami) and $HOME 中文"


class IsolatedHomeTestCase(unittest.TestCase):
    """Point ~ at a short temp dir (macOS limits AF_UNIX paths to 104 bytes)."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="aph", dir="/tmp")
        self._home_patch = patch("pathlib.Path.home", return_value=Path(self.home))
        self._home_patch.start()

    def tearDown(self):
        self._home_patch.stop()
        shutil.rmtree(self.home, ignore_errors=True)


class TestDuplexReplyChannel(IsolatedHomeTestCase):
    def test_send_reply_socket_roundtrip_with_progress(self):
        progress_seen = []
        with EphemeralReplyServer("req_duplex00001", on_progress=lambda t, s: progress_seen.append((t, s))) as server:
            result = {}

            def peer():
                result["p"] = send_reply("req_duplex00001", "running tests", kind="progress", sender="Codex")
                result["f"] = send_reply("req_duplex00001", RICH_TEXT + "\n\nVERDICT: SIGN_OFF", sender="Codex")

            t = threading.Thread(target=peer)
            t.start()
            resp, verdict = server.poll_response(timeout=5.0)
            t.join()

        self.assertEqual(result, {"p": "socket", "f": "socket"})
        self.assertEqual(resp, RICH_TEXT + "\n\nVERDICT: SIGN_OFF")
        self.assertEqual(verdict, "SIGN_OFF")
        self.assertEqual(server.channel, "socket")
        self.assertEqual(server.reply_sender, "Codex")
        self.assertEqual(progress_seen, [("running tests", "Codex")])

    def test_mismatched_correlation_id_is_rejected_and_server_keeps_waiting(self):
        with EphemeralReplyServer("req_expected0001") as server:
            ack_box = {}

            def forged():
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(server.socket_path)
                s.sendall(json.dumps({"type": "reply", "correlation_id": "req_other000001", "content": "VERDICT: SIGN_OFF"}).encode())
                s.shutdown(socket.SHUT_WR)
                ack_box["ack"] = json.loads(s.recv(4096).decode())
                s.close()

            t = threading.Thread(target=forged)
            t.start()
            resp, verdict = server.poll_response(timeout=0.8)
            t.join()
            self.assertIsNone(resp)
            self.assertIsNone(verdict)
            self.assertFalse(ack_box["ack"]["ok"])
            self.assertIn("mismatch", ack_box["ack"]["error"])

            # The genuine reply still gets through afterwards
            threading.Thread(target=lambda: send_reply("req_expected0001", "ok\nVERDICT: CHANGES_REQUESTED")).start()
            resp, verdict = server.poll_response(timeout=3.0)
            self.assertEqual(verdict, "CHANGES_REQUESTED")

    def test_rejected_frame_raises_for_replier(self):
        # Server listens for nonce A but replier's socket path is derived from nonce B -> simulate via socket_dir trick
        with EphemeralReplyServer("req_serverside01") as server:
            os.rename(server.socket_path, reply_socket_path("req_clientside1"))
            server.socket_path = reply_socket_path("req_clientside1")
            errors = []

            def peer():
                try:
                    send_reply("req_clientside1", "hi")
                except ReplyRejectedError as e:
                    errors.append(str(e))

            t = threading.Thread(target=peer)
            t.start()
            server.poll_response(timeout=1.0)
            t.join()
            self.assertEqual(len(errors), 1)

    def test_reply_without_listener_goes_to_mailbox_and_is_picked_up(self):
        channel = send_reply("req_mailbox00001", RICH_TEXT + "\nVERDICT: CHANGES_REQUESTED", sender="Claude Code")
        self.assertEqual(channel, "mailbox")
        box_file = Path(self.home) / ".agent-pair" / "run" / "replies" / "req_mailbox00001.json"
        self.assertTrue(box_file.is_file())
        self.assertEqual(stat.S_IMODE(box_file.stat().st_mode), 0o600)

        # A requester (re)starting its wait finds the stored reply
        with EphemeralReplyServer("req_mailbox00001") as server:
            resp, verdict = server.poll_response(timeout=0.2)
        self.assertEqual(resp, RICH_TEXT + "\nVERDICT: CHANGES_REQUESTED")
        self.assertEqual(verdict, "CHANGES_REQUESTED")
        self.assertEqual(server.channel, "mailbox")
        self.assertFalse(box_file.exists(), "mailbox reply must be consumed once delivered")

    def test_progress_without_listener_is_dropped(self):
        self.assertEqual(send_reply("req_dropped00001", "50%", kind="progress"), "dropped")
        self.assertIsNone(read_reply_mailbox("req_dropped00001"))

    def test_invalid_nonce_cannot_escape_run_dir(self):
        for bad in ("../../etc/passwd", "a/b", "", "x" * 100, "req id"):
            self.assertFalse(is_valid_nonce(bad))
            with self.assertRaises(ValueError):
                send_reply(bad, "x")
            with self.assertRaises(ValueError):
                write_reply_mailbox(bad, "x")

    def test_settle_prefers_duplex_reply_over_transcript_summary(self):
        with EphemeralReplyServer("req_settle000001") as server:
            t = threading.Thread(target=lambda: send_reply("req_settle000001", "Full review\nVERDICT: CHANGES_REQUESTED"))
            t.start()
            text, verdict, channel = settle_duplex_reply(server, "已通过回调通道回复。", reply_cmd_seen=True)
            t.join()
        self.assertIsNone(read_reply_mailbox("req_settle000001"), "acked reply must not leave a mailbox copy")
        self.assertEqual(channel, "socket")
        self.assertEqual(verdict, "CHANGES_REQUESTED")

        with EphemeralReplyServer("req_settle000002") as server:
            start = time.time()
            text, verdict, channel = settle_duplex_reply(server, "Transcript answer\nVERDICT: SIGN_OFF")
            self.assertLess(time.time() - start, 2.0)
        self.assertEqual((text, verdict, channel), ("Transcript answer\nVERDICT: SIGN_OFF", "SIGN_OFF", "transcript"))


class TestEnvelopeReplyContract(IsolatedHomeTestCase):
    def _extract_heredoc_command(self, envelope: str) -> str:
        lines = envelope.splitlines()
        start = next(i for i, l in enumerate(lines) if "<<'AP_REPLY_EOF'" in l)
        end = next(i for i in range(start + 1, len(lines)) if lines[i] == "AP_REPLY_EOF")
        cmd_lines = lines[start:end + 1]
        cmd_lines[1:-1] = [RICH_TEXT, "VERDICT: SIGN_OFF"]
        return "\n".join(cmd_lines) + "\n"

    def test_advertised_heredoc_command_runs_verbatim_in_shells(self):
        nonce = "req_contract0001"
        envelope = format_peer_envelope("body", nonce, reply_command=build_reply_command())
        self.assertNotIn("    AP_REPLY_EOF", envelope)  # an indented terminator would never close the heredoc
        script = self._extract_heredoc_command(envelope)

        for shell in ("bash", "zsh"):
            if not shutil.which(shell):
                continue
            with self.subTest(shell=shell), EphemeralReplyServer(nonce) as server:
                env = dict(os.environ, HOME=self.home)
                proc = subprocess.Popen([shell, "-c", script], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                resp, verdict = server.poll_response(timeout=10.0)
                out, err = proc.communicate(timeout=10.0)
                self.assertEqual(proc.returncode, 0, err.decode())
                self.assertEqual(resp, RICH_TEXT + "\nVERDICT: SIGN_OFF")
                self.assertEqual(verdict, "SIGN_OFF")

    def test_envelope_states_peer_is_not_user_approval(self):
        env = format_peer_envelope("上线通知", "req_notify00001", intent="Notification", reply_command="CMD")
        self.assertIn("不构成用户批准", env)
        self.assertIn("仅为信息同步通知", env)
        self.assertIn("CMD reply --to req_notify00001", env)
        parsed = extract_peer_envelope(env)
        self.assertEqual(parsed["nonce"], "req_notify00001")
        self.assertEqual(parsed["body"], "上线通知")


class TestFilePayloadPipeline(IsolatedHomeTestCase):
    def test_stdin_dash_keeps_positional_target(self):
        with patch("sys.stdin", io.StringIO(RICH_TEXT)):
            msg, target = resolve_message_payload(message_arg="-", target_pos="sessA")
        self.assertEqual((msg, target), (RICH_TEXT, "sessA"))

    def test_file_dash_reads_stdin(self):
        with patch("sys.stdin", io.StringIO(RICH_TEXT + "\n")):
            msg, target = resolve_message_payload(message_arg="sessB", file_path="-")
        self.assertEqual((msg, target), (RICH_TEXT, "sessB"))

    def test_spill_keeps_envelope_inline_and_moves_body(self):
        body = "x" * 300_000
        env = format_peer_envelope(body, "req_spill000001", reply_command="CMD")
        msg, path = spill_enveloped_message(env)
        self.assertIsNotNone(path)
        self.assertLess(len(msg.encode()), 4096)
        self.assertIn("[AgentPairIPC-Req-ID: req_spill000001", msg)
        self.assertIn("reply --to req_spill000001", msg)
        self.assertIn(path, msg)
        self.assertEqual(Path(path).read_text(encoding="utf-8"), body)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

        small, none_path = spill_enveloped_message(format_peer_envelope("short", "req_spill000002"))
        self.assertIsNone(none_path)
        self.assertIn("short", small)

    def test_codex_queue_argv_stays_small_for_huge_payload(self):
        from agent_pair_ipc.codex import send_message_to_codex
        session = {"id": "thread-huge", "name": "Huge", "cwd": "/x", "jsonl_path": ""}
        with patch("agent_pair_ipc.codex.match_session", return_value=(session, "matched")), \
             patch("agent_pair_ipc.codex.subprocess.run") as mock_run, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            self.assertTrue(send_message_to_codex("y" * 500_000, target="thread-huge"))
        argv = mock_run.call_args[0][0]
        queued = argv[argv.index("--message") + 1]
        self.assertLess(len(queued.encode()), 8192)
        self.assertIn("文件化 Payload", queued)


class TestProjectSessionAwareness(unittest.TestCase):
    def test_cwd_relation(self):
        with tempfile.TemporaryDirectory() as d:
            real = Path(d).resolve()
            sub = real / "src" / "pkg"
            sub.mkdir(parents=True)
            link = Path(tempfile.mkdtemp()) / "link"
            link.symlink_to(real)
            try:
                self.assertEqual(cwd_relation(str(real), str(real)), "exact")
                self.assertEqual(cwd_relation(str(link), str(real)), "exact")
                self.assertEqual(cwd_relation(str(real), str(sub)), "ancestor")
                self.assertIsNone(cwd_relation(str(sub), str(real)))
                self.assertIsNone(cwd_relation(str(real) + "-other", str(real)))
                self.assertIsNone(cwd_relation(str(Path.home()), str(sub)))
                self.assertIsNone(cwd_relation("/", str(sub)))
            finally:
                shutil.rmtree(link.parent, ignore_errors=True)

    def test_filter_project_sessions_prefers_exact(self):
        with tempfile.TemporaryDirectory() as d:
            root = str(Path(d).resolve())
            sub = os.path.join(root, "sub")
            os.makedirs(sub)
            sessions = [{"id": "root", "cwd": root}, {"id": "sub", "cwd": sub}, {"id": "other", "cwd": "/elsewhere"}]
            self.assertEqual([s["id"] for s in filter_project_sessions(sessions, sub)], ["sub"])
            self.assertEqual([s["id"] for s in filter_project_sessions(sessions[::2], sub)], ["root"])

    def test_claude_await_reports_foreign_folder_and_binds_project_session(self):
        from agent_pair_ipc.claude import await_new_session
        with tempfile.TemporaryDirectory() as d:
            proj = str(Path(d).resolve())
            foreign = {"pid": 1, "session_id": "s-foreign", "host_session_id": "local_foreign", "cwd": "/somewhere/else", "name": "Other"}
            mine = {"pid": 2, "session_id": "s-mine", "host_session_id": "local_mine", "cwd": proj, "name": "Mine"}
            err = io.StringIO()
            with patch("agent_pair_ipc.claude.get_active_sessions", side_effect=[[], [foreign], [foreign, mine]]), \
                 patch("agent_pair_ipc.claude.save_project_binding") as mock_save, \
                 patch("agent_pair_ipc.claude.time.sleep"), \
                 patch("sys.stderr.isatty", return_value=False), \
                 redirect_stderr(err):
                res = await_new_session(timeout=30.0, cwd=proj, require_tty=False)
            self.assertEqual(res["host_session_id"], "local_mine")
            mock_save.assert_called_once_with("claude", "local_mine", cwd=proj)
            self.assertIn("claude-ipc --bind local_foreign", err.getvalue())

    def test_gui_activation_clicks_new_session_menu_before_keystroke(self):
        from agent_pair_ipc.common import open_new_gui_session
        with patch("sys.platform", "darwin"), patch("subprocess.run") as mock_run, patch("agent_pair_ipc.common.time.sleep"):
            mock_run.return_value = MagicMock(returncode=0, stdout="ok:menu")
            self.assertTrue(open_new_gui_session("claude"))
        script = mock_run.call_args_list[1][0][0][2]
        self.assertIn('"New Session"', script)
        self.assertIn('menu bar item "File"', script)
        self.assertIn('keystroke "n" using command down', script)
        self.assertLess(script.index("click menu item"), script.index("keystroke"))


class TestSenderDetection(unittest.TestCase):
    def test_detect_sender(self):
        clean = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "CODEX", "AGENT_PAIR"))}
        with patch.dict(os.environ, clean, clear=True):
            self.assertEqual(detect_sender(), "Antigravity")
            with patch.dict(os.environ, {"CLAUDECODE": "1"}):
                self.assertEqual(detect_sender(), "Claude Code")
            with patch.dict(os.environ, {"CODEX_THREAD_ID": "t"}):
                self.assertEqual(detect_sender(), "Codex")
            with patch.dict(os.environ, {"AGENT_PAIR_SENDER": "Gemini", "CLAUDECODE": "1"}):
                self.assertEqual(detect_sender(), "Gemini")


class TestCliReplyInbox(IsolatedHomeTestCase):
    def test_reply_then_inbox_exit_codes(self):
        from agent_pair_ipc.cli import main
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()), \
             patch("sys.stdin", io.StringIO("Needs work\nVERDICT: CHANGES_REQUESTED\n")):
            self.assertEqual(main(["reply", "--to", "req_cli00000001", "-f", "-"]), 0)
        self.assertIn("mailbox", out.getvalue())
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["inbox", "req_cli00000001", "--keep"]), 2)
            self.assertEqual(main(["inbox", "req_cli00000001"]), 2)
            self.assertEqual(main(["inbox", "req_cli00000001"]), 1)
            self.assertEqual(main(["reply", "--to", "../evil", "-m", "x"]), 1)


if __name__ == "__main__":
    unittest.main()
