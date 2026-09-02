import contextlib
import io
import socket
import struct
import threading
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import winauto
from winauto_modules.file_transfer import iter_files, push_root, safe_destination
from winauto_modules.screenshot import Screenshot, encode_bgra_png


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


class ScreenshotTests(unittest.TestCase):
    def test_bgra_pixels_are_encoded_as_rgb_png(self):
        png = encode_bgra_png(
            2,
            1,
            bytes(
                [
                    3,
                    2,
                    1,
                    255,
                    30,
                    20,
                    10,
                    0,
                ]
            ),
        )
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        offset = 8
        idat = bytearray()
        width = height = 0
        while offset < len(png):
            length = struct.unpack(">I", png[offset : offset + 4])[0]
            chunk_type = png[offset + 4 : offset + 8]
            data = png[offset + 8 : offset + 8 + length]
            if chunk_type == b"IHDR":
                width, height = struct.unpack(">II", data[:8])
            elif chunk_type == b"IDAT":
                idat.extend(data)
            offset += 12 + length
        self.assertEqual((width, height), (2, 1))
        self.assertEqual(zlib.decompress(idat), b"\x00\x01\x02\x03\x0a\x14\x1e")

    def test_screenshot_parser_accepts_local_and_remote_output(self):
        local = winauto._build_parser().parse_args(["screenshot", "capture.png"])
        remote = winauto._build_parser().parse_args(
            ["-s", "127.0.0.1:27889", "screenshot", "capture.png"]
        )
        self.assertEqual(local.output, "capture.png")
        self.assertEqual(remote.global_target, "127.0.0.1:27889")

    def test_remote_screenshot_is_streamed_and_verified(self):
        payload = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 300
        screenshot = Screenshot(width=1920, height=1080, png=payload)
        server = winauto.WinautoServer(("127.0.0.1", 0))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                destination = Path(temp_dir) / "remote.png"
                args = SimpleNamespace(
                    target=f"{server.server_address[0]}:{server.server_address[1]}",
                    output=str(destination),
                )
                with mock.patch.object(winauto, "capture_screenshot", return_value=screenshot):
                    with contextlib.redirect_stdout(io.StringIO()):
                        result = winauto._screenshot_remote(args)
                self.assertEqual(result, 0)
                self.assertEqual(destination.read_bytes(), payload)
                self.assertFalse(Path(str(destination) + ".winauto.part").exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
