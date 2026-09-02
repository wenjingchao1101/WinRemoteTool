import contextlib
import io
import socket
import threading
import tempfile
import unittest
from pathlib import Path

import winauto
from winauto_modules.file_transfer import iter_files, push_root, safe_destination


class ProtocolTests(unittest.TestCase):
    def test_frame_round_trip(self):
        left, right = socket.socketpair()
        try:
            message = {"type": "hello", "value": "中文", "items": [1, 2]}
            winauto.send_frame(left, message)
            self.assertEqual(winauto.recv_frame(right), message)
        finally:
            left.close()
            right.close()

    def test_frame_rejects_oversized_payload(self):
        left, right = socket.socketpair()
        try:
            left.sendall((winauto.MAX_FRAME_SIZE + 1).to_bytes(4, "big"))
            with self.assertRaises(winauto.ProtocolError):
                winauto.recv_frame(right)
        finally:
            left.close()
            right.close()

    def test_agent_handshake_does_not_require_a_token(self):
        server = winauto.WinautoServer(("127.0.0.1", 0))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with socket.create_connection(server.server_address, timeout=2) as client:
                winauto.send_frame(client, {"type": "hello", "client_version": winauto.VERSION})
                response = winauto.recv_frame(client)
                self.assertEqual(response["type"], "hello_ok")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_agent_does_not_enable_port_reuse(self):
        self.assertFalse(winauto.WinautoServer.allow_reuse_address)


class CommandTests(unittest.TestCase):
    def test_cmd_wraps_command_like_windows_cmd(self):
        command = winauto.build_command("cmd", ["dir", "C:\\"])
        if winauto.os.name == "nt":
            self.assertEqual(command, ["cmd.exe", "/d", "/s", "/c", "dir C:\\"])
        else:
            self.assertEqual(command, ["sh", "-c", "dir C:\\"])

    def test_cmd_subcommand_defaults_to_cmd_shell(self):
        args = winauto._build_parser().parse_args(["cmd", "dir"])
        self.assertEqual(args.shell, "cmd")
        self.assertEqual(args.command, ["dir"])

    def test_global_s_selects_a_target_before_exec(self):
        args = winauto._build_parser().parse_args(["-s", "127.0.0.1:27889", "exec", "ipconfig /all"])
        self.assertEqual(args.global_target, "127.0.0.1:27889")
        self.assertEqual(args.command, ["ipconfig /all"])

    def test_global_short_target_option_is_registered(self):
        help_text = winauto._build_parser().format_help()
        self.assertIn("-s GLOBAL_TARGET", help_text)

    def test_version_options_print_the_tool_version(self):
        parser = winauto._build_parser()
        for option in ("--version", "-V"):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                with self.assertRaises(SystemExit) as raised:
                    parser.parse_args([option])
            self.assertEqual(raised.exception.code, 0)
            self.assertIn(winauto.VERSION, output.getvalue())

    def test_raw_command_preserves_argument_boundaries(self):
        self.assertEqual(winauto.build_command("raw", ["python", "-c", "print(1)"]), ["python", "-c", "print(1)"])

    def test_separator_is_removed(self):
        self.assertEqual(winauto.build_command("raw", ["--", "python", "-V"]), ["python", "-V"])

    def test_file_transfer_enumerates_files_and_blocks_traversal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "nested").mkdir()
            (root / "one.txt").write_text("one", encoding="utf-8")
            (root / "nested" / "two.txt").write_text("two", encoding="utf-8")
            names = [relative for _, relative in iter_files(root)]
            self.assertEqual(names, ["nested/two.txt", "one.txt"])
            destination = root / "downloads"
            self.assertEqual(safe_destination(destination, "nested/two.txt"), destination / "nested" / "two.txt")
            with self.assertRaises(ValueError):
                safe_destination(destination, "../outside.txt")

    def test_push_root_uses_adb_like_file_and_directory_semantics(self):
        self.assertEqual(push_root("", "file", "app.log"), Path("app.log"))
        self.assertEqual(push_root("C:/backup", "file", "app.log"), Path("C:/backup"))
        self.assertEqual(push_root("C:/backup/", "file", "app.log"), Path("C:/backup/app.log"))
        self.assertEqual(push_root("C:/backup", "directory", "logs"), Path("C:/backup"))
        self.assertEqual(push_root("", "directory", "logs"), Path("logs"))

    def test_interactive_shell_has_a_default_command(self):
        executable = winauto.build_command("cmd", [], interactive=True)
        self.assertEqual(executable, ["cmd.exe"] if winauto.os.name == "nt" else ["sh"])


if __name__ == "__main__":
    unittest.main()
