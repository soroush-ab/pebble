"""Link to the physical Pebble (firmware in `../firmware`, `main/proto.h`).

The real robot is a thin I/O bridge: it dials out to this PC over TCP, streams
its sensors and applies setpoints. This module speaks that binary protocol so
the twin's MCU logic (twin/mcu.py) can drive the real hardware unchanged:

    link = RobotLink(port=9000)          # the ESP32 connects to us
    link.wait(timeout=30)
    link.blit(0, 0, 0, 240, 240, rgb565_bytes)
    link.motion_enable(base=False, servos=True)
    link.motion_set(base_deg, servo_a_deg, servo_b_deg)

Everything here is transport. Safety limits for moving the hardware live in
twin/robot.py, where the joint semantics are known.
"""

import logging
import socket
import struct
import threading
import time

log = logging.getLogger("pebble.robot_link")

MAGIC = 0x5042
HDR = struct.Struct("<HBBII")              # magic, type, flags, len, seq

HELLO, LOG, PONG = 0x01, 0x02, 0x03
MIC_ANALOG, MIC_DIGITAL = 0x10, 0x11
ENCODER, RFID, MOTION, FRAME_INFO = 0x20, 0x21, 0x22, 0x23
PING, CONFIG = 0x80, 0x81
DISP_BLIT, DISP_FILL = 0x90, 0x91
FRAME_STORE, FRAME_SHOW, FRAME_ANIM, FRAME_CLEAR, FRAME_QUERY = 0x92, 0x93, 0x94, 0x95, 0x96
FRAME_NONE = 0xFFFF
FRAME_CHUNK = 60 * 1024                    # < firmware RX buffer (72 kB) incl. header
MOTION_SET, MOTION_EN = 0xA0, 0xA1

HELLO_T = struct.Struct("<BBHI4iIBBHH4ffQ24s")
AUDIO_HDR = struct.Struct("<QQHBB")        # t_us, sample_index, n_frames, n_ch, bits
ENCODER_T = struct.Struct("<QHfB")
RFID_T = struct.Struct("<QBB10s")
MOTION_T = struct.Struct("<QfffBB")
FRAME_INFO_T = struct.Struct("<HHII")      # count, max_id, bytes, psram_free


class RobotLink:
    """TCP server the robot dials into. Reconnects are handled transparently;
    `generation` increases on every new connection (a reboot looks like one)."""

    def __init__(self, port=9000, host="0.0.0.0"):
        self.port, self.host = port, host
        self.sock = None
        self.peer = None
        self.hello = None
        self.generation = 0
        self.motion = None          # latest motion_state: dict
        self.encoder = None
        self.rfid = None            # latest {"present", "uid"}
        self.on_rfid = None         # callback(uid_hex or None)
        self.on_audio = None        # callback(kind, header dict, int16 bytes)
        self.on_connect = None      # callback(hello dict)
        self.logs = []
        self.frame_info = None
        self._frame_info_evt = threading.Event()
        self._send_lock = threading.Lock()
        self._connected = threading.Event()
        threading.Thread(target=self._serve, daemon=True, name="robot-link").start()

    # ------------------------------------------------------------ uplink

    def _serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(1)
        log.info("waiting for the robot on :%d", self.port)
        while True:
            s, peer = srv.accept()
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._send_lock:
                if self.sock:
                    try:
                        self.sock.close()
                    except OSError:
                        pass
                self.sock, self.peer = s, peer[0]
            self.generation += 1
            log.info("robot connected from %s (connection %d)", peer[0], self.generation)
            try:
                self._read(s)
            except OSError as e:
                log.warning("robot link error: %s", e)
            log.warning("robot disconnected")
            self._connected.clear()
            with self._send_lock:
                if self.sock is s:
                    self.sock = None

    def _read(self, s):
        buf = b""
        while True:
            d = s.recv(1 << 18)
            if not d:
                return
            buf += d
            off = 0
            while len(buf) - off >= HDR.size:
                magic, mtype, _flags, ln, _seq = HDR.unpack_from(buf, off)
                if magic != MAGIC:
                    log.error("framing lost; dropping %d bytes", len(buf) - off)
                    off = len(buf)
                    break
                if len(buf) - off < HDR.size + ln:
                    break
                payload = buf[off + HDR.size: off + HDR.size + ln]
                off += HDR.size + ln
                try:
                    self._handle(mtype, payload)
                except Exception as e:  # noqa: BLE001 - one bad frame must not kill the link
                    log.warning("bad frame type %#x: %s", mtype, e)
            buf = buf[off:]

    def _handle(self, mtype, p):
        if mtype == HELLO and len(p) >= HELLO_T.size:
            f = HELLO_T.unpack_from(p)
            self.hello = {"version": f[0], "n_analog_mics": f[1], "analog_fs_per_ch": f[2],
                          "digital_fs": f[8], "n_displays": f[10], "disp_w": f[11],
                          "disp_h": f[12], "fw": f[19].split(b"\0")[0].decode(errors="replace")}
            self._connected.set()
            if self.on_connect:
                self.on_connect(self.hello)
        elif mtype == LOG:
            text = p.decode(errors="replace").strip()
            self.logs.append(text)
            del self.logs[:-200]
            log.info("robot: %s", text)
        elif mtype == ENCODER and len(p) >= ENCODER_T.size:
            t, raw, deg, status = ENCODER_T.unpack_from(p)
            self.encoder = {"t_us": t, "raw": raw, "deg": deg, "status": status}
        elif mtype == MOTION and len(p) >= MOTION_T.size:
            t, base, sa, sb, moving, enabled = MOTION_T.unpack_from(p)
            self.motion = {"t_us": t, "base_deg": base, "servo_a_deg": sa,
                           "servo_b_deg": sb, "moving": bool(moving), "enabled": bool(enabled),
                           "at": time.time()}
        elif mtype == FRAME_INFO and len(p) >= FRAME_INFO_T.size:
            c, mx, b, free = FRAME_INFO_T.unpack_from(p)
            self.frame_info = {"count": c, "max_id": mx, "bytes": b, "psram_free": free}
            self._frame_info_evt.set()
        elif mtype == RFID and len(p) >= RFID_T.size:
            t, present, n, uid = RFID_T.unpack_from(p)
            uid_hex = uid[:n].hex().upper() if present else None
            changed = uid_hex != (self.rfid or {}).get("uid")
            self.rfid = {"present": bool(present), "uid": uid_hex}
            if changed and self.on_rfid:
                self.on_rfid(uid_hex)
        elif mtype in (MIC_ANALOG, MIC_DIGITAL) and self.on_audio and len(p) >= AUDIO_HDR.size:
            t, idx, n, nch, bits = AUDIO_HDR.unpack_from(p)
            self.on_audio("analog" if mtype == MIC_ANALOG else "digital",
                          {"t_us": t, "sample_index": idx, "n_frames": n, "n_ch": nch},
                          p[AUDIO_HDR.size:])

    # ---------------------------------------------------------- downlink

    @property
    def connected(self):
        return self._connected.is_set() and self.sock is not None

    def wait(self, timeout=None):
        return self._connected.wait(timeout)

    def send(self, mtype, payload=b""):
        with self._send_lock:
            if not self.sock:
                return False
            try:
                self.sock.sendall(HDR.pack(MAGIC, mtype, 0, len(payload), 0) + payload)
                return True
            except OSError as e:
                log.warning("send failed: %s", e)
                return False

    def config(self, analog=0, digital=0, encoder=1, rfid=1, encoder_hz=25, rfid_hz=10):
        return self.send(CONFIG, struct.pack("<BBBBHH", analog, digital, encoder, rfid,
                                             encoder_hz, rfid_hz))

    def fill(self, which, x, y, w, h, rgb565):
        return self.send(DISP_FILL, struct.pack("<BBHHHHH", which, 0, x, y, w, h, rgb565))

    def blit(self, which, x, y, w, h, pixels):
        """`pixels`: w*h*2 bytes, already in the panel's byte order."""
        assert len(pixels) == w * h * 2, (len(pixels), w, h)
        return self.send(DISP_BLIT, struct.pack("<BBHHHH", which, 0, x, y, w, h) + pixels)

    # ----- stored eye frames (firmware "pebble-bridge 2 frames" and later)

    @property
    def has_frames(self):
        return "frames" in (self.hello or {}).get("fw", "")

    def frame_store(self, frame_id, rle):
        """Upload one full-panel RLE16 frame, in chunks the firmware can buffer."""
        for off in range(0, len(rle), FRAME_CHUNK):
            chunk = rle[off:off + FRAME_CHUNK]
            if not self.send(FRAME_STORE, struct.pack("<HBBII", frame_id, 1, 0, len(rle), off)
                             + chunk):
                return False
        return True

    def frame_show(self, left, right):
        return self.send(FRAME_SHOW, struct.pack("<HH", left, right))

    def frame_anim(self, pairs, frame_ms):
        pairs = list(pairs)[:16]
        return self.send(FRAME_ANIM, struct.pack("<HBB", int(frame_ms), len(pairs), 0)
                         + b"".join(struct.pack("<HH", l, r) for l, r in pairs))

    def frame_clear(self):
        return self.send(FRAME_CLEAR)

    def frame_query(self, timeout=10.0):
        """Round trip: also proves every earlier message was processed."""
        self._frame_info_evt.clear()
        if not self.send(FRAME_QUERY):
            return None
        return self.frame_info if self._frame_info_evt.wait(timeout) else None

    def motion_enable(self, base, servos):
        return self.send(MOTION_EN, struct.pack("<BB", int(bool(base)), int(bool(servos))))

    def motion_set(self, base_deg, servo_a_deg, servo_b_deg, max_vel_dps=0.0):
        return self.send(MOTION_SET, struct.pack("<ffff", base_deg, servo_a_deg,
                                                 servo_b_deg, max_vel_dps))
