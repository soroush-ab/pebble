#!/usr/bin/env python3
"""Flatten a Fusion 360 URDF export into what the digital twin needs.

    python3 tools/bake_urdf.py ~/Downloads/Assembly/assembly_1

A Fusion export is 136 links and 135 joints, of which 117 are `fixed` - screws,
servo bodies, bearings. Only four bodies actually move. This script:

  1. groups every link under its nearest actuated ancestor,
  2. bakes each link's fixed-transform chain into its vertices,
  3. merges each group into one binary STL (split shell / internal for shading),
  4. emits the real joint origins, axes and limits as JSON.

Output: twin/web/meshes/*.stl  and  config/urdf_derived.json
"""

import argparse
import json
import os
import struct
import sys
import xml.etree.ElementTree as ET

import numpy as np

# fasteners and bearings: thousands of triangles, never visible
SKIP = ("screw", "bearing", "washer", "nut_", "din_")

INTERNAL = ("mg995", "nema17", "pcb", "tft", "ball", "servoend", "part_7",
            "loop_closure", "hinge")


def rpy_to_R(r, p, y):
    cr, sr, cp, sp, cy, sy = (np.cos(r), np.sin(r), np.cos(p),
                              np.sin(p), np.cos(y), np.sin(y))
    return np.array([[cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
                     [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
                     [-sp,     cp * sr,                cp * cr]])


def load_stl(path):
    with open(path, "rb") as fh:
        data = fh.read()
    n = struct.unpack("<I", data[80:84])[0]
    if len(data) != 84 + n * 50:
        return None                                   # ASCII or malformed
    buf = np.frombuffer(data, dtype=np.uint8, count=n * 50, offset=84).reshape(n, 50)
    tris = buf[:, 12:48].copy().view("<f4").reshape(n, 3, 3)
    return tris.astype(np.float64)


def find_eye_holes(tris, r_min=0.008, r_max=0.03):
    """Locate circular through-holes in a closed, thick shell mesh.

    A hole in a shell with wall thickness shows up as two loops of sharp edges
    (outer rim and inner rim). Returns [(centre, axis, radius)] with the centre
    on the outer rim and the axis pointing out of the shell, one per hole.
    """
    V = np.round(tris.reshape(-1, 3) * 1e5).astype(np.int64)
    uniq, inv = np.unique(V, axis=0, return_inverse=True)
    inv = inv.reshape(-1, 3)
    P = uniq / 1e5
    N = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    ln = np.linalg.norm(N, axis=1)
    good = ln > 1e-14
    N[good] /= ln[good, None]

    # edges shared by two faces meeting at more than ~60 degrees
    idx = np.flatnonzero(good)
    E = np.concatenate([inv[idx][:, [0, 1]], inv[idx][:, [1, 2]],
                        inv[idx][:, [2, 0]]])
    F = np.concatenate([idx, idx, idx])
    E.sort(axis=1)
    order = np.lexsort((E[:, 1], E[:, 0]))
    E, F = E[order], F[order]
    pair = np.flatnonzero((E[1:] == E[:-1]).all(axis=1))
    sharp = pair[np.einsum("ij,ij->i", N[F[pair]], N[F[pair + 1]]) < 0.5]

    adj = {}
    for a, b in E[sharp]:
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
    seen, loops = set(), []
    for s in adj:
        if s in seen:
            continue
        stack, comp = [s], []
        seen.add(s)
        while stack:
            x = stack.pop()
            comp.append(x)
            for y in adj[x]:
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        if len(comp) < 16:
            continue
        Q = P[comp]
        c = Q.mean(axis=0)
        n = np.linalg.svd(Q - c)[2][2]
        d = Q - c
        r = np.linalg.norm(d - np.outer(d @ n, n), axis=1)
        if r_min < r.mean() < r_max and r.std() / r.mean() < 0.08:
            loops.append((c, n, float(r.mean())))

    # pair outer/inner rims of the same hole: close centres, same radius
    holes, used = [], set()
    for i, (c1, n1, r1) in enumerate(loops):
        for j, (c2, n2, r2) in enumerate(loops):
            if j <= i or i in used or j in used:
                continue
            gap = np.linalg.norm(c1 - c2)
            if gap < 0.5 * min(r1, r2) and abs(r1 - r2) < 0.15 * r1:
                used |= {i, j}
                # the outer rim is the one further from the shell's centroid
                mid = tris.reshape(-1, 3).mean(axis=0)
                if np.linalg.norm(c2 - mid) > np.linalg.norm(c1 - mid):
                    c1, c2 = c2, c1
                axis = c1 - c2
                axis /= np.linalg.norm(axis)
                holes.append((c1, axis, max(r1, r2)))
    return holes


def write_stl(path, tris):
    n = len(tris)
    out = bytearray(b"\0" * 80 + struct.pack("<I", n))
    rec = np.zeros((n, 50), dtype=np.uint8)
    v = np.ascontiguousarray(tris, dtype="<f4")
    e1, e2 = v[:, 1] - v[:, 0], v[:, 2] - v[:, 0]
    nz = np.cross(e1, e2)
    ln = np.linalg.norm(nz, axis=1, keepdims=True)
    nz = np.divide(nz, ln, out=np.zeros_like(nz), where=ln > 1e-20)
    nz = np.ascontiguousarray(nz, dtype="<f4")
    rec[:, 0:12] = nz.view(np.uint8).reshape(n, 12)
    rec[:, 12:48] = v.view(np.uint8).reshape(n, 36)
    out += rec.tobytes()
    with open(path, "wb") as fh:
        fh.write(bytes(out))
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("export_dir", help="dir containing urdf/ and meshes/")
    ap.add_argument("--out-meshes", default="twin/web/meshes")
    ap.add_argument("--out-json", default="config/urdf_derived.json")
    args = ap.parse_args()

    urdf = None
    for f in os.listdir(os.path.join(args.export_dir, "urdf")):
        if f.endswith(".urdf"):
            urdf = os.path.join(args.export_dir, "urdf", f)
    if not urdf:
        sys.exit("no .urdf found")
    mesh_dir = os.path.join(args.export_dir, "meshes")

    root = ET.parse(urdf).getroot()

    J = {}
    for j in root.findall("joint"):
        o, a = j.find("origin"), j.find("axis")
        lim = j.find("limit")
        J[j.find("child").get("link")] = dict(
            name=j.get("name"), type=j.get("type"),
            parent=j.find("parent").get("link"),
            xyz=np.array([float(x) for x in
                          (o.get("xyz") if o is not None else "0 0 0").split()]),
            rpy=np.array([float(x) for x in
                          (o.get("rpy") if o is not None else "0 0 0").split()]),
            axis=(np.array([float(x) for x in a.get("xyz").split()])
                  if a is not None else None),
            lower=float(lim.get("lower")) if lim is not None else None,
            upper=float(lim.get("upper")) if lim is not None else None)

    links = {}
    for l in root.findall("link"):
        m = l.find("visual/geometry/mesh")
        o = l.find("visual/origin")
        links[l.get("name")] = dict(
            mesh=m.get("filename").split("/")[-1] if m is not None else None,
            vxyz=np.array([float(x) for x in
                           (o.get("xyz") if o is not None else "0 0 0").split()]),
            vrpy=np.array([float(x) for x in
                           (o.get("rpy") if o is not None else "0 0 0").split()]))

    def T_of(j):
        T = np.eye(4)
        T[:3, :3] = rpy_to_R(*j["rpy"])
        T[:3, 3] = j["xyz"]
        return T

    def fk(link):
        chain, n = [], link
        while n in J:
            chain.append(n)
            n = J[n]["parent"]
        T = np.eye(4)
        for n in reversed(chain):
            T = T @ T_of(J[n])
        return T

    MOVING = {"revolute", "continuous", "prismatic"}

    def group_of(link):
        n = link
        while n in J:
            if J[n]["type"] in MOVING:
                return n
            n = J[n]["parent"]
        return "root"

    # the actuated joints, ordered root -> head
    actuated = [c for c, j in J.items() if j["type"] in MOVING]
    # keep only those on the chain to the head (ignore the closed-loop linkage)
    head_chain, n = [], "head"
    while n in J:
        if J[n]["type"] in MOVING:
            head_chain.append(n)
        n = J[n]["parent"]
    head_chain.reverse()

    # Front is taken from the shell part literally named "front", not from a
    # display's local Z - Fusion orients display modules arbitrarily, and using
    # the LCD normal put the face 90 degrees off.
    def world_mesh_centre(link_name):
        L2 = links.get(link_name)
        if not L2 or not L2["mesh"]:
            return None
        f = os.path.join(mesh_dir, L2["mesh"])
        t = load_stl(f) if os.path.isfile(f) else None
        if t is None:
            return None
        V = np.eye(4)
        V[:3, :3] = rpy_to_R(*L2["vrpy"])
        V[:3, 3] = L2["vxyz"]
        M = fk(link_name) @ V
        P = t.reshape(-1, 3)
        P = (M[:3, :3] @ P.T).T + M[:3, 3]
        return (P.min(axis=0) + P.max(axis=0)) / 2.0

    axis_pt = fk(head_chain[0])[:3, 3]           # the yaw axis location
    front = None
    fc = world_mesh_centre("body__front_")
    if fc is not None:
        v = fc - axis_pt
        v[2] = 0.0
        if np.linalg.norm(v) > 1e-6:
            front = v / np.linalg.norm(v)
    if front is None:
        front = np.array([1.0, 0.0, 0.0])

    bc = world_mesh_centre("body__back_")
    if bc is not None:
        vb = bc - axis_pt
        vb[2] = 0.0
        if np.linalg.norm(vb) > 1e-6:
            agree = float(np.dot(vb / np.linalg.norm(vb), front))
            print(f"\n  front from body__front_ : azimuth "
                  f"{np.degrees(np.arctan2(front[1], front[0])):.1f} deg")
            print(f"  body__back_ agreement   : {agree:+.3f} "
                  f"(expect about -1.0)")


    # ---- group each link under its nearest actuated ancestor --------------


    # Name each DOF by what its axis actually does, never by position in the
    # chain. Assuming chain order gave pitch and roll swapped: revolute_2's axis
    # lies along the FORWARD vector (roll), revolute_4's along the lateral (pitch).
    up = np.array([0.0, 0.0, 1.0])
    lateral = np.cross(front, up)
    lateral /= np.linalg.norm(lateral)
    REF = {"yaw": up, "roll": front, "pitch": lateral}

    NAMES, used = {}, set()
    print("actuated chain root -> head:")
    for c in head_chain:
        aw = fk(c)[:3, :3] @ J[c]["axis"]
        aw = aw / np.linalg.norm(aw)
        scores = {k: abs(float(np.dot(aw, v))) for k, v in REF.items() if k not in used}
        best = max(scores, key=scores.get)
        used.add(best)
        NAMES[c] = best
        print(f"  {J[c]['name']:12} axis_world=[{aw[0]:+.3f} {aw[1]:+.3f} {aw[2]:+.3f}]"
              f"  -> '{best}' (|cos|={scores[best]:.3f})  child {c}")

    CHAIN = [NAMES[c] for c in head_chain]
    print(f"  nesting order: base -> {' -> '.join(CHAIN)}")

    groups = {"root": "base", **NAMES}
    GROUP_ROOT = {"base": "root"}
    for c in head_chain:
        GROUP_ROOT[NAMES[c]] = c

    # ---- bake meshes -----------------------------------------------------

    buckets = {}
    skipped = 0
    for name, L in links.items():
        if not L["mesh"]:
            continue
        if any(s in name.lower() for s in SKIP):
            skipped += 1
            continue
        g = group_of(name)
        gname = groups.get(g)
        if gname is None:
            # Parts of the closed kinematic loop (pushrods, ball joints, servo
            # horns). URDF cannot solve a closed chain, so they are frozen into
            # the yaw body at the zero pose and do not animate with the linkage.
            gname = CHAIN[0]
        path = os.path.join(mesh_dir, L["mesh"])
        if not os.path.isfile(path):
            continue
        tris = load_stl(path)
        if tris is None:
            continue

        # link frame relative to the frame of the group it is ACTUALLY placed in
        # (not relative to g, which for loop-closure parts is a different frame)
        groot = GROUP_ROOT[gname]
        base = fk(groot) if groot != "root" else np.eye(4)
        T = np.linalg.inv(base) @ fk(name)
        V = np.eye(4)
        V[:3, :3] = rpy_to_R(*L["vrpy"])
        V[:3, 3] = L["vxyz"]
        M = T @ V

        pts = tris.reshape(-1, 3)
        pts = (M[:3, :3] @ pts.T).T + M[:3, 3]
        kind = "internal" if any(k in name.lower() for k in INTERNAL) else "shell"
        buckets.setdefault((gname, kind), []).append(pts.reshape(-1, 3, 3))

    os.makedirs(args.out_meshes, exist_ok=True)
    manifest = {}
    print(f"\nbaked meshes  (skipped {skipped} fasteners):")
    for (gname, kind), chunks in sorted(buckets.items()):
        tris = np.concatenate(chunks, axis=0)
        fn = f"{gname}_{kind}.stl"
        n = write_stl(os.path.join(args.out_meshes, fn), tris)
        kb = os.path.getsize(os.path.join(args.out_meshes, fn)) / 1024
        manifest.setdefault(gname, {})[kind] = fn
        print(f"  {fn:22} {n:>7,} tris  {kb:7.0f} KB")

    # ---- joint geometry in each parent group's frame ---------------------

    out = {"source": os.path.basename(urdf), "groups": manifest, "joints": {},
           "chain": ["base"] + CHAIN}
    parent_group = {CHAIN[0]: "root"}
    for i in range(1, len(head_chain)):
        parent_group[CHAIN[i]] = head_chain[i - 1]
    for child, gname in NAMES.items():
        j = J[child]
        pg = parent_group[gname]
        base = fk(pg) if pg != "root" else np.eye(4)
        T = np.linalg.inv(base) @ fk(child)
        axis_world = fk(child)[:3, :3] @ j["axis"]
        out["joints"][gname] = {
            "urdf_joint": j["name"],
            "origin_xyz": [round(float(x), 6) for x in T[:3, 3]],
            # full offset incl. rotation, column-major for THREE.Matrix4.fromArray
            "origin_matrix": [round(float(x), 8) for x in T.T.flatten()],
            "axis_local": [round(float(x), 6) for x in j["axis"]],
            "axis_world_zero": [round(float(x), 4) for x in
                                axis_world / np.linalg.norm(axis_world)],
            "lower_deg": round(float(np.degrees(j["lower"])), 2) if j["lower"] else None,
            "upper_deg": round(float(np.degrees(j["upper"])), 2) if j["upper"] else None,
        }

    # servo joints (the actual MG995s driving the parallel linkage)
    out["servos"] = {}
    for child, j in J.items():
        if j["type"] == "revolute" and j["lower"] is not None:
            out["servos"][j["name"]] = {
                "lower_deg": round(float(np.degrees(j["lower"])), 2),
                "upper_deg": round(float(np.degrees(j["upper"])), 2),
            }

    # display placement, in the roll group's frame
    base = fk(head_chain[-1])
    out["displays"] = []
    for name in links:
        if "lcd" in name.lower():
            T = np.linalg.inv(base) @ fk(name)
            out["displays"].append({
                "link": name,
                "xyz": [round(float(x), 5) for x in T[:3, 3]],
                "matrix": [round(float(x), 8) for x in T.T.flatten()],
                "normal": [round(float(x), 4) for x in T[:3, :3] @ np.array([0, 0, 1])],
            })

    # bbox in WORLD frame, by walking the group chain (not group-local points)
    Tw, allpts = np.eye(4), []
    for gname in ["base"] + CHAIN:
        if gname != "base":
            Tw = Tw @ np.array(out["joints"][gname]["origin_matrix"]).reshape(4, 4).T
        for kind in buckets:
            if kind[0] != gname:
                continue
            P = np.concatenate(buckets[kind]).reshape(-1, 3)
            allpts.append((Tw[:3, :3] @ P.T).T + Tw[:3, 3])
    allpts = np.concatenate(allpts)

    out["bbox"] = {"min": [round(float(x), 4) for x in allpts.min(axis=0)],
                   "max": [round(float(x), 4) for x in allpts.max(axis=0)],
                   "size_mm": [round(float(np.ptp(allpts[:, i]) * 1000), 1)
                               for i in range(3)]}

    # ---- frame conventions, so the viewer and the SSL sim agree -----------
    # URDF is Z-up. The yaw axis points along -Z, so a POSITIVE base_yaw command
    # (counter-clockwise seen from above, our bearing convention) must be sent to
    # the joint negated. yaw_sign captures that once, here, instead of in three
    # different places later.
    yaw_axis_w = np.array(out["joints"][CHAIN[0]]["axis_world_zero"])
    yaw_sign = -1.0 if yaw_axis_w[2] < 0 else 1.0

    out["frame"] = {
        "up_world": [0, 0, 1],
        "front_world": [round(float(x), 4) for x in front],
        "front_yaw_deg": round(float(np.degrees(np.arctan2(front[1], front[0]))), 2),
        "yaw_sign": yaw_sign,
        "note": "URDF Z-up. bearing 0 = front_world, positive = CCW seen from above.",
    }

    # ---- second display -------------------------------------------------
    # The CAD contains ONE GC9A01. The robot has two, so mirror the modelled one
    # across the head's sagittal plane (the plane through the head centre
    # spanned by `front` and `up`). Positions mirror; orientation does not -
    # both panels face the same way on a real head.
    if len(out["displays"]) == 1:
        R_roll = fk(head_chain[-1])[:3, :3]
        lateral_w = np.cross(front, np.array([0.0, 0.0, 1.0]))
        lateral_w /= np.linalg.norm(lateral_w)
        L = R_roll.T @ lateral_w                      # lateral axis, head frame
        h = (np.linalg.inv(fk(head_chain[-1])) @ fk("head"))[:3, 3]
        d = np.array(out["displays"][0]["xyz"])
        mirrored = d - 2.0 * float(np.dot(d - h, L)) * L
        sep = float(np.linalg.norm(mirrored - d))
        out["displays"].append({
            "link": out["displays"][0]["link"] + "__mirrored",
            "xyz": [round(float(x), 5) for x in mirrored],
            "matrix": out["displays"][0]["matrix"],
            "normal": out["displays"][0]["normal"],
            "derived": "mirrored across the sagittal plane; not present in CAD",
        })
        out["eye_separation_m"] = round(sep, 5)
        print(f"\n  second display mirrored; eye separation {sep*1000:.1f} mm")

    # ---- display orientation -------------------------------------------
    # A display's own local Z does NOT point out of the face. Build a frame
    # whose +Z is the outward dome normal at that point, so the twin can put a
    # textured disc on it and have it face the viewer.
    head_c_w = world_mesh_centre("head")
    if head_c_w is not None and out["displays"]:
        G = fk(head_chain[-1])
        Ginv = np.linalg.inv(G)
        head_c = (Ginv @ np.append(head_c_w, 1.0))[:3]
        up_g = Ginv[:3, :3] @ np.array([0.0, 0.0, 1.0])

        # Head shell points in the pitch-group frame, for seating the eye discs
        # on the dome surface. The LCD link origin sits at the back of the
        # module, so a disc placed there renders inside the shell.
        head_pts = None
        hl = links.get("head")
        if hl and hl["mesh"]:
            hp = os.path.join(mesh_dir, hl["mesh"])
            ht = load_stl(hp) if os.path.isfile(hp) else None
            if ht is not None:
                V = np.eye(4)
                V[:3, :3] = rpy_to_R(*hl["vrpy"])
                V[:3, 3] = hl["vxyz"]
                M = Ginv @ fk("head") @ V
                head_pts = (M[:3, :3] @ ht.reshape(-1, 3).T).T + M[:3, 3]

        # Best source: the eye holes cut in the head shell. The LCD module
        # origin is off the hole axis, so seating along head_c -> LCD put the
        # discs beside the holes instead of in them.
        holes = find_eye_holes(head_pts.reshape(-1, 3, 3)) if head_pts is not None else []
        if len(holes) >= len(out["displays"]):
            for disp in out["displays"]:
                pos = np.array(disp["xyz"])
                k = min(range(len(holes)),
                        key=lambda i: np.linalg.norm(holes[i][0] - pos))
                c, z, r = holes.pop(k)
                x = np.cross(up_g, z)
                if np.linalg.norm(x) < 1e-6:
                    x = np.cross(np.array([1.0, 0.0, 0.0]), z)
                x /= np.linalg.norm(x)
                y = np.cross(z, x)
                seat = c - z * 0.0005        # just inside the outer rim
                M = np.eye(4)
                M[:3, 0], M[:3, 1], M[:3, 2], M[:3, 3] = x, y, z, seat
                disp["face_matrix"] = [round(float(v), 8) for v in M.T.flatten()]
                disp["outward"] = [round(float(v), 4) for v in z]
                disp["seat_xyz"] = [round(float(v), 5) for v in seat]
                disp["seated_mm"] = round(float(np.linalg.norm(seat - pos)) * 1000, 1)
                disp["seat_source"] = "shell eye hole"
                out["eye_radius_m"] = round(r, 5)
            print(f"  eyes seated in shell holes, r = {out['eye_radius_m']*1000:.1f} mm")

        for disp in out["displays"]:
            if "seat_source" in disp:
                continue
            pos = np.array(disp["xyz"])
            z = pos - head_c
            if np.linalg.norm(z) < 1e-9:
                continue
            z /= np.linalg.norm(z)
            x = np.cross(up_g, z)
            if np.linalg.norm(x) < 1e-6:
                x = np.cross(np.array([1.0, 0.0, 0.0]), z)
            x /= np.linalg.norm(x)
            y = np.cross(z, x)
            seat = pos
            if head_pts is not None:
                rel = head_pts - head_c
                rad = np.linalg.norm(rel, axis=1)
                ok = rad > 1e-6
                cosang = np.zeros(len(rel))
                cosang[ok] = (rel[ok] @ z) / rad[ok]
                cone = cosang > 0.97                  # ~14 deg around the eye axis
                if cone.sum() >= 20:
                    # outermost shell point along the eye axis, + 1 mm clearance
                    surf = float((rel[cone] @ z).max())
                    seat = head_c + z * (surf + 0.001)
                    disp["seated_mm"] = round(
                        float(np.linalg.norm(seat - pos)) * 1000, 1)

            M = np.eye(4)
            M[:3, 0], M[:3, 1], M[:3, 2], M[:3, 3] = x, y, z, seat
            disp["face_matrix"] = [round(float(v), 8) for v in M.T.flatten()]
            disp["outward"] = [round(float(v), 4) for v in z]
            disp["seat_xyz"] = [round(float(v), 5) for v in seat]

    out.setdefault("eye_radius_m", 0.0162)   # GC9A01 1.28 in active area

    with open(args.out_json, "w") as fh:
        json.dump(out, fh, indent=2)
    print(f"\nwrote {args.out_json}")
    for g, v in out["joints"].items():
        print(f"  {g:6} {v['urdf_joint']:12} axis_world={v['axis_world_zero']} "
              f"origin={v['origin_xyz']}")
    print(f"  servos: {out['servos']}")
    print(f"  displays: {len(out['displays'])}")
    print(f"  bbox: {out['bbox']}")


if __name__ == "__main__":
    main()
