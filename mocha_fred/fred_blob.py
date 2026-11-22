"""Mocha tracking and shape data -> Fred node-clipboard JSON. Pure Python,
so it is testable anywhere (python mocha_fred/fred_blob.py --selftest).

Fred's node clipboard is the SYSTEM clipboard holding compact JSON text
{"nodes": [...]} -- the same node dicts a .fred graph stores (Fred
MainWindow::nodePaste -> NodeScene::pasteSelection). A Mocha exporter with one
data stream gets Mocha's "Copy to Clipboard" button, so returning this text
from do_export gives Mocha -> Fred copy/paste, as Mocha -> Nest works today.

Conventions (measured on Mocha Pro 2026.5):
  Mocha layer.get_surface_position(i, t, view): plate PIXELS, origin bottom-left,
  y UP; index 0 = BL, 1 = BR, 2 = TL, 3 = TR (Nest CornerPin to1, to2, to4, to3).
  Frame numbers: Mocha time t is 0-based; the plate frame is t + first_frame_offset.
  Fred CornerPin: corners 1..4 = TL, TR, BR, BL as UV 0..1 of the plate, y DOWN;
  animated channels are named "to1.x" ... (field cpTo1x), linear keys.
  Fred Tracker (com.skf.tracker, built in since Fred 0.2.9): the tracks are ONE JSON string
  in pluginParams.vars["tracks"]; keys [frame, x, y, match, by hand], pixels of the input
  format, top-left origin (y DOWN); per track T / R / S say what the motion uses.
  Since Fred 0.2.10 the Tracker is a TOOL: it passes its input through, wires only to a
  Viewer and has no Stabilise / Match-move of its own -- the tracks leave through Create
  Transform and Create CornerPin (MainWindow_tracker.cpp trackerExportCornerPin).
  Fred Transform (Create Transform, MainWindow_tracker.cpp trackerExportTransform):
  P' = pivot + t + R S (P - pivot), pivot = picture centre + (centerX, centerY), rotate in
  degrees clockwise on screen, Invert = stabilise, Mitchell filter, no motion blur.
  Fred Roto (0.2.21; Mask is deprecated and loads as a Roto): maskShapes, each point
  [pos, in, out (absolute), feather offset, inside feather offset, falloff weight, outer-rail
  tangent deltas, core-rail tangent deltas], top-left origin; a keyed shape is a full point
  snapshot per shape key. Roto 6+ reads Mocha's Transform & Shape export, with
  the movement on the layer folder. Older Rotos read Shake Rotoshape.
  The keys are linear between frames (interp 0), as Mocha's own Nest Roto paste keys them; a
  layer's in / out points become Hold keys on the shape's opacity channel "rotoOp.<sid>"
  (roto_life_curve), the keys Fred PR #233's Nest import writes for a shape lifetime.
"""
import json
import math
import os
import re
import zlib

# NodeType ids from NodeGraph_NodeTypes.h (NodeGraph.h in older tags). The loader
# prefers pluginType, then typeKey, then the numeric id.
TRANSFORM = 5
PLANARTRACK = 65
CORNERPIN = 103
ROTO = 115        # Roto (0.2.21: upstream; Mask is deprecated and converts to Roto on load)
PLUGIN = 139
TYPE_KEYS = {TRANSFORM: "Transform", PLANARTRACK: "PlanarTrack", CORNERPIN: "CornerPin", ROTO: "Roto"}
LATEST_FRED_VERSION = (0, 3, 22)

# First tagged release supporting each revision, in descending order. Fred
# started stamping opVersion in 0.2.23. See NodeGraph_NodeVersion.h for the
# built-ins and NodeRegistry.cpp for the plugin default (Tracker is still 1).
OP_VERSIONS = {
    ROTO: (((0, 3, 21), 9), ((0, 3, 2), 8), ((0, 2, 34), 5),
           ((0, 2, 25), 4), ((0, 2, 23), 3)),
    TRANSFORM: (((0, 3, 21), 3), ((0, 2, 29), 2), ((0, 2, 23), 1)),
    CORNERPIN: (((0, 3, 21), 4), ((0, 2, 34), 3), ((0, 2, 29), 2), ((0, 2, 23), 1)),
    PLANARTRACK: (((0, 3, 21), 3), ((0, 3, 2), 2), ((0, 2, 23), 1)),
}


def fred_version(path=None):
    """The version of the Fred the paste is for, as a tuple: FRED_MOCHA_VERSION ("0.2.34")
    names it; else `path`, or the file FRED_MOCHA_VERSION_FILE names, holds it at the start
    of its text (a deployment's VERSION file, "0.2.34-<build> <sha> <date>"). () when none
    is set or readable."""
    txt = os.environ.get("FRED_MOCHA_VERSION", "")
    path = path or os.environ.get("FRED_MOCHA_VERSION_FILE", "")
    if not txt and path:
        try:
            with open(path, encoding="utf-8-sig") as fh:
                txt = fh.read()
        except (OSError, UnicodeError):
            pass
    m = re.match(r"\s*(\d+)\.(\d+)\.(\d+)", txt)
    return tuple(int(x) for x in m.groups()) if m else ()


def _op_version(node_type, version):
    return next((op for since, op in OP_VERSIONS[node_type] if version >= since), None)


def roto_op_version(version=None):
    """The Roto op version for the target release. Unknown targets use the newest."""
    v = fred_version() if version is None else version
    return _op_version(ROTO, v or LATEST_FRED_VERSION)


def plugin_type_id(version=None):
    """The Plugin enum moves as built-ins are added. Older releases load it by id."""
    v = (fred_version() if version is None else version) or LATEST_FRED_VERSION
    ids = (((0, 3, 22), PLUGIN), ((0, 3, 21), 136), ((0, 2, 35), 126),
           ((0, 2, 30), 124), ((0, 2, 29), 123), ((0, 2, 22), 121),
           ((0, 2, 16), 116), ((0, 2, 14), 115), ((0, 2, 13), 113),
           ((0, 2, 11), 112), ((0, 2, 10), 111))
    return next((type_id for since, type_id in ids if v >= since), 110)


MITCHELL = 4      # imgops::Filter::Mitchell -- Create Transform's and Create CornerPin's resampler
# Fred CornerPin corner -> Mocha surface index (TL, TR, BR, BL <- 2, 3, 1, 0)
FRED_FROM_MOCHA = {1: 2, 2: 3, 3: 1, 4: 0}
TRACK_NAMES = {0: "BL", 1: "BR", 2: "TL", 3: "TR"}


def _key(frame, value):
    return {"t": int(frame), "v": float(value), "i": 1, "idx": 0, "idy": 0, "odx": 0, "ody": 0}


def _surface_uv(point, width, height):
    return point[0] / width, 1.0 - point[1] / height


def cornerpin_node(corners, width, height, name="CornerPin_mocha", node_id=1, x=0.0, y=0.0, invert=False):
    """corners: {plate_frame: [[x, y] * 4]} in Mocha order/space. One Fred CornerPin,
    To corners animated, From = the full frame (Fred's default), as Mocha's Nest export."""
    frames = sorted(corners)
    op = _op_version(CORNERPIN, fred_version() or LATEST_FRED_VERSION)
    node = {"id": node_id, "type": CORNERPIN, "name": name, "x": x, "y": y, "inputs": [], "cpInvert": bool(invert),
            "cpFilter": MITCHELL, "mbShutter": 0.0,   # as Fred's own Create CornerPin
            "cpFrom1x": 0.0, "cpFrom1y": 0.0, "cpFrom2x": 1.0, "cpFrom2y": 0.0,
            "cpFrom3x": 1.0, "cpFrom3y": 1.0, "cpFrom4x": 0.0, "cpFrom4y": 1.0, "anim": {}}
    for fc, mi in FRED_FROM_MOCHA.items():
        uv = [_surface_uv(corners[f][mi], width, height) for f in frames]
        kx = [_key(f, u) for f, (u, _) in zip(frames, uv)]
        ky = [_key(f, v) for f, (_, v) in zip(frames, uv)]
        node["anim"]["to%d.x" % fc] = {"keys": kx}
        node["anim"]["to%d.y" % fc] = {"keys": ky}
        node["cpTo%dx" % fc] = kx[0]["v"]
        node["cpTo%dy" % fc] = ky[0]["v"]
    if op is not None:
        node["opVersion"] = op
    if op == 4:
        node["mbSamples"] = 8
    return node


def planartrack_node(corners, width, height, ref_frame, name="PlanarTrack_mocha", node_id=1):
    """Export the Mocha surface as Fred's editable per-frame PlanarTrack corners."""
    op = _op_version(PLANARTRACK, fred_version() or LATEST_FRED_VERSION)
    tracked = {str(frame): [uv for index in FRED_FROM_MOCHA.values()
                            for uv in _surface_uv(corners[frame][index], width, height)]
               for frame in sorted(corners)}
    if ref_frame not in corners:
        ref_frame = min(corners)
    node = {"id": node_id, "type": PLANARTRACK, "name": name, "x": 0.0, "y": 0.0, "inputs": [],
            "planarRefFrame": int(ref_frame), "planarCorners": tracked[str(ref_frame)],
            "planarTracked": tracked, "planarOutMode": 0}
    if op is not None:
        node["opVersion"] = op
    if op and op >= 2:
        node["maskShapes"] = []
    if op and op >= 3:
        node["planarAlpha"] = 1  # Input: this export carries the surface alone
    return node


def _track_id(seed):
    """A stable 8-hex-digit track id (Fred's fredpt::Track::uid shape), so the same export
    pastes the same ids."""
    return "%08x" % (zlib.crc32(seed.encode("utf-8")) & 0xFFFFFFFF)


def tracks_from_corners(corners, height, layer_name="mocha", precision=4):
    """The four surface corners as Fred tracks (TL, TR, BR, BL), each giving T, R and S, so
    Create Transform fits the best similarity through all four -- what a four-point Nest
    Tracker does -- and Create CornerPin takes the four as its corners."""
    frames = sorted(corners)
    tracks = []
    for mi in (2, 3, 1, 0):   # TL, TR, BR, BL
        name = "mocha_%s" % TRACK_NAMES[mi]
        tracks.append({"id": _track_id("%s/%s" % (layer_name, name)), "name": name, "on": True,
                       "pw": 32.0, "ph": 32.0, "sx": 32, "sy": 32, "ox": 0.0, "oy": 0.0,
                       "t": 1, "r": 1, "s": 1,
                       "keys": [[int(f), round(corners[f][mi][0], precision),
                                 round(height - corners[f][mi][1], precision), 1.0, 0]
                                for f in frames]})
    return tracks


def tracker_node(corners, height, ref_frame, name="Tracker_mocha", node_id=2, x=0.0, y=0.0, layer_name="mocha"):
    """A Fred 0.2.10 Tracker carrying the four corners (a tool: no transform of its own;
    bake with Create Transform / Create CornerPin, whose reference is refFrame)."""
    version = fred_version() or LATEST_FRED_VERSION
    tracks = tracks_from_corners(corners, height, layer_name, precision=3)
    node = {"id": node_id, "type": plugin_type_id(version), "name": name, "x": x, "y": y, "inputs": [],
            "pluginType": "com.skf.tracker",
            "pluginParams": {"nums": {"refFrame": int(ref_frame)},
                             "vars": {"tracks": json.dumps({"active": 0, "tracks": tracks}, separators=(",", ":"))}}}
    if version >= (0, 2, 23):
        node["opVersion"] = 1
    if version >= (0, 2, 20):
        node["typeKey"] = "Plugin"
    return node


def motion_at(tracks, ref, frame):
    """Python port of Fred fredtk::motionFrom (tracker_model.cpp; unchanged 0.2.9 -> 0.2.10) for tracks keyed on
    every frame (Mocha's export is): T = centroid shift of the T tracks; R = atan2 of the
    summed cross / dot about each centroid; S = sqrt of the summed squared spreads. Returns
    (tx, ty, rot_deg, scale, cx, cy) or None."""
    pairs = []
    for tr in tracks:
        if not tr.get("on", True):
            continue
        k = {key[0]: (key[1], key[2]) for key in tr["keys"]}
        if ref in k and frame in k:
            pairs.append((k[ref], k[frame], tr))
    T = [(a, b) for a, b, tr in pairs if tr.get("t")]
    R = [(a, b) for a, b, tr in pairs if tr.get("r")]
    S = [(a, b) for a, b, tr in pairs if tr.get("s")]
    rot_on, scl_on = len(R) >= 2, len(S) >= 2
    if not T and not rot_on and not scl_on:
        return None

    def cents(v):
        n = float(len(v))
        return (sum(a[0] for a, _ in v) / n, sum(a[1] for a, _ in v) / n), (sum(b[0] for _, b in v) / n, sum(b[1] for _, b in v) / n)

    ca, cb = cents(T if T else (R if rot_on else S))
    tx, ty = (cb[0] - ca[0], cb[1] - ca[1]) if T else (0.0, 0.0)
    rot, scale = 0.0, 1.0
    if rot_on:
        ra, rb = cents(R)
        dot = cross = 0.0
        for a, b in R:
            ux, uy, wx, wy = a[0] - ra[0], a[1] - ra[1], b[0] - rb[0], b[1] - rb[1]
            dot += ux * wx + uy * wy
            cross += ux * wy - uy * wx
        if abs(dot) + abs(cross) > 1e-12:
            rot = math.degrees(math.atan2(cross, dot))
    if scl_on:
        sa, sb = cents(S)
        na = sum((a[0] - sa[0]) ** 2 + (a[1] - sa[1]) ** 2 for a, _ in S)
        nb = sum((b[0] - sb[0]) ** 2 + (b[1] - sb[1]) ** 2 for _, b in S)
        if na > 1e-9 and nb > 1e-12:
            scale = math.sqrt(nb / na)
    return tx, ty, rot, scale, ca[0], ca[1]


def transform_node(corners, width, height, ref_frame, name="MatchMove_mocha", node_id=3, x=0.0, y=0.0,
                   stabilise=False, layer_name="mocha"):
    """What Fred's Tracker > Create Transform makes from the same four tracks: a keyed
    Transform (translate, rotate, uniform scale about the corners' centroid on the
    reference frame). stabilise = Invert on (Fred's stabilise)."""
    tracks = tracks_from_corners(corners, height, layer_name)
    frames = sorted(corners)
    if ref_frame not in corners:   # as Fred: a reference outside the track -> the first tracked frame
        ref_frame = frames[0]
    curves = {"tx": [], "ty": [], "rotate": [], "scaleF": []}
    first = None
    for f in frames:
        m = motion_at(tracks, ref_frame, f)
        if m is None:
            continue
        first = first or m
        curves["tx"].append(_key(f, m[0]))
        curves["ty"].append(_key(f, m[1]))
        curves["rotate"].append(_key(f, m[2]))
        curves["scaleF"].append(_key(f, m[3]))
    if first is None:
        raise ValueError("no motion against reference frame %d" % ref_frame)
    op = _op_version(TRANSFORM, fred_version() or LATEST_FRED_VERSION)
    node = {"id": node_id, "type": TRANSFORM, "name": name, "x": x, "y": y, "inputs": [],
            "tx": first[0], "ty": first[1], "rotate": first[2], "scaleF": first[3], "scaleY": first[3],
            "centerX": first[4] - 0.5 * width, "centerY": first[5] - 0.5 * height,
            "xformFilter": MITCHELL, "mbShutter": 0.0, "xformNoClip": False,
            "anim": {"tx": {"keys": curves["tx"]}, "ty": {"keys": curves["ty"]}, "rotate": {"keys": curves["rotate"]},
                     "scaleF": {"keys": curves["scaleF"]}, "scaleY": {"keys": [dict(k) for k in curves["scaleF"]]}}}
    if stabilise:
        node["xformInvert"] = True
    if op is not None:
        node["opVersion"] = op
    if op == 3:
        node["mbSamples"] = 8
        del node["xformNoClip"]  # saved only when enabled
        for axis in ("centerX", "centerY"):
            if not node[axis]:
                del node[axis]  # the centred pivot is Fred's default
    return node, ref_frame


def corners_from_nest_cornerpin(txt):
    """Mocha's own "Nest Corner Pin" export text -> {plate_frame: [[x, y] * 4]} in Mocha
    order (BL, BR, TL, TR = Nest to1, to2, to4, to3). Used when the export dialog's
    Invert / Stabilize / Remove lens distortion boxes are ticked: Mocha bakes those into
    the corner values (Invert is NOT a corner-pin invert flag), so we take its numbers
    rather than re-deriving them. Values are as Mocha writes them (6 significant digits)."""
    per = {}
    for nk in (1, 2, 3, 4):
        m = re.search(r"\bto%d\s+\{\{curve\s+([^}]*)\}\s+\{curve\s+([^}]*)\}" % nk, txt)
        if not m:
            raise ValueError("no animated to%d in Mocha's CornerPin export" % nk)
        xy = []
        for s in m.groups():
            d, cur = {}, None
            for tok in s.split():
                if tok.startswith("x"):
                    cur = int(float(tok[1:]))
                elif cur is not None:
                    d[cur] = float(tok)
                    cur += 1
            xy.append(d)
        per[nk] = xy
    frames = sorted(set(per[1][0]) & set(per[1][1]))
    nest_of_mocha = (1, 2, 4, 3)
    return {f: [[per[nk][0][f], per[nk][1][f]] for nk in nest_of_mocha] for f in frames}


def shapes_from_mocha_ssf(txt):
    """Mocha's own "Shake Rotoshape" export (shake_shape_data 4.0) -> a list of
    {"name", "closed", "keys": {plate_frame: [[12 floats] * vertices]}}.

    Measured on Mocha Pro 2026.5: Mocha converts its
    X-splines to beziers here, bakes the layer's tracking into every key, and writes one
    shape per contour. Each vertex is 12 numbers, all ABSOLUTE plate pixels, bottom-left
    origin, y up: the spline point, its incoming and outgoing handles, then the same three
    for the edge (feather) curve; no feather = the edge repeats the spline. key_time is
    the plate frame (Mocha applies the project's frame offset). "visible" is the shape's own
    visible line (1 when absent)."""
    shapes, cur, frame = [], None, None
    for line in txt.splitlines():
        tok = line.split()
        if not tok:
            continue
        kw = tok[0]
        if kw == "shape_name":
            cur = {"name": " ".join(tok[1:]), "closed": True, "visible": True, "n": 0, "keys": {}}
            shapes.append(cur)
        elif cur is None:
            continue
        elif kw == "closed":
            cur["closed"] = tok[1] != "0"
        elif kw == "visible":
            cur["visible"] = tok[1] != "0"
        elif kw == "num_vertices":
            cur["n"] = int(tok[1])
        elif kw == "key_time":
            frame = int(round(float(tok[1])))
        elif kw == "vertex_data":
            v = [float(t) for t in tok[1:]]
            if len(v) != 12 * cur["n"]:
                raise ValueError("%s: key %s has %d numbers for %d vertices (expected 12 each)"
                                 % (cur["name"], frame, len(v), cur["n"]))
            cur["keys"][frame] = [v[i:i + 12] for i in range(0, len(v), 12)]
    for s in shapes:
        del s["n"]
    return shapes


def _roto_point(v, height):
    """One SSF vertex (12 floats, y up) -> Fred's serialised RotoPoint (y down): pos, in,
    out (absolute), outer feather as an offset from pos, no inside feather, linear
    falloff, and the outer rail's tangent deltas (the edge curve's own handles minus the
    spline's), so the feather rail is Mocha's edge curve exactly."""
    px, py, ix, iy, ox, oy, ex, ey, eix, eiy, eox, eoy = v
    H = height
    fx, fy = ex - px, py - ey
    tin = ((eix - ex) - (ix - px), -((eiy - ey) - (iy - py)))
    tout = ((eox - ex) - (ox - px), -((eoy - ey) - (oy - py)))
    return [px, H - py, ix, H - iy, ox, H - oy, fx, fy, 0.0, 0.0, 0.5,
            tin[0], tin[1], tout[0], tout[1], 0.0, 0.0, 0.0, 0.0]


def life_from_points(in_point, out_point, frame_offset, length=None):
    """A Mocha layer's in / out points (0-based Mocha times) as roto_node's "life": plate frames
    (first, last), an end left open (None) where it is the project's own first or last frame,
    so a layer over the whole project steps nothing. length = the project's frame count (None:
    unknown, the out point is kept)."""
    first = in_point + frame_offset if in_point > 0 else None
    last = out_point + frame_offset if length is None or out_point < length - 1 else None
    return first, last


def roto_life_curve(first, last, opacity=1.0):
    """A shape drawn on frames first..last only, on Fred's per-shape opacity channel
    "rotoOp.<sid>": Hold keys ("i" 0) stepping between `opacity` and 0 at whole frames (the
    keys Fred PR #233's Nest import writes for a shape lifetime; a Fred Roto has no lifetime
    field, and opacity 0 adds nothing to the matte). None marks an open end (drawn on every
    frame before / after); a reversed pair is read smaller first. Returns None when both ends
    are open (no curve needed). With motion blur on, the steps fall at whole frames, so the in
    frame and the frame after the out point take part of the shutter."""
    if first is not None and last is not None and first > last:
        first, last = last, first
    pts = []
    if first is not None:
        pts += [(first - 1, 0.0), (first, opacity)]
    if last is not None:
        pts += [] if pts else [(last, opacity)]
        pts.append((last + 1, 0.0))
    if not pts:
        return None
    return {"keys": [dict(_key(f, v), i=0) for f, v in pts]}


def roto_node(layers, height, name="Roto_mocha", node_id=1, x=0.0, y=0.0, invert=False, op_version=None):
    """layers: [(layer name, ssf shapes, subtract[, options])] in Mocha's stacking order. One
    Fred Roto: a shape per Mocha contour, keyed on every exported frame (static when every key
    is the same), linear between keys as Mocha's own Nest Roto paste keys them.
    Multiple contours from one layer share a folder when they are ordinary shapes.
    subtract (Mocha blend mode Subtract) cuts the layer's shapes from the union.
    options (a dict, all optional): "life" = (first,
    last) plate frames the layer is drawn on (Mocha's in / out points; None = an open end):
    keys outside are dropped and the shapes step to opacity 0 outside (roto_life_curve);
    "visible" = False ticks the layer's shapes off (they stay in the node, out of the
    matte). A layer left with no shape (no exported frame inside its life) is an error.
    invert = the whole matte inverted (node level). RGB passes untouched and alpha = the
    matte, as Mocha's Nest Roto paste. op_version: the Roto's "opVersion" (None: what a new
    Roto gets in the Fred FRED_MOCHA_VERSION / FRED_MOCHA_VERSION_FILE names, else the
    newest, roto_op_version; 0 writes none = legacy)."""
    ver = roto_op_version() if op_version is None else op_version
    modern_stack = (ver or 0) >= 6
    root = 1 if (ver or 0) >= 9 else -1
    shapes, holes, groups, anim, sid = [], [], [], {}, 0
    if root >= 0:
        groups.append({"id": root, "name": "Root", "root": True, "visible": True, "locked": False, "opacity": 1.0})
    for layer in layers:
        lname, ssf, subtract = layer[:3]
        opts = layer[3] if len(layer) > 3 else {}
        first, last = opts.get("life") or (None, None)
        if first is not None and last is not None and first > last:
            first, last = last, first
        life = roto_life_curve(first, last)
        gid, kept = root, 0
        for i, s in enumerate(ssf):
            frames = [f for f in sorted(s["keys"])
                      if (first is None or f >= first) and (last is None or f <= last)]
            if not frames:
                continue
            stencil = modern_stack and subtract and s["closed"]
            if len(ssf) > 1 and gid == root and not stencil:
                gid = len(groups) + 1
                group = {"id": gid, "name": lname, "visible": True, "locked": False, "opacity": 1.0}
                if root >= 0:
                    group["parent"] = root
                groups.append(group)
            sid += 1
            kept += 1
            keys = [{"frame": f, "pts": [_roto_point(v, height) for v in s["keys"][f]]} for f in frames]
            sh = {"name": lname if len(ssf) == 1 else "%s_%d" % (lname, i + 1), "sid": sid,
                  "group": root if stencil else gid,
                  "closed": s["closed"], "visible": bool(s.get("visible", True) and opts.get("visible", True)),
                  "invert": bool(subtract and not modern_stack), "opacity": 1.0,
                  "mbOn": True, "mbAmount": 1.0, "pts": keys[0]["pts"]}
            if modern_stack:
                sh["paintRGBA"] = [1.0, 1.0, 1.0, 1.0]
                if stencil:
                    sh["blend"] = "stencil"
                elif subtract:
                    sh["visible"] = False  # legacy inverted open shapes drew nothing
            if any(k["pts"] != keys[0]["pts"] for k in keys[1:]):
                sh["keys"] = keys   # interp absent = 0, linear
            (holes if stencil else shapes).append(sh)
            if life:
                anim["rotoOp.%d" % sid] = json.loads(json.dumps(life))   # a curve of its own per shape
        if ssf and not kept:
            raise ValueError("%s: no exported frame inside its in / out points" % lname)
    shapes.extend(holes)  # match Fred's 5 -> 6 upgrade: holes cut the whole stack
    return _roto_blob(shapes, groups, anim, name, node_id, x, y, invert, ver)


def _roto_blob(shapes, groups, anim, name, node_id, x, y, invert, ver):
    if not shapes:
        raise ValueError("no shapes in the selected layers")
    node = {"id": node_id, "type": ROTO, "name": name, "x": x, "y": y, "inputs": [],
            "maskShapes": shapes, "maskFeather": 0.0, "maskInvert": bool(invert),
            "maskOutputMatte": False, "maskPremult": False, "maskReplaceAlpha": True, "maskOpacity": 1.0}
    if groups:
        node["maskGroups"] = groups
    if anim:
        node["anim"] = anim
    if (ver or 0) >= 6:
        node.update(maskMaintainInputBbox=False, maskCropToFormat=False, maskPaintMode=0)
    if ver:
        node["opVersion"] = int(ver)
    return node


def roto_folders_node(layers, name, invert, op_version):
    """One folder per selected Mocha layer, with its source-local contours."""
    from fred_roto_curves import moves

    groups, shapes, anim = [], [], {}
    root = -1
    if op_version >= 9:
        root = 1
        groups.append({"id": root, "name": "Root", "root": True, "visible": True, "locked": False, "opacity": 1.0})

    def folder(name, parent, transform, visible=True, opacity=1.0):
        gid = len(groups) + 1
        xf, curves = transform
        group = {"id": gid, "name": name, "visible": visible, "locked": False, "opacity": opacity, "xf": xf}
        if parent >= 0:
            group["parent"] = parent
        groups.append(group)
        for channel, rows in curves.items():
            keys = []
            for j, (frame, value, left, right, hold) in enumerate(rows):
                idx = -(frame - rows[j - 1][0] if j else 1.0) / 3.0
                odx = (rows[j + 1][0] - frame if j + 1 < len(rows) else 1.0) / 3.0
                key = {"t": frame, "v": value, "i": 0 if hold else 2,
                       "idx": idx, "idy": idx * left, "odx": odx, "ody": odx * right}
                if abs(left - right) > 1e-6 * max(1.0, abs(left), abs(right)):
                    key["brk"] = True
                keys.append(key)
            anim["rotoLayer.%d.%s" % (gid, channel)] = {"keys": keys}
        return gid

    def add_layer(source, label, parent, subtract, first, last, visible=True):
        gid = folder(label, parent, source["xf"], visible and source["visible"], source["opacity"])
        for kind, data in source["entries"]:
            if kind == "layer":
                add_layer(data, data["name"], gid, subtract, first, last)
                continue
            sid = len(shapes) + 1
            shape = {k: data[k] for k in ("name", "closed", "visible", "opacity", "pts")}
            shape.update(sid=sid, group=gid, invert=False, paintRGBA=[1.0] * 4, mbOn=True, mbAmount=1.0)
            if moves(data["xf"]):
                shape["group"] = folder(data["name"] + " transform", gid, data["xf"])
            if subtract:
                if shape["closed"]:
                    shape["blend"] = "stencil"
                else:
                    shape["visible"] = False
            if data["keys"]:
                shape["keys"] = data["keys"]
                if data["interp"]:
                    shape["interp"] = data["interp"]
            life = roto_life_curve(first, last, shape["opacity"])
            if life:
                anim["rotoOp.%d" % sid] = life
            shapes.append(shape)

    for label, source, subtract, opts in layers:
        first, last = opts["life"]
        add_layer(source, label, root, subtract, first, last, opts["visible"])
    shapes.sort(key=lambda shape: shape.get("blend") == "stencil")
    return _roto_blob(shapes, groups, anim, name, 1, 0.0, 0.0, invert, op_version)


def clipboard_text(nodes):
    for n in nodes:   # the type by name (Fred 0.2.20+ prefers it to the int)
        if "pluginType" not in n and n["type"] in TYPE_KEYS:
            n.setdefault("typeKey", TYPE_KEYS[n["type"]])
    return json.dumps({"nodes": nodes}, separators=(",", ":"))


def _selftest():
    # a 100x50 plate, a box that moves 10 px right per frame
    c = {1001 + t: [[10 + 10 * t, 10], [30 + 10 * t, 10], [10 + 10 * t, 40], [30 + 10 * t, 40]] for t in range(3)}
    n = cornerpin_node(c, 100.0, 50.0)
    assert n["anim"]["to1.x"]["keys"][0]["v"] == 0.1 and abs(n["anim"]["to1.y"]["keys"][0]["v"] - 0.2) < 1e-12   # TL: (10, 40 up) -> (0.1, 0.2 down)
    assert n["anim"]["to3.x"]["keys"][2]["v"] == 0.5 and abs(n["anim"]["to3.y"]["keys"][2]["v"] - 0.8) < 1e-12   # BR at frame 1003
    t = tracker_node(c, 50.0, 1001)
    tr = json.loads(t["pluginParams"]["vars"]["tracks"])["tracks"]
    assert tr[0]["name"] == "mocha_TL" and tr[0]["keys"][0] == [1001, 10, 10, 1.0, 0] and len(tr[0]["id"]) == 8
    assert t["type"] == plugin_type_id() and "transform" not in t["pluginParams"]["nums"]
    assert n["cpFilter"] == MITCHELL and n["mbShutter"] == 0.0
    # a pure 10 px/frame move: tx 10 per frame, no rotation, scale 1, pivot = the box centre (y down)
    xf, ref = transform_node(c, 100.0, 50.0, 1001)
    assert ref == 1001 and [k["v"] for k in xf["anim"]["tx"]["keys"]] == [0.0, 10.0, 20.0]
    assert all(abs(k["v"]) < 1e-12 for k in xf["anim"]["rotate"]["keys"]) and all(abs(k["v"] - 1) < 1e-12 for k in xf["anim"]["scaleF"]["keys"])
    assert xf["centerX"] == 20.0 - 50.0 and xf.get("centerY", 0.0) == 25.0 - 25.0
    # a 90 degree counter-clockwise turn (as seen on screen; Fred counts clockwise positive) about the box centre, doubled
    c2 = {1: [[0, 0], [2, 0], [0, 2], [2, 2]], 2: [[2, -2], [2, 2], [-2, -2], [-2, 2]]}
    m = motion_at(tracks_from_corners(c2, 0.0), 1, 2)
    assert abs(m[2] + 90.0) < 1e-9 and abs(m[3] - 2.0) < 1e-12, m
    blob = json.loads(clipboard_text([n, t]))
    assert isinstance(blob["nodes"], list) and blob["nodes"][0]["type"] == CORNERPIN
    assert blob["nodes"][0]["typeKey"] == "CornerPin"
    version = fred_version() or LATEST_FRED_VERSION
    assert blob["nodes"][1].get("typeKey") == ("Plugin" if version >= (0, 2, 20) else None)
    # Roto from Mocha's SSF: a 2-vertex toy on a 100 px plate, a feathered vertex and a plain one, two keys
    ssf = ("shake_shape_data 4.0\nnum_shapes 1\nshape_name L_0_mask\nclosed 1\nnum_vertices 2\nnum_key_times 2\n"
           "key_time 1001\nvertex_data 10 90 8 90 12 90 10 95 7 95 13 95  50 90 48 90 52 90 50 90 48 90 52 90\n"
           "key_time 1002\nvertex_data 11 90 9 90 13 90 11 95 8 95 14 95  51 90 49 90 53 90 51 90 49 90 53 90\n")
    sh = shapes_from_mocha_ssf(ssf)
    assert len(sh) == 1 and sorted(sh[0]["keys"]) == [1001, 1002]
    r = roto_node([("L", sh, False)], 100.0)
    p = r["maskShapes"][0]["keys"][0]["pts"][0]
    assert p[:6] == [10, 10, 8, 10, 12, 10] and p[6:8] == [0, -5]          # y flipped; feather 5 px up on screen
    assert p[11:15] == [-1, 0, 1, 0]                                         # edge handles 3 px vs the spline's 2
    # op version follows the target Fred: 0.2.34 = 5, 0.2.25-0.2.33 = 4, 0.2.23 / 0.2.24 = 3, older = none,
    # unknown = the newest; an explicit one wins
    assert roto_op_version((0, 2, 25)) == 4 and roto_op_version((0, 2, 24)) == 3 and roto_op_version((0, 2, 21)) is None
    assert roto_op_version(()) == 9
    assert roto_node([("L", sh, False)], 100.0, op_version=4)["opVersion"] == 4
    assert "opVersion" not in roto_node([("L", sh, False)], 100.0, op_version=0)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    testdata = os.path.join(repo, "tests", "testdata")
    missing = os.path.join(testdata, "missing_VERSION")
    saved = {k: os.environ.pop(k) for k in ("FRED_MOCHA_VERSION", "FRED_MOCHA_VERSION_FILE") if k in os.environ}
    try:
        assert fred_version(missing) == () and fred_version() == ()
        os.environ["FRED_MOCHA_VERSION"] = "0.2.25-build1 abc"
        assert fred_version(missing) == (0, 2, 25)
        assert roto_node([("L", sh, False)], 100.0)["opVersion"] == 4
        os.environ["FRED_MOCHA_VERSION"] = "0.2.34-build1 abc"
        assert roto_node([("L", sh, False)], 100.0)["opVersion"] == 5
        del os.environ["FRED_MOCHA_VERSION"]
        os.environ["FRED_MOCHA_VERSION_FILE"] = os.path.abspath(__file__)   # no version at its start
        assert fred_version() == ()
        os.environ["FRED_MOCHA_VERSION_FILE"] = os.path.join(testdata, "VERSION")
        assert fred_version() == (0, 2, 33) and roto_node([("L", sh, False)], 100.0)["opVersion"] == 4
        os.environ["FRED_MOCHA_VERSION"] = "0.2.34"   # the version itself wins over the file
        assert fred_version() == (0, 2, 34)
    finally:
        for k in ("FRED_MOCHA_VERSION", "FRED_MOCHA_VERSION_FILE"):
            os.environ.pop(k, None)
        os.environ.update(saved)
    # linear between keys (interp absent = 0), as Mocha's own Nest Roto paste keys them (word 258)
    assert r["maskShapes"][0]["pts"][1][6:8] == [0, 0] and "interp" not in r["maskShapes"][0]
    assert r["maskShapes"][0]["visible"] is True and "anim" not in r
    # 0.2.34 = Roto 5 (#204), 0.2.25-0.2.33 = 4
    assert roto_op_version((0, 2, 34)) == 5 and roto_op_version((0, 2, 33)) == 4 and roto_op_version((0, 2, 40)) == 5
    # a layer's in / out points: keys outside dropped, Hold opacity steps at whole frames
    hold = lambda c: [(k["t"], k["v"], k["i"]) for k in c["keys"]]
    assert roto_life_curve(None, None) is None
    assert hold(roto_life_curve(5, None)) == [(4, 0.0, 0), (5, 1.0, 0)]
    assert hold(roto_life_curve(None, 9)) == [(9, 1.0, 0), (10, 0.0, 0)]
    assert hold(roto_life_curve(5, 9)) == [(4, 0.0, 0), (5, 1.0, 0), (10, 0.0, 0)]
    assert hold(roto_life_curve(5, 5)) == [(4, 0.0, 0), (5, 1.0, 0), (6, 0.0, 0)]
    assert hold(roto_life_curve(9, 5)) == hold(roto_life_curve(5, 9))   # reversed: smaller first
    # Mocha in / out points (0-based) -> plate frames; the project's own ends stay open
    assert life_from_points(0, 146, 985, 147) == (None, None) and life_from_points(47, 146, 985, 147) == (1032, None)
    assert life_from_points(0, 100, 985, 147) == (None, 1085) and life_from_points(47, 146, 985) == (1032, 1131)
    tail = roto_node([("L", sh, False, {"life": (None, 1001)})], 100.0)
    assert "keys" not in tail["maskShapes"][0] and hold(tail["anim"]["rotoOp.1"]) == [(1001, 1.0, 0), (1002, 0.0, 0)]
    lr = roto_node([("L", sh, False, {"life": (1002, None)})], 100.0)
    s0 = lr["maskShapes"][0]
    assert "keys" not in s0 and s0["pts"] == r["maskShapes"][0]["keys"][1]["pts"]   # one key left: static
    assert hold(lr["anim"]["rotoOp.1"]) == [(1001, 0.0, 0), (1002, 1.0, 0)]
    assert "anim" not in roto_node([("L", sh, False, {"life": (None, None)})], 100.0)
    for bad in ([("L", sh, False, {"life": (1003, 1009)})],
                [("A", sh + sh, False, {"life": (2000, 2010)}), ("B", sh, False)]):   # one layer empty: refused
        try:
            roto_node(bad, 100.0)
            raise AssertionError("a layer alive on no exported frame must be refused")
        except ValueError as exc:
            assert "in / out points" in str(exc)
    # visibility: a hidden layer, or a contour Mocha writes "visible 0", ticks its shapes off
    assert roto_node([("L", sh, False, {"visible": False})], 100.0)["maskShapes"][0]["visible"] is False
    hid = shapes_from_mocha_ssf(ssf.replace("closed 1\n", "closed 1\nvisible 0\n"))
    assert hid[0]["visible"] is False and roto_node([("L", hid, False)], 100.0)["maskShapes"][0]["visible"] is False
    # two layers, two contours each: a folder per layer, sids and opacity channels per shape
    two = roto_node([("A", sh + sh, False, {"life": (1002, None)}), ("B", sh + sh, True)], 100.0, op_version=5)
    assert [g["name"] for g in two["maskGroups"]] == ["A", "B"] and [x["sid"] for x in two["maskShapes"]] == [1, 2, 3, 4]
    assert two["anim"]["rotoOp.1"] is not two["anim"]["rotoOp.2"]
    assert sorted(two["anim"]) == ["rotoOp.1", "rotoOp.2"] and [x["invert"] for x in two["maskShapes"]] == [False, False, True, True]
    # Mocha's own SSF (testdata: one feathered X-spline layer, Mocha Pro 2026.5)
    with open(os.path.join(testdata, "mocha_shake_rotoshape_feather_sample.ssf"), encoding="utf-8") as fh:
        real = shapes_from_mocha_ssf(fh.read())
    assert len(real) == 1 and len(real[0]["keys"]) == 108 and real[0]["visible"] is True
    rr = roto_node([("FeatherX", real, False, {"life": (1010, 1100)})], 2010.0, op_version=5)
    sr = rr["maskShapes"][0]
    assert "keys" not in sr and len(sr["pts"]) == 76   # the sample does not move: one static shape
    assert any(p[6] or p[7] for p in sr["pts"]) and hold(rr["anim"]["rotoOp.1"])[-1] == (1101, 0.0, 0)
    assert plugin_type_id((0, 2, 34)) == 124 and plugin_type_id((0, 2, 35)) == 126
    assert plugin_type_id((0, 3, 21)) == 136 and plugin_type_id(()) == 139
    assert json.loads(clipboard_text([rr]))["nodes"][0]["opVersion"] == 5
    assert json.loads(clipboard_text([r]))["nodes"][0]["typeKey"] == "Roto"
    static = roto_node([("L", [{"name": "s", "closed": True, "keys": {1: sh[0]["keys"][1001], 2: sh[0]["keys"][1001]}}], True)], 100.0, op_version=5)
    assert "keys" not in static["maskShapes"][0] and static["maskShapes"][0]["invert"] is True
    print("fred_blob selftest: PASS")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
