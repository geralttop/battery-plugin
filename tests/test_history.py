import array
import datetime as dt
import fcntl
import json
import mmap
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import battery_history as history


class HistoryTests(unittest.TestCase):
    def test_rate_uses_fifteen_minutes(self):
        points = [(1000 + i * 60, 90 - i / 5, 2, "live") for i in range(20)]
        self.assertAlmostEqual(history.discharge_rate(points, 19), -12)
        self.assertIsNone(history.discharge_rate(points, 5))

    def test_rate_interpolates_window_boundary(self):
        points = [(1000 + i * 70, 90 - i * .1, 2, "live") for i in range(20)]
        self.assertAlmostEqual(history.discharge_rate(points, 19), -.1 * 3600 / 70)

    def test_rate_excludes_sleep_and_charging(self):
        points = [(1000 + i * 60, 90 - i / 5, 2, "live") for i in range(20)]
        points[10] = (*points[10][:2], 1, "live")
        self.assertIsNone(history.discharge_rate(points, 19))
        points = [(1000 + i * 300, 90 - i, 2, "upower") for i in range(20)]
        self.assertIsNone(history.discharge_rate(points, 19))

    def test_zero_rate_and_zero_charge_are_valid(self):
        points = [(1000 + i * 60, 0, 2, "live") for i in range(20)]
        self.assertEqual(history.discharge_rate(points, 19), 0)

    def test_merge_retention_validation_and_device_separation(self):
        with tempfile.TemporaryDirectory() as root:
            now = 5000000
            with history.connect(root) as con:
                history.merge_points(con, "a", [(now, 60, 2, "upower"), (now, 61, 2, "live"),
                    (now - history.RETENTION - 1, 50, 2, "live"), (now + 1, 5, 2, "live"),
                    (now - 2, float("nan"), 2, "live"), (now - 1, 0, 0, "upower")], now)
                history.merge_points(con, "a", [(now, 10, 2, "upower")], now)
                history.merge_points(con, "b", [(now, 0, 2, "live")], now)
                self.assertEqual(con.execute("SELECT device,percent FROM points ORDER BY device").fetchall(), [("a", 61), ("b", 0)])
            self.assertEqual(history.read_points(root, "a", now - 60, now)[0][1], 61)

    def test_days_are_local_midnight_and_span_thirty_dates(self):
        now = dt.datetime(2026, 11, 8, 1, 0).timestamp()
        days = history.day_choices(now)
        self.assertEqual(len(days), 30)
        self.assertEqual(days[0]["label"], "08.11.2026")
        self.assertEqual(dt.datetime.fromtimestamp(days[0]["finish"]), dt.datetime(2026, 11, 9))

    def test_unavailable_upower_keeps_stored_history(self):
        with tempfile.TemporaryDirectory() as root:
            now = int(time.time())
            with history.connect(root) as con:
                history.merge_points(con, "a", [(now, 42, 2, "live")], now)
                con.execute("INSERT INTO metadata VALUES ('device', 'a')")
            with patch.object(history, "dbus", side_effect=OSError("unavailable")):
                status = history.collect(root)
            self.assertEqual(status["device"], "a")
            self.assertEqual(status["error"], "UPower недоступен")
            self.assertNotIn("percent", status)
            self.assertEqual(history.read_points(root, "a", now - 60, now)[0][1], 42)

    def test_no_battery(self):
        with tempfile.TemporaryDirectory() as root, patch.object(history, "dbus", return_value=[]):
            self.assertEqual(history.collect(root)["error"], "Батарея не найдена")

    def test_import_and_live_sample(self):
        now = int(time.time())
        props = {"NativePath": "BAT0", "Type": 2, "PowerSupply": True, "IsPresent": True,
                 "Percentage": 0, "State": 2}
        def call(path, interface, method, *args):
            if method == "EnumerateDevices": return ["/battery"]
            if method == "GetHistory": return [(now - 60, 1, 2), (now - 120, 0, 0)]
            return {k: {"data": v} for k, v in props.items()}
        with tempfile.TemporaryDirectory() as root, patch.object(history, "dbus", side_effect=call):
            status = history.collect(root)
            self.assertEqual(status["percent"], 0)
            records = history.read_points(root, status["device"], now - 600, now)
            self.assertEqual(len(records), 2)
            self.assertEqual([r[3] for r in records], ["upower", "live"])

    def test_render_empty_single_and_hover_gap(self):
        req = {"hours": 1, "finish": 10000, "colors": {}}
        cases = [[], [(9000, 0, 2, "live")], [(7000, 100, 1, "upower"), (9500, 50, 2, "upower")]]
        for points in cases:
            for hover in (None, [.5, .5], [1, 1]):
                raw = history.draw_chart(points, dict(req, hover=hover))
                self.assertEqual(len(raw), 1040 * 570 * 4)
                self.assertEqual(set(raw[3::4]), {255})


class PixelProtocolTests(unittest.TestCase):
    def test_real_socket_fd_seal_frame_reconnect_and_shutdown(self):
        with tempfile.TemporaryDirectory() as root:
            request = Path(root) / "view.json"
            sock_path = Path(root) / "graph.sock"
            history.atomic_json(request, {"active": True, "hours": 24, "finish": int(time.time()), "colors": {}})
            process = subprocess.Popen([sys.executable, str(Path(history.__file__)), "render", root, str(request), str(sock_path)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not sock_path.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                for attempt in range(2):
                    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as peer:
                        peer.settimeout(4)
                        peer.connect(str(sock_path))
                        peer.send(struct.pack("<II", 1, 1)); peer.send(struct.pack("<II", 2, 0))
                        packet, ancillary, _, _ = peer.recvmsg(64, socket.CMSG_SPACE(4))
                        self.assertEqual(struct.unpack("<IIII", packet), (1, 0, 1040, 570))
                        fds = array.array("i"); fds.frombytes(ancillary[0][2][:4]); fd = fds[0]
                        try:
                            self.assertTrue(fcntl.fcntl(fd, fcntl.F_GET_SEALS) & fcntl.F_SEAL_SHRINK)
                            with mmap.mmap(fd, 1040 * 570 * 4, access=mmap.ACCESS_READ) as pixels:
                                self.assertEqual(pixels[3], 255)
                            self.assertEqual(struct.unpack("<II", peer.recv(64)), (2, 0))
                            peer.send(struct.pack("<II", 2, 0))
                            history.atomic_json(request, {"active": True, "hours": 6 + attempt, "finish": int(time.time()), "colors": {}})
                            self.assertEqual(struct.unpack("<II", peer.recv(64)), (2, 0))
                        finally:
                            os.close(fd)
                history.atomic_json(request, {"active": False})
                _, err = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, err.decode())
                self.assertFalse(sock_path.exists())
                self.assertFalse(request.exists())
            finally:
                if process.poll() is None:
                    process.terminate(); process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
