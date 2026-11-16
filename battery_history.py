#!/usr/bin/env python3
"""UPower collection, persistent history and Noctalia pixel-stream companion."""
from __future__ import annotations

import argparse
import array
import bisect
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import mmap
import os
from pathlib import Path
import selectors
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import time

RETENTION = 30 * 86400
MAX_GAP = 180
RATE_WINDOW = 900
UP = "org.freedesktop.UPower"
DEVICE = UP + ".Device"
STATES = {1: "Зарядка", 2: "Разряд", 3: "Разряжена", 4: "Заряжена", 5: "Ожидание зарядки", 6: "Ожидание разряда"}


def dbus(path, interface, method, *args):
    result = subprocess.run(["busctl", "--system", "--timeout=3", "--json=short", "call", UP,
                             path, interface, method, *map(str, args)],
                            capture_output=True, text=True, timeout=5, check=True)
    return json.loads(result.stdout)["data"][0]


def properties(path):
    return {k: v["data"] for k, v in dbus(path, "org.freedesktop.DBus.Properties", "GetAll", "s", DEVICE).items()}


@contextlib.contextmanager
def connect(data_dir):
    root = Path(data_dir)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    con = sqlite3.connect(root / "history.sqlite3", timeout=5)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS points (
            device TEXT NOT NULL, ts INTEGER NOT NULL, percent REAL NOT NULL,
            state INTEGER NOT NULL, source TEXT NOT NULL,
            PRIMARY KEY (device, ts));
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        PRAGMA user_version=1;
    """)
    try:
        with con:
            yield con
    finally:
        con.close()


def atomic_json(path, value):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def day_choices(now):
    today = dt.datetime.fromtimestamp(now).date()
    return [{"label": (today - dt.timedelta(days=i)).strftime("%d.%m.%Y"),
             "finish": int(dt.datetime.combine(today - dt.timedelta(days=i - 1), dt.time()).timestamp())}
            for i in range(30)]


def merge_points(con, device, records, now):
    valid = []
    for record in records:
        try:
            stamp, percent, state, source = record
            stamp, percent, state = int(stamp), float(percent), int(state)
            if now - RETENTION <= stamp <= now and math.isfinite(percent) and 0 <= percent <= 100 and state in STATES:
                valid.append((device, stamp, percent, state, source))
        except (ValueError, TypeError):
            continue
    con.executemany("""INSERT INTO points VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(device, ts) DO UPDATE SET percent=excluded.percent,
        state=excluded.state, source=excluded.source WHERE excluded.source='live'""", valid)
    con.execute("DELETE FROM points WHERE ts < ?", (now - RETENTION,))


def collect(data_dir):
    now = int(time.time())
    status = {"days": day_choices(now), "sampled_at": now}
    with connect(data_dir) as con:
        try:
            paths = dbus("/org/freedesktop/UPower", UP, "EnumerateDevices")
            battery = None
            for path in paths:
                props = properties(path)
                if props.get("Type") == 2 and props.get("PowerSupply") and props.get("IsPresent"):
                    battery = (path, props)
                    break
            if battery is None:
                status["error"] = "Батарея не найдена"
            else:
                path, props = battery
                identity = "|".join(str(props.get(k, "")) for k in ("NativePath", "Vendor", "Model", "Serial"))
                device = hashlib.sha256(identity.encode()).hexdigest()[:20]
                status.update(device=device, percent=props["Percentage"], state=props["State"])
                import_key = "import:" + device
                last = con.execute("SELECT value FROM metadata WHERE key=?", (import_key,)).fetchone()
                if not last or now - int(last[0]) >= 21600:
                    try:
                        records = dbus(path, DEVICE, "GetHistory", "suu", "charge", RETENTION, 100000)
                        merge_points(con, device, [(*r, "upower") for r in records], now)
                        con.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", (import_key, str(now)))
                    except (subprocess.SubprocessError, ValueError, KeyError) as exc:
                        status["warning"] = "Системная история недоступна; новые измерения сохраняются"
                        print("UPower history: " + str(exc), file=sys.stderr)
                merge_points(con, device, [(now, props["Percentage"], props["State"], "live")], now)
                con.execute("INSERT OR REPLACE INTO metadata VALUES ('device', ?)", (device,))
        except (subprocess.SubprocessError, OSError, KeyError, ValueError) as exc:
            status["error"] = "UPower недоступен"
            print("UPower: " + str(exc), file=sys.stderr)
        if "device" not in status:
            old = con.execute("SELECT value FROM metadata WHERE key='device'").fetchone()
            if old:
                status["device"] = old[0]
        con.execute("DELETE FROM points WHERE ts < ?", (now - RETENTION,))
    atomic_json(Path(data_dir) / "status.json", status)
    return status


def read_points(data_dir, device, start, finish):
    if not device or not (Path(data_dir) / "history.sqlite3").exists():
        return []
    # Read-only connection: the renderer never mutates collection data.
    con = sqlite3.connect((Path(data_dir) / "history.sqlite3").resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return con.execute("SELECT ts, percent, state, source FROM points WHERE device=? AND ts>=? AND ts<=? ORDER BY ts",
                           (device, start - RATE_WINDOW - MAX_GAP, finish + MAX_GAP)).fetchall()
    finally:
        con.close()


def discharge_rate(points, index):
    """Percent points/hour over exactly 15 continuous minutes; never across a gap."""
    target = points[index][0] - RATE_WINDOW
    first = index
    while first > 0 and points[first][0] > target:
        if points[first][2] != 2 or points[first - 1][2] != 2 or points[first][0] - points[first - 1][0] > MAX_GAP:
            return None
        first -= 1
    if points[first][0] > target or points[index][2] != 2:
        return None
    if points[first][0] == target:
        initial = points[first][1]
    else:
        a, b = points[first], points[first + 1]
        initial = a[1] + (b[1] - a[1]) * (target - a[0]) / (b[0] - a[0])
    delta = points[index][1] - initial
    return delta * 3600 / RATE_WINDOW if delta <= 0 else None


def color(ctx, hex_color, alpha=1):
    hex_color = hex_color.lstrip("#")
    ctx.set_source_rgba(*(int(hex_color[i:i + 2], 16) / 255 for i in (0, 2, 4)), alpha)


def draw_chart(points, request, width=1040, height=570):
    import cairo
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    ctx = cairo.Context(surface)
    ctx.scale(width / 520, height / 285)
    palette = request.get("colors", {})
    bg = palette.get("surface_container") or "#24232b"
    fg = palette.get("on_surface") or "#e8e0ed"
    muted = palette.get("on_surface_variant") or "#bdb7c5"
    primary = palette.get("primary") or "#c4a2ef"
    grid = palette.get("outline_variant") or "#49454f"
    charging = "#83c998"
    color(ctx, bg)
    ctx.paint()
    ctx.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)

    def text(x, y, label, size=11, ink=fg, align="left"):
        ctx.set_font_size(size)
        ext = ctx.text_extents(label)
        if align == "right": x -= ext.width
        if align == "center": x -= ext.width / 2
        color(ctx, ink)
        ctx.move_to(x, y)
        ctx.show_text(label)

    finish = int(request["finish"])
    start = finish - int(request["hours"]) * 3600
    left, top, right, bottom = 45, 22, 507, 218
    def xy(p):
        return left + (p[0] - start) / (finish - start) * (right - left), bottom - p[1] / 100 * (bottom - top)

    for value in (0, 25, 50, 75, 100):
        y = bottom - value / 100 * (bottom - top)
        color(ctx, grid, .5)
        ctx.set_line_width(.5)
        ctx.move_to(left, y); ctx.line_to(right, y); ctx.stroke()
        text(left - 8, y + 4, str(value) + "%", 10, muted, "right")
    for i in range(5):
        stamp = start + (finish - start) * i / 4
        x = left + (right - left) * i / 4
        color(ctx, grid, .3)
        ctx.move_to(x, top); ctx.line_to(x, bottom); ctx.stroke()
        align = "left" if i == 0 else "right" if i == 4 else "center"
        text(x, bottom + 19, dt.datetime.fromtimestamp(stamp).strftime("%H:%M"), 10, muted, align)
        if dt.datetime.fromtimestamp(start).date() != dt.datetime.fromtimestamp(finish).date():
            text(x, bottom + 34, dt.datetime.fromtimestamp(stamp).strftime("%d.%m"), 9, muted, align)

    ctx.save()
    ctx.rectangle(left, top, right - left, bottom - top); ctx.clip()
    for a, b in zip(points, points[1:]):
        if b[0] < start or a[0] > finish: continue
        ax, ay = xy(a); bx, by = xy(b)
        gap = b[0] - a[0] > MAX_GAP
        stroke = muted if gap else charging if a[2] == 1 else primary
        if not gap:
            color(ctx, stroke, .11)
            ctx.move_to(ax, bottom); ctx.line_to(ax, ay); ctx.line_to(bx, by); ctx.line_to(bx, bottom); ctx.close_path(); ctx.fill()
        color(ctx, stroke, .7 if gap else 1)
        ctx.set_line_width(1.5 if gap else 2)
        ctx.set_dash([3, 4] if gap else [])
        ctx.move_to(ax, ay); ctx.line_to(bx, by); ctx.stroke()
    ctx.set_dash([])
    visible = [p for p in points if start <= p[0] <= finish]
    if len(visible) == 1:
        x, y = xy(visible[0]); color(ctx, primary); ctx.arc(x, y, 3, 0, math.tau); ctx.fill()
    ctx.restore()

    if not visible:
        text((left + right) / 2, 115, request.get("error") or "За этот период пока нет измерений", 13, muted, "center")
    hover = request.get("hover")
    footer = "Наведи на график, чтобы увидеть измерение"
    if hover and points and left <= hover[0] * 520 <= right and top <= hover[1] * 285 <= bottom:
        stamp = start + (hover[0] * 520 - left) / (right - left) * (finish - start)
        times = [p[0] for p in points]
        index = bisect.bisect_left(times, stamp)
        gap = 0 < index < len(points) and points[index][0] - points[index - 1][0] > MAX_GAP
        outside = index == 0 and stamp < times[0] or index == len(points) and stamp > times[-1]
        if gap or outside:
            footer = dt.datetime.fromtimestamp(stamp).strftime("%d.%m %H:%M") + " · Нет измерений · нет оценки"
        else:
            index = min(index, len(points) - 1)
            if index > 0 and abs(times[index - 1] - stamp) < abs(times[index] - stamp): index -= 1
            point = points[index]
            x, y = xy(point)
            color(ctx, muted, .7); ctx.set_line_width(1)
            ctx.move_to(x, top); ctx.line_to(x, bottom); ctx.stroke()
            color(ctx, primary); ctx.arc(x, y, 3.5, 0, math.tau); ctx.fill()
            rate = discharge_rate(points, index)
            rate_text = (f"{rate:.1f}".replace(".", ",") + " п.п./ч") if rate is not None else "нет оценки"
            footer = dt.datetime.fromtimestamp(point[0]).strftime("%d.%m %H:%M") + f" · {point[1]:g}% · {STATES.get(point[2], 'Нет данных')} · {rate_text}"
    text(45, 278, footer, 10, muted)
    surface.flush()
    # Cairo ARGB32 is native-endian premultiplied BGRA on this platform. All pixels are opaque.
    raw = bytearray(surface.get_data())
    red = raw[2::4]
    raw[2::4] = raw[0::4]
    raw[0::4] = red
    return bytes(raw)


def stream(data_dir, request_path, socket_path):
    """One opaque sealed memfd, rewritten only after the host's next REQUEST."""
    running = True
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    request_path, socket_path = Path(request_path), Path(socket_path)
    sel = selectors.DefaultSelector()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    server.bind(str(socket_path)); os.chmod(socket_path, 0o600); server.listen(2); server.setblocking(False)
    sel.register(server, selectors.EVENT_READ)
    clients = {}
    last_request = None
    pixels = None
    revision = 0
    cached_key, points = None, []
    width, height = 1040, 570
    try:
        while running:
            try:
                raw_request = request_path.read_text()
                request = json.loads(raw_request)
            except (OSError, ValueError):
                time.sleep(.05); continue
            if not request.get("active"): break
            db_path = Path(data_dir) / "history.sqlite3"
            wal_path = Path(str(db_path) + "-wal")
            db_version = tuple((p.stat().st_mtime_ns if p.exists() else 0) for p in (db_path, wal_path))
            finish = int(request["finish"])
            key = (request.get("device"), finish // 60, request["hours"], db_version)
            if key != cached_key:
                points = read_points(data_dir, request.get("device"), finish - int(request["hours"]) * 3600, finish)
                cached_key = key
                last_request = None
            if raw_request != last_request:
                pixels = draw_chart(points, request, width, height)
                revision += 1
                last_request = raw_request
            for event, _ in sel.select(.05):
                if event.fileobj is server:
                    peer, _ = server.accept()
                    peer.setblocking(False)
                    credentials = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                    if struct.unpack("3i", credentials)[1] != os.getuid():
                        peer.close(); continue
                    fd = os.memfd_create("battery-graph", os.MFD_ALLOW_SEALING | os.MFD_CLOEXEC)
                    os.ftruncate(fd, width * height * 4)
                    fcntl.fcntl(fd, fcntl.F_ADD_SEALS, fcntl.F_SEAL_SHRINK)
                    clients[peer] = {"fd": fd, "map": mmap.mmap(fd, width * height * 4), "hello": False,
                                     "pending": False, "sent": -1, "attached": False}
                    sel.register(peer, selectors.EVENT_READ)
                else:
                    peer = event.fileobj
                    state = clients[peer]
                    try:
                        packet = peer.recv(64)
                        if packet == struct.pack("<II", 1, 1) and not state["hello"]:
                            state["hello"] = True
                        elif packet == struct.pack("<II", 2, 0) and state["hello"] and not state["pending"]:
                            state["pending"] = True
                        else:
                            raise ConnectionError("invalid packet or disconnected")
                    except (OSError, ConnectionError):
                        sel.unregister(peer); peer.close()
                        state["map"].close(); os.close(state["fd"]); del clients[peer]
            for peer, state in list(clients.items()):
                if not state["pending"] or state["sent"] == revision: continue
                try:
                    state["map"][:] = pixels
                    if not state["attached"]:
                        peer.sendmsg([struct.pack("<IIII", 1, 0, width, height)],
                                     [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [state["fd"]]))])
                        state["attached"] = True
                    peer.send(struct.pack("<II", 2, 0))
                    state["pending"] = False; state["sent"] = revision
                except OSError:
                    sel.unregister(peer); peer.close()
                    state["map"].close(); os.close(state["fd"]); del clients[peer]
    finally:
        for peer, state in clients.items():
            peer.close(); state["map"].close(); os.close(state["fd"])
        sel.close(); server.close()
        socket_path.unlink(missing_ok=True)
        request_path.unlink(missing_ok=True)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["collect", "render"])
    parser.add_argument("data_dir")
    parser.add_argument("request", nargs="?")
    parser.add_argument("socket", nargs="?")
    args = parser.parse_args()
    if args.command == "collect":
        print(json.dumps(collect(args.data_dir), ensure_ascii=False))
    else:
        if not args.request or not args.socket: parser.error("render needs request and socket paths")
        stream(args.data_dir, args.request, args.socket)


if __name__ == "__main__":
    main()
