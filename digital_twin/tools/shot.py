#!/usr/bin/env python3
"""Headless screenshot + page health check, driving Firefox over W3C WebDriver.

No pip, no node: geckodriver speaks plain HTTP/JSON and ships with the Firefox
snap, so this is stdlib only.

    python3 tools/shot.py                       # default: the twin viewer
    python3 tools/shot.py --url ... --out x.png
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

GECKO = shutil.which("firefox.geckodriver") or shutil.which("geckodriver")


def rq(method, url, body=None, timeout=90):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8730/")
    ap.add_argument("--out", default="shots/viewer.png")
    ap.add_argument("--wait", type=float, default=6.0,
                    help="seconds to let WebGL + mesh fetches settle")
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=950)
    ap.add_argument("--port", type=int, default=4444)
    ap.add_argument("--script", help="extra JS to evaluate; result is printed")
    ap.add_argument("--after", type=float, default=0.0,
                    help="seconds to wait after --script before the screenshot "
                         "(use when the script triggers a motion)")
    args = ap.parse_args()

    if not GECKO:
        sys.exit("geckodriver not found (try: snap install firefox)")

    drv = subprocess.Popen([GECKO, "--port", str(args.port)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{args.port}"
    try:
        for _ in range(60):                      # wait for the driver to listen
            try:
                urllib.request.urlopen(base + "/status", timeout=1)
                break
            except OSError:
                time.sleep(0.25)

        caps = {"capabilities": {"alwaysMatch": {
            "browserName": "firefox",
            "moz:firefoxOptions": {
                "args": ["-headless", f"--width={args.width}",
                         f"--height={args.height}"],
                "prefs": {
                    # software WebGL so a headless box still renders three.js
                    "webgl.force-enabled": True,
                    "webgl.disabled": False,
                    "gfx.webrender.all": True,
                    "layers.acceleration.force-enabled": False,
                },
            }}}}
        s = rq("POST", base + "/session", caps)
        if "value" not in s or "sessionId" not in s.get("value", {}):
            sys.exit(f"session failed: {json.dumps(s)[:400]}")
        sid = s["value"]["sessionId"]
        sess = f"{base}/session/{sid}"

        try:
            rq("POST", sess + "/window/rect",
               {"width": args.width, "height": args.height, "x": 0, "y": 0})
            rq("POST", sess + "/url", {"url": args.url})
            time.sleep(args.wait)

            probe = """
              const out = {
                title: document.title,
                status: (document.getElementById('fw')||{}).textContent,
                errors: window.__errors || [],
                canvas: !!document.querySelector('canvas'),
              };
              const c = document.querySelector('canvas');
              if (c) {
                out.canvasSize = [c.width, c.height];
                const gl = c.getContext('webgl2') || c.getContext('webgl');
                out.webgl = !!gl;
                if (gl) {
                  const d = gl.getExtension('WEBGL_debug_renderer_info');
                  out.renderer = d ? gl.getParameter(d.UNMASKED_RENDERER_WEBGL)
                                   : gl.getParameter(gl.RENDERER);
                  // non-black pixels = something actually drew
                  const px = new Uint8Array(4 * 400);
                  try {
                    gl.readPixels(0, 0, 20, 20, gl.RGBA, gl.UNSIGNED_BYTE, px);
                    out.nonBlank = px.some((v, i) => i % 4 !== 3 && v > 8);
                  } catch (e) { out.readPixels = String(e); }
                }
              }
              if (window.__twin) {
                out.twin = {
                  joints: Object.keys(window.__twin.pivots || {}),
                  meshes: window.__twin.meshCount,
                  chain: (window.__twin.meta || {}).chain,
                };
              }
              return JSON.stringify(out);
            """
            r = rq("POST", sess + "/execute/sync", {"script": probe, "args": []})
            print("page probe:")
            try:
                print(json.dumps(json.loads(r["value"]), indent=2))
            except Exception:
                print("  " + json.dumps(r)[:600])

            if args.script:
                r2 = rq("POST", sess + "/execute/sync",
                        {"script": args.script, "args": []})
                print("script:", json.dumps(r2.get("value"))[:1200])
                if args.after:
                    time.sleep(args.after)

            shot = rq("GET", sess + "/screenshot")
            b64 = shot.get("value")
            if isinstance(b64, str):
                import base64
                os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
                with open(args.out, "wb") as fh:
                    fh.write(base64.b64decode(b64))
                print(f"\nsaved {args.out} ({os.path.getsize(args.out)/1024:.0f} KB)")
            else:
                print("screenshot failed:", json.dumps(shot)[:300])
        finally:
            rq("DELETE", sess)
    finally:
        drv.terminate()
        try:
            drv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            drv.kill()


if __name__ == "__main__":
    main()
