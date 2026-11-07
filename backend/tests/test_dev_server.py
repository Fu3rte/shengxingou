import socket
import subprocess
import sys
import unittest
from pathlib import Path


class DevServerTests(unittest.TestCase):
    def test_occupied_port_fails_before_app_startup(self):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", 8765)) == 0:
                self.skipTest("端口 8765 正在使用")
        with socket.create_server(("127.0.0.1", 8765)):
            result = subprocess.run(
                [sys.executable, "-m", "uvicorn", "src.api:app", "--host", "127.0.0.1", "--port", "8765", "--no-access-log"],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                timeout=15,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stderr)
        self.assertNotIn(b"Application startup complete", result.stderr)


if __name__ == "__main__":
    unittest.main()
