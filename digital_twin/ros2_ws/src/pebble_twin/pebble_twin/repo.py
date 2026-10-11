"""Locate the Pebble repo root so the ROS node can reuse the twin's engine.

The point of this module is that there is exactly ONE motion engine, ONE
`motions.yaml` and ONE linkage model. The ROS 2 node imports them rather than
carrying a copy, so RViz, the web viewer and the firmware can never disagree.

The workspace lives inside the repo, so walking up from this file finds the
root whether we are running from source or from colcon's install/ tree.
$PEBBLE_REPO overrides it.
"""

import os
import sys

MARKERS = ("motions.yaml", "twin", "characters")


def find_repo():
    env = os.environ.get("PEBBLE_REPO")
    if env and all(os.path.exists(os.path.join(env, m)) for m in MARKERS):
        return env
    here = os.path.abspath(__file__)
    for _ in range(12):
        here = os.path.dirname(here)
        if all(os.path.exists(os.path.join(here, m)) for m in MARKERS):
            return here
        if here == os.path.dirname(here):
            break
    raise RuntimeError(
        "Pebble repo not found. Set PEBBLE_REPO to the directory holding "
        "motions.yaml.")


def add_twin_to_path():
    root = find_repo()
    twin = os.path.join(root, "twin")
    if twin not in sys.path:
        sys.path.insert(0, twin)
    return root
