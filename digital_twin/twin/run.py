#!/usr/bin/env python3
"""Pebble digital twin - entry point.

    python3 twin/run.py                 # serve viewer + MCU emulator on :8730
    python3 twin/run.py --validate      # offline motion check, exits non-zero on issues

Endpoints:
    http://localhost:8730/          3D viewer
    ws://localhost:8730/brain       MCU protocol  (docs/protocol.md) - brain service
    ws://localhost:8730/viewer      50 Hz pose stream - browser only
"""

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mcu as mcu_mod            # noqa: E402
import motion                    # noqa: E402
import ws                        # noqa: E402

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".json": "application/json",
         ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
         ".stl": "model/stl"}

ROBOT = None     # populated in main()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PebbleTwin/0.1"

    def log_message(self, fmt, *args):
        pass                                     # quiet; the twin logs its own events

    # -- websocket endpoints --------------------------------------------

    def _serve_ws(self, on_connect, on_message, on_close):
        conn = ws.upgrade(self)
        if conn is None:
            self.send_error(400, "expected a websocket upgrade")
            return
        on_connect(conn)
        try:
            while True:
                msg = conn.recv_json()
                if msg:
                    on_message(msg)
        except ws.WSClosed:
            pass
        except Exception as exc:                 # keep one bad client from killing the twin
            print(f"  ws error ({self.path}): {exc}")
        finally:
            on_close(conn)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        m = self.server.mcu

        if path == "/brain":
            def closed(c):
                if m.brain is c:
                    m.brain = None
                m.log("brain", "disconnected")
            return self._serve_ws(m.on_brain_connect, m.on_brain, closed)

        if path == "/viewer":
            def opened(c):
                m.viewers.append(c)
                c.send_json({"type": "init", "robot": ROBOT,
                             "motions": sorted(m.motions),
                             "characters": [
                                 {"id": c2["character_id"], "name": c2["display_name"],
                                  "colour": c2["eye_style"]["colour"],
                                  "profile": c2["motion_profile"]}
                                 for c2 in m.characters.values()],
                             "expressions": list(mcu_mod.EYE_EXPRESSIONS)})

            def closed(c):
                if c in m.viewers:
                    m.viewers.remove(c)
            return self._serve_ws(opened, m.on_viewer, closed)

        if path == "/robot.json":
            derived = os.path.join(os.path.dirname(WEB), "..", "config",
                                   "urdf_derived.json")
            derived = os.path.normpath(derived)
            if not os.path.isfile(derived):
                return self.send_error(404, "run tools/bake_urdf.py first")
            with open(derived, "rb") as fh:
                return self._send(200, "application/json", fh.read())

        if path == "/health":
            return self._send(200, "application/json",
                              json.dumps({"ok": True, "fw": mcu_mod.FW_VERSION,
                                          "brain": bool(m.brain)}).encode())

        # -- static ------------------------------------------------------
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.normpath(os.path.join(WEB, rel))
        if not full.startswith(WEB) or not os.path.isfile(full):
            return self.send_error(404, "not found")
        with open(full, "rb") as fh:
            body = fh.read()
        ctype = TYPES.get(os.path.splitext(full)[1], "application/octet-stream")
        self._send(200, ctype, body)

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def run_validate():
    chars, _ = mcu_mod.load_characters()
    profiles = {c["display_name"]: c["motion_profile"] for c in chars.values()}
    errors, notes = motion.validate(profiles=profiles)
    n = len(motion.load_motions())
    try:
        import linkage
        hr = linkage.Linkage(motion.load_robot()).headroom()
        print(f"linkage envelope: pitch-only +/-{hr['pitch_only_deg']:.0f}deg, "
              f"roll-only +/-{hr['roll_only_deg']:.0f}deg "
              f"(servo stop {hr['servo_limit_deg']:.0f}deg)")
    except Exception:
        pass
    if errors:
        print(f"FAIL - {len(errors)} limit/velocity/servo violation(s):")
        for e in errors:
            print("  x", e)
    else:
        print(f"OK - {n} motions x {len(profiles)} characters, "
              "no limit or velocity violations")
    if notes:
        print(f"\n{len(notes)} motion(s) with reduced pose headroom:")
        for nt in notes:
            print("  !", nt)
    return 1 if errors else 0


def serve(mcu, host="0.0.0.0", port=8730):
    """Build the HTTP/WebSocket server around an existing PebbleMCU.

    Split out of main() so the ROS 2 node can host the same viewer and the same
    /brain endpoint rather than reimplementing them.
    """
    global ROBOT
    ROBOT = mcu.robot
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    httpd.mcu = mcu
    return httpd


def main():
    ap = argparse.ArgumentParser(description="Pebble digital twin")
    ap.add_argument("--port", type=int, default=8730)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--robot", nargs="?", const=9000, type=int, metavar="PORT",
                    help="also drive the PHYSICAL robot: listen for its ESP32 on PORT "
                         "(default 9000); eyes + RFID, servos only with --robot-motion")
    ap.add_argument("--robot-motion", action="store_true",
                    help="with --robot: move the real neck servos (capped, see "
                         "config/robot_hw.yaml)")
    ap.add_argument("--validate", action="store_true",
                    help="check motions against joint limits and exit")
    args = ap.parse_args()

    if args.validate:
        sys.exit(run_validate())

    m = mcu_mod.PebbleMCU()
    ROBOT = m.robot

    stop = threading.Event()
    threading.Thread(target=m.run, args=(stop,), daemon=True).start()

    httpd = serve(m, args.host, args.port)

    bridge = None
    if args.robot:
        import logging
        import robot as robot_mod
        import robot_link
        logging.basicConfig(level=logging.INFO, format="  %(name)s: %(message)s")
        bridge = robot_mod.RobotBridge(m, robot_link.RobotLink(args.robot),
                                       motion=args.robot_motion)
        bridge.start()

    print(f"Pebble digital twin  fw={mcu_mod.FW_VERSION}  {m.tick_hz:.0f} Hz")
    print(f"  viewer   http://localhost:{args.port}/")
    print(f"  brain    ws://localhost:{args.port}/brain")
    print(f"  shells   {', '.join(sorted(m.characters))}")
    if bridge:
        print(f"  robot    ESP32 -> :{args.robot}  eyes+RFID"
              + (f", servos capped at +/-{bridge.limit:g} deg" if args.robot_motion
                 else ", servos OFF (add --robot-motion)"))
    print("Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        if bridge:
            bridge.stop.set()
            if bridge.motion_on:
                bridge.park()
        stop.set()
        httpd.shutdown()


if __name__ == "__main__":
    main()
