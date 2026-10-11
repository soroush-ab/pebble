"""Pebble MCU emulator as a ROS 2 node.

Runs the SAME PebbleMCU the standalone twin runs, and exposes it three ways at
once, from one process:

  * /joint_states -> robot_state_publisher -> TF -> RViz
  * ROS topics for motions, eyes, gaze, shell swap and speaker position
  * the original HTTP viewer and /brain WebSocket on :8730, unchanged

So `examples/brain_stub.py` drives the ROS robot with no modification, and
`motions.yaml` remains the single source of motion truth.

Joint sign convention: the CAD's yaw axis points along -Z, so a positive
base_yaw in OUR convention (counter-clockwise from above) is published to the
URDF joint negated. `frame.yaw_sign` from the bake carries that.
"""

import json
import math
import os
import threading

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSProfile

from sensor_msgs.msg import JointState
from std_msgs.msg import Float32, Float32MultiArray, String
from visualization_msgs.msg import Marker, MarkerArray

from .repo import add_twin_to_path

REPO = add_twin_to_path()

import mcu as mcu_mod          # noqa: E402
import run as run_mod          # noqa: E402

JOINT_NAMES = {"base_yaw": "base_yaw",
               "head_roll": "head_roll",
               "head_pitch": "head_pitch"}


class PebbleNode(Node):
    def __init__(self):
        super().__init__("pebble_mcu")

        self.declare_parameter("web_port", 8730)
        self.declare_parameter("serve_web", True)
        self.declare_parameter("publish_rate", 50.0)

        self.mcu = mcu_mod.PebbleMCU()
        derived_path = os.path.join(REPO, "config", "urdf_derived.json")
        with open(derived_path) as fh:
            self.derived = json.load(fh)
        self.yaw_sign = float(self.derived["frame"].get("yaw_sign", 1.0))

        latched = QoSProfile(depth=1,
                             durability=QoSDurabilityPolicy.TRANSIENT_LOCAL)

        self.pub_joints = self.create_publisher(JointState, "joint_states", 10)
        self.pub_servos = self.create_publisher(JointState, "pebble/servo_states", 10)
        self.pub_bearing = self.create_publisher(Float32, "pebble/bearing", 10)
        self.pub_char = self.create_publisher(String, "pebble/character", latched)
        self.pub_eyes = self.create_publisher(String, "pebble/eyes_state", latched)
        self.pub_marks = self.create_publisher(MarkerArray, "pebble/eye_markers", 10)

        self.create_subscription(String, "pebble/motion", self._on_motion, 10)
        self.create_subscription(String, "pebble/eyes", self._on_eyes, 10)
        self.create_subscription(String, "pebble/shell", self._on_shell, 10)
        self.create_subscription(Float32, "pebble/attend", self._on_attend, 10)
        self.create_subscription(Float32MultiArray, "pebble/speaker",
                                 self._on_speaker, 10)
        self.create_subscription(String, "pebble/utterance", self._on_utter, 10)

        rate = float(self.get_parameter("publish_rate").value)
        self.dt = 1.0 / rate
        self.create_timer(self.dt, self._tick)

        self.httpd = None
        if bool(self.get_parameter("serve_web").value):
            port = int(self.get_parameter("web_port").value)
            try:
                self.httpd = run_mod.serve(self.mcu, "0.0.0.0", port)
                threading.Thread(target=self.httpd.serve_forever,
                                 daemon=True).start()
                self.get_logger().info(f"viewer http://localhost:{port}/  "
                                       f"brain ws://localhost:{port}/brain")
            except OSError as exc:
                self.get_logger().warning(f"web server not started: {exc}")

        self.get_logger().info(
            f"repo {REPO} | motions {len(self.mcu.motions)} | "
            f"shells {sorted(self.mcu.characters)}")
        self._last_char = None
        self._last_eyes = None

    # ---------------------------------------------------------- subscribers

    def _on_motion(self, msg):
        with self.mcu.lock:
            if not self.mcu.engine.play(msg.data):
                self.get_logger().warning(f"unknown motion '{msg.data}' ignored")

    def _on_eyes(self, msg):
        with self.mcu.lock:
            self.mcu.set_eyes(msg.data)

    def _on_shell(self, msg):
        with self.mcu.lock:
            char = self.mcu.characters.get(msg.data)
            if char:
                self.mcu.attach_shell(char["rfid_uid"])
            else:
                self.get_logger().warning(f"no shell '{msg.data}'")

    def _on_attend(self, msg):
        with self.mcu.lock:
            self.mcu.attend_deg = float(msg.data)

    def _on_speaker(self, msg):
        d = list(msg.data)
        with self.mcu.lock:
            if d:
                self.mcu.speaker_deg = float(d[0])
            if len(d) > 1:
                self.mcu.speaker_dist = float(d[1])
            self.mcu.speaker_active = True

    def _on_utter(self, msg):
        with self.mcu.lock:
            self.mcu.log("user", msg.data)
            self.mcu.to_brain({"type": "utterance", "text": msg.data,
                               "bearing": self.mcu.attend_deg,
                               "character_id": (self.mcu.character or {}).get(
                                   "character_id")})

    # ----------------------------------------------------------------- tick

    def _tick(self):
        with self.mcu.lock:
            self.mcu.step(self.dt)
            frame = self.mcu.frame()
            # the web viewer is driven by this same tick; without it the page
            # connects, renders the CAD and then sits frozen
            self.mcu.broadcast(frame)

        now = self.get_clock().now().to_msg()

        js = JointState()
        js.header.stamp = now
        for ch, jname in JOINT_NAMES.items():
            sign = self.yaw_sign if ch == "base_yaw" else 1.0
            js.name.append(jname)
            js.position.append(sign * math.radians(frame["joints"][ch]))
        self.pub_joints.publish(js)

        sv = frame.get("servos") or {}
        if sv:
            m = JointState()
            m.header.stamp = now
            m.name = ["servo_a", "servo_b"]
            m.position = [math.radians(sv["a"]), math.radians(sv["b"])]
            self.pub_servos.publish(m)
            if sv.get("over"):
                self.get_logger().warning(
                    f"servo past its stop: {sv['over']} "
                    f"(a={sv['a']:.1f} b={sv['b']:.1f})")

        if frame["speaker"]["active"]:
            self.pub_bearing.publish(Float32(data=float(frame["speaker"]["deg"])))

        if frame["character"] != self._last_char:
            self._last_char = frame["character"]
            self.pub_char.publish(String(data=str(frame["character"])))
        eyes = frame["eyes"]["expression"]
        if eyes != self._last_eyes:
            self._last_eyes = eyes
            self.pub_eyes.publish(String(data=eyes))

        self.pub_marks.publish(self._eye_markers(frame, now))

    def _eye_markers(self, frame, stamp):
        """Two spheres on the eye frames, in the active character's colour."""
        arr = MarkerArray()
        colour = frame["eyes"]["colour"].lstrip("#")
        r, g, b = (int(colour[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
        open_frac = max(0.12, 1.0 - frame["eyes"]["blink"])
        for i in range(len(self.derived.get("displays", []))):
            mk = Marker()
            mk.header.frame_id = f"eye_{i}_link"
            mk.header.stamp = stamp
            mk.ns = "eyes"
            mk.id = i
            mk.type = Marker.SPHERE
            mk.action = Marker.ADD
            rad = self.derived.get("eye_radius_m", 0.0162)
            mk.scale.x = rad * 1.6
            mk.scale.y = rad * 1.6 * open_frac
            mk.scale.z = rad * 0.5
            mk.pose.position.z = 0.002
            mk.pose.orientation.w = 1.0
            mk.color.r, mk.color.g, mk.color.b, mk.color.a = r, g, b, 1.0
            arr.markers.append(mk)
        return arr

    def destroy_node(self):
        if self.httpd:
            self.httpd.shutdown()
        return super().destroy_node()


def main(argv=None):
    rclpy.init(args=argv)
    node = PebbleNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
