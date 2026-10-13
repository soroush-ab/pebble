#!/usr/bin/env python3
"""PC side of the Pebble bridge: listens for the ESP32 and decodes its streams.

    python3 host/pebble_link.py [--port 9000] [--wav out.wav]

The ESP32 dials out to this machine, so start this first. It prints a live
summary and can record the analog array to a WAV for offline bearing work.

Timebase note: `sample_index` is exact (it counts ADC scans), `t_us` only
anchors that to wall time. Never time audio by packet arrival - Wi-Fi jitter
is tens of milliseconds and would destroy any TDOA estimate.
"""
import argparse, socket, struct, sys, time, wave

try:
    import numpy as np
except ImportError:
    np = None

MAGIC = 0x5042
HDR = struct.Struct("<HBBII")          # magic, type, flags, len, seq

(HELLO, LOG, PONG) = (0x01, 0x02, 0x03)
(MIC_ANALOG, MIC_DIGITAL) = (0x10, 0x11)
(ENCODER, RFID, MOTION) = (0x20, 0x21, 0x22)
(PING, CONFIG) = (0x80, 0x81)
(DISP_BLIT, DISP_FILL) = (0x90, 0x91)
(MOTION_SET, MOTION_EN) = (0xA0, 0xA1)

AUDIO_HDR = struct.Struct("<QQHBB")    # t_us, sample_index, n_frames, n_ch, bits


def send(sock, mtype, payload=b""):
    sock.sendall(HDR.pack(MAGIC, mtype, 0, len(payload), 0) + payload)


def disp_fill(sock, which, x, y, w, h, color):
    send(sock, DISP_FILL, struct.pack("<BBHHHHH", which, 0, x, y, w, h, color))


def _wav_block(pcm, nch, dc, gain):
    """Raw ADC counts -> listenable PCM.

    Two corrections, both necessary:
      * remove the per-channel DC bias. The AUDIO pin idles at VCC/2 (~1500
        counts), so the raw stream is a large constant with a tiny signal on
        top - as a WAV that is a DC step, not audio.
      * apply gain. The useful swing is tens of counts in a +/-32768 container
        (about -56 dBFS), so without scaling you turn the volume up and hear
        the noise floor instead of the signal.
    The DC estimate is a slow IIR so it tracks supply drift without following
    the audio itself.
    """
    if np is not None:
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        n = x.size // nch
        if n == 0:
            return b""
        x = x[:n * nch].reshape(n, nch)
        for c in range(nch):
            m = float(x[:, c].mean())
            dc[c] = m if dc[c] == 0.0 else dc[c] * 0.95 + m * 0.05
        y = (x - np.asarray(dc[:nch], dtype=np.float32)) * gain
        return np.clip(y, -32768, 32767).astype("<i2").tobytes()

    out = bytearray()
    raw = struct.unpack(f"<{len(pcm)//2}h", bytes(pcm))
    for i in range(0, len(raw) - nch + 1, nch):
        for c in range(nch):
            v = int((raw[i + c] - dc[c]) * gain)
            out += struct.pack("<h", max(-32768, min(32767, v)))
    return bytes(out)


def set_streams(sock, analog=1, digital=0, encoder=1, rfid=1,
                encoder_hz=25, rfid_hz=10):
    """Gate the uplink streams.

    digital defaults OFF: an unwired I2S mic still streams full-rate zeros,
    which was eating most of the link budget for nothing."""
    send(sock, CONFIG, struct.pack("<BBBBHH", analog, digital, encoder, rfid,
                                   encoder_hz, rfid_hz))


def motion_set(sock, base_deg, sa=0.0, sb=0.0, max_vel=0.0):
    send(sock, MOTION_SET, struct.pack("<ffff", base_deg, sa, sb, max_vel))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--wav", help="record the 3-mic array to this WAV file")
    ap.add_argument("--gain", type=float, default=0.0,
                    help="WAV gain; 0 = auto peak-normalise to -1 dBFS (default)")
    ap.add_argument("--digital", type=int, default=0,
                    help="1 to also stream the I2S mic (default off)")
    a = ap.parse_args()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", a.port))
    srv.listen(1)
    print(f"waiting for the robot on :{a.port} ...")
    sock, peer = srv.accept()
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    print(f"connected: {peer[0]}")

    # The WAV is opened LAZILY, after HELLO, because the sample rate must be
    # the robot's measured one. Writing the nominal 27777 into the header when
    # the ADC actually runs at ~13890 plays the file back at double speed,
    # which on its own makes speech sound like noise.
    # PCM is buffered and the WAV header written at close, using the rate
    # measured from the robot's own timestamps. The first HELLO necessarily
    # quotes the nominal rate (the ADC has not been measured yet), and a WAV
    # header cannot be changed after frames are written - so opening early
    # bakes in the wrong rate and plays everything back at the wrong speed.
    wav_buf = bytearray() if a.wav else None
    wav_nch = 3
    wav_dc = [0.0, 0.0, 0.0]

    buf = b""
    stats = {"analog": 0, "digital": 0, "bytes": 0, "gaps": 0}
    next_index = None
    anchor = None          # (t_us, sample_index) of the first analog frame
    t0 = time.time()
    last_print = 0.0

    try:
        while True:
            d = sock.recv(1 << 18)
            if not d:
                print("\nrobot disconnected")
                break
            buf += d
            stats["bytes"] += len(d)

            # Walk with an offset and compact ONCE per recv. Re-slicing
            # `buf = buf[n:]` for every frame copies the whole remainder each
            # time, which is O(n^2) and could not keep up with 0.9 Mbit/s -
            # the socket backed up and the robot dropped the connection.
            # Parse by OFFSET and compact once per recv. The original
            # `buf = buf[n:]` per frame re-copied the whole remainder every
            # time - O(n^2), which could not keep up with 0.9 Mbit/s and made
            # the robot drop the link. (memoryview looks tempting here but a
            # live export blocks resizing the buffer afterwards.)
            off = 0
            while len(buf) - off >= HDR.size:
                magic, mtype, _flags, ln, _seq = HDR.unpack_from(buf, off)
                if magic != MAGIC:
                    print("framing lost"); off = len(buf); break
                if len(buf) - off < HDR.size + ln:
                    break
                p = buf[off + HDR.size: off + HDR.size + ln]
                off += HDR.size + ln

                if mtype == HELLO:
                    v, n_an, fs_ch, fs_tot = struct.unpack_from("<BBHI", p)
                    skew = struct.unpack_from("<4i", p, 8)
                    again = stats.get("hello", 0)
                    stats["hello"] = again + 1
                    print(f"\n{'HELLO (corrected)' if again else 'HELLO'} v{v}: "
                          f"{n_an} analog mics @ {fs_ch} Hz/ch ({fs_tot} Hz total)")
                    print(f"  ADC scan skew: {[s/1000 for s in skew[:3]]} us "
                          f"<- subtract these before any TDOA")
                    set_streams(sock, analog=1, digital=a.digital,
                                encoder=1, rfid=1)
                    print(f"  requested streams: analog=on "
                          f"digital={'on' if a.digital else 'off'}")
                elif mtype == LOG:
                    print("  [esp]", p.decode("utf-8", "replace"))
                elif mtype in (MIC_ANALOG, MIC_DIGITAL):
                    t_us, idx, n, nch, bits = AUDIO_HDR.unpack_from(p)
                    nch = nch or 3
                    pcm = p[AUDIO_HDR.size:]
                    if mtype == MIC_ANALOG:
                        stats["analog"] += n
                        # True rate from the ROBOT's clock, not from packet
                        # arrival. If this disagrees with the rate in HELLO,
                        # every delay the PC computes is scaled wrong.
                        if anchor is None:
                            anchor = (t_us, idx)
                        elif t_us > anchor[0] + 2_000_000:
                            stats["fs"] = ((idx - anchor[1]) * 1e6
                                           / (t_us - anchor[0]))
                        if next_index is not None and idx != next_index:
                            stats["gaps"] += 1
                        next_index = idx + n
                        if wav_buf is not None:
                            wav_nch = nch
                            wav_buf += _wav_block(pcm, nch, wav_dc, 1.0)
                    else:
                        stats["digital"] += n
                elif mtype == ENCODER:
                    t_us, raw, deg, st = struct.unpack("<QHfB", p)
                    stats["enc"] = f"{deg:7.2f} deg (0x{st:02x})"
                elif mtype == RFID:
                    t_us, pres, ulen = struct.unpack_from("<QBB", p)
                    uid = p[10:10 + ulen].hex().upper()
                    stats["rfid"] = uid if pres else "-"
                elif mtype == MOTION:
                    t_us, base, sa, sb, moving_, en = struct.unpack("<QfffBB", p)
                    stats["motion"] = f"base {base:7.2f} servo {sa:+5.1f}/{sb:+5.1f}"

            if off:
                buf = buf[off:]          # one copy per recv, not per frame

            now = time.time()
            el = now - t0
            # wait for a real measurement window: dividing by a few milliseconds
            # prints a meaningless "400 kHz / 27 Mb/s" on the first line
            if now - last_print > 0.5 and el > 1.0:
                print(f"\r{el:6.1f}s  analog {stats['analog']/el:7.0f} Hz  "
                      f"digital {stats['digital']/el:6.0f} Hz  "
                      f"{stats['bytes']*8/el/1e6:5.2f} Mb/s  gaps {stats['gaps']}  "
                      f"fs {stats.get('fs',0):7.0f} Hz  "
                      f"enc {stats.get('enc','-')}  rfid {stats.get('rfid','-')}  ",
                      end="", flush=True)
                last_print = now
    except KeyboardInterrupt:
        print()
    finally:
        if wav_buf is not None and len(wav_buf):
            # Prefer the rate derived from the robot's clock; fall back to
            # whatever HELLO last claimed.
            fs = int(stats.get("fs") or 0) or 13890

            # Normalise against the ACTUAL peak rather than a guessed gain.
            # The useful swing is only tens of counts, but transients are much
            # larger; a fixed gain big enough to hear the quiet parts clips the
            # loud ones, and clipping is what makes a good recording sound like
            # noise. Peak-normalising to -1 dBFS cannot clip by construction.
            pcm_all = bytes(wav_buf)
            if np is not None:
                y = np.frombuffer(pcm_all, dtype="<i2").astype(np.float32)
                pk = float(np.abs(y).max()) or 1.0
                g = (0.89 * 32767.0 / pk) if a.gain <= 0 else a.gain
                y = np.clip(y * g, -32768, 32767).astype("<i2")
                pcm_all = y.tobytes()
                clipped = int((np.abs(y) >= 32767).sum())
            else:
                g, clipped = (a.gain if a.gain > 0 else 1.0), 0

            w = wave.open(a.wav, "wb")
            w.setnchannels(wav_nch); w.setsampwidth(2); w.setframerate(fs)
            w.writeframes(pcm_all)
            w.close()
            secs = len(pcm_all) / (2 * wav_nch * fs)
            print(f"wrote {a.wav}: {wav_nch} ch, {fs} Hz, {secs:.1f} s, "
                  f"gain x{g:.1f}{' (auto)' if a.gain <= 0 else ''}, "
                  f"{clipped} clipped samples")
        sock.close()


if __name__ == "__main__":
    main()
