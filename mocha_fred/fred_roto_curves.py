"""Read the Roto curves stream from Mocha's Transform & Shape exporter.

The records, coordinate conversions and matrix layout follow Fred's Nest
importer. This reader handles Roto data without evaluating script commands.
"""
from bisect import bisect_right
import math
import re
import struct


_TOKEN = re.compile(r'[{}]|"(?:\\.|[^"\\])*"|(?:\\.|[^\s{}"\\])+')


def _tree(text):
    stack = [[]]
    # Ignore comments outside the curves data. Mocha puts a header before the node.
    for match in _TOKEN.finditer(re.sub(r"(?m)^\s*#.*$", "", text)):
        token = match[0]
        if token == "{":
            block = []
            stack[-1].append(block)
            stack.append(block)
        elif token == "}":
            if len(stack) == 1:
                raise ValueError("unexpected closing brace in Roto stream")
            stack.pop()
        else:
            stack[-1].append(re.sub(r"\\(.)", r"\1", token[1:-1]) if token.startswith('"') else token)
    if len(stack) != 1:
        raise ValueError("unclosed brace in Roto stream")
    return stack[0]


def _find(words, name):
    return next((word for word in words if isinstance(word, list) and word and word[0] == name), None)


def _number(word):
    if word.startswith("x"):
        value = struct.unpack(">f", int(word[1:], 16).to_bytes(4, "big"))[0]
    else:
        value = float(word)
    if not math.isfinite(value):
        raise ValueError("non-finite Roto value")
    return value


def _records(block):
    for record in block:
        if isinstance(record, list) and record and record[0] == "x":
            yield from [record[2]] * int(_number(record[1]))
        else:
            yield record


def _curve(block, frames=None):
    """A scalar or [(frame, value, interpolation word)] in the curves stream."""
    if isinstance(block, str):
        return _number(block), []
    if not block:
        return 0.0, []
    if block[0] == "r" and len(block) >= 2:
        block = [[str(_number(row[0]) + j), *row[1:]]
                 for row, count in block[1] for j in range(int(_number(count)))]
    records = list(_records(block))
    if not records:
        return 0.0, []
    if frames is not None:
        # The importer repeats the last value and word for unwritten frames.
        records = (records + [records[-1]] * max(0, len(frames) - len(records)))[:len(frames)]
        if not records:
            raise ValueError("Roto coordinate has values without frames")
    keys = []
    for index, row in enumerate(records):
        if frames is not None:
            value = _number(row[0] if isinstance(row, list) else row)
            mode = int(_number(row[5])) if isinstance(row, list) and len(row) >= 6 else 256
            keys.append((frames[index], value, mode))
        elif isinstance(row, str):
            return _number(row), []
        elif len(row) == 1:
            if not keys:
                raise ValueError("Roto carry key has no preceding value")
            keys.append((_number(row[0]), *keys[-1][1:]))
        elif (len(row) == 2 and row[1] == "-") or len(row) == 5:
            return _number(row[0]), []
        else:
            keys.append((_number(row[0]), _number(row[1]), int(_number(row[6])) if len(row) >= 7 else 256))
    if any(a[0] >= b[0] for a, b in zip(keys, keys[1:])):
        raise ValueError("Roto key times are out of order")
    return keys[0][1], keys if len(keys) > 1 else []


class _Curve:
    """The Nest importer's linear, smooth and hold modes for Roto scalars."""

    def __init__(self, raw, allow_hold=False):
        self.value, self.keys = raw
        if self.keys and all(v == self.value for _, v, _ in self.keys):
            self.keys = []
        self.frames = [frame for frame, _, _ in self.keys]
        self.modes = [word & 255 for _, _, word in self.keys]
        allowed = (0, 1, 2) if allow_hold else (0, 2)
        if any(word >> 8 != 1 or mode not in allowed for (_, _, word), mode in zip(self.keys, self.modes)):
            raise ValueError("unsupported interpolation in Mocha Roto stream")
        self.slopes = [[0.0, 0.0] for _ in self.keys]
        secants = [(b[1] - a[1]) / (b[0] - a[0]) for a, b in zip(self.keys, self.keys[1:])]
        fixed = [None] * len(self.keys)
        for i in range(1, len(self.keys) - 1):
            if self.modes[i] == 1:
                fixed[i] = 0.0
            elif self.modes[i] == 0:
                left, right = secants[i - 1:i + 1]
                if left * right <= 0:
                    fixed[i] = 0.0
                else:
                    a, b = self.keys[i - 1], self.keys[i + 1]
                    slope = (b[1] - a[1]) / (b[0] - a[0])
                    fixed[i] = math.copysign(min(abs(slope), 3 * min(abs(left), abs(right))), slope)
        for i, secant in enumerate(secants):
            a, b = fixed[i:i + 2]
            if self.modes[i] == 1:
                a = b = 0.0
            elif a is None and b is None:
                a = b = secant
            elif a is None:
                a = (3 * secant - b) / 2
            elif b is None:
                b = (3 * secant - a) / 2
            self.slopes[i][1], self.slopes[i + 1][0] = a, b

    def sample(self, frame):
        """Value and its left/right slopes at an original or split key."""
        i = bisect_right(self.frames, frame)
        if not self.keys:
            return self.value, 0.0, 0.0
        if not i:
            return self.keys[0][1], 0.0, 0.0
        t, v, _ = self.keys[i - 1]
        if frame == t:
            return v, *self.slopes[i - 1]
        if i == len(self.keys) or self.modes[i - 1] == 1:
            return v, 0.0, 0.0
        end, target, _ = self.keys[i]
        h = end - t
        x = (frame - t) / h
        a, b = self.slopes[i - 1][1] * h, self.slopes[i][0] * h
        d = target - v
        value = v + x * x * (3 - 2 * x) * d + x * (x - 1) ** 2 * a + x * x * (x - 1) * b
        slope = (6 * x * (1 - x) * d + (3 * x * x - 4 * x + 1) * a + (3 * x * x - 2 * x) * b) / h
        return value, slope, slope


def _transform(item, height):
    block = _find(item, "t")
    tx = _find(item, "tx")
    slots = block[1:] if block is not None else (tx[2:] if tx else [])
    if len(slots) > 35:
        raise ValueError("unexpected Roto transform slot")
    defaults = [0.0] * 19 + [float(i % 5 == 0) for i in range(16)]
    defaults[9:12] = [1.0] * 3
    values = [_curve(slots[i]) if i < len(slots) else (default, [])
              for i, default in enumerate(defaults)]
    static, curves = {}, {}
    fields = {0: ("cx", 1, 0), 1: ("cy", -1, height), 3: ("tx", 1, 0), 4: ("ty", -1, 0),
              8: ("rot", -1, 0), 9: ("sx", 1, 0), 10: ("sy", 1, 0),
              12: ("skx", -1, 0), 13: ("sky", -1, 0)}
    for i in range(19):
        value, keys = values[i]
        if i in fields:
            name, sign, offset = fields[i]
            static[name] = sign * value + offset
            curve = _Curve(values[i], allow_hold=True)
            if curve.keys:
                curves[name] = [(f, sign * v + offset, sign * left, sign * right, mode == 1)
                                for (f, v, _), (left, right), mode in
                                zip(curve.keys, curve.slopes, curve.modes)]
        elif i == 18 and not keys:
            static["skewOrder"] = 0 if abs(value) > 0.5 else 1
        elif keys or value != defaults[i]:
            raise ValueError("unsupported 3D or keyed skew-order Roto transform")

    def matrix(m):
        # Same F E F conjugation as the Nest importer, for F(x,y)=(x,H-y).
        a, b, c, d, e, f, g, h, w = (m[i] for i in (0, 1, 3, 4, 5, 7, 12, 13, 15))
        return [a, -b, height * b + c,
                -d + height * g, e - height * h, -height * e + height * height * h - f + height * w,
                g, -h, height * h + w]

    cells = [_Curve(curve, allow_hold=True) for curve in values[19:]]
    frames = sorted({f for cell in cells for f in cell.frames})
    static_matrix = matrix([cell.value for cell in cells])
    if frames:
        modes = {mode == 1 for cell in cells for mode in cell.modes[:-1]}
        if len(modes) > 1:
            raise ValueError("mixed Hold and continuous interpolation in a Roto matrix")
        rows = []
        for frame in frames:
            # The conjugation is linear, so it applies to the tangent rows too.
            v, left, right = [matrix(column) for column in zip(*(cell.sample(frame) for cell in cells))]
            rows.append((v, left, right))
        static["extra"] = rows[0][0]
        for i in range(9):
            if any(row[0][i] != rows[0][0][i] or row[1][i] or row[2][i] for row in rows):
                curves["m%d" % i] = [(f, v[i], left[i], right[i], modes == {True})
                                    for f, (v, left, right) in zip(frames, rows)]
    else:
        if static_matrix[8]:
            static_matrix = [v / static_matrix[8] for v in static_matrix]
        if static_matrix != [1., 0., 0., 0., 1., 0., 0., 0., 1.]:
            static["extra"] = static_matrix
    return static, curves


def moves(transform):
    static, curves = transform
    return bool(curves or "extra" in static or
                any(static[key] != (1 if key in ("sx", "sy") else 0)
                    for key in ("tx", "ty", "rot", "sx", "sy", "skx", "sky")))


def _points(block):
    px = _find(block, "px")
    data = px[2:] if px is not None else (_find(block, "p") or [])[1:]
    times = px[1] if px is not None else None
    if times is not None:
        times = [_number(t) for t in (times if isinstance(times, list) else [times])]
    if not data or len(data) % 3:
        raise ValueError("Roto curve needs Bezier handle/point/handle triples")
    out = []
    for pair in data:
        if _find(pair, "a") is not None:
            pair = [p for p in pair if not (isinstance(p, list) and p and p[0] == "a")]
        if len(pair) != 2:
            raise ValueError("invalid Roto point")
        for coord in pair:
            out.append(_Curve(_curve(coord, times)))
    return out


def _shape(item, height):
    if item[3] != "bezier":
        raise ValueError("unsupported Roto spline type %s" % item[3])
    curves = next((word for word in item if isinstance(word, list) and word and isinstance(word[0], list)), None)
    if not curves:
        raise ValueError("Roto shape has no curves")
    main = _points(curves[0])
    feather = _points(curves[1]) if len(curves) > 1 and isinstance(curves[1], list) else []
    if feather and len(feather) != len(main):
        raise ValueError("Roto feather and core point counts differ")
    flags = int(_number((_find(curves[0], "f") or ["f", "0"])[1]))
    frames = sorted({f for curve in main + feather for f in curve.frames})
    if any(not frame.is_integer() for frame in frames):
        raise ValueError("Fred Roto shape keys need whole frames")
    smooth = any(0 in curve.modes for curve in main + feather)

    def points_at(frame, component=0):
        points = []
        offset = height if component == 0 else 0.0
        for i in range(0, len(main), 6):
            lx, ly, x, y, rx, ry = [curve.sample(frame)[component] for curve in main[i:i + 6]]
            fx, fy = [curve.sample(frame)[component] for curve in feather[i + 2:i + 4]] if feather else (0., 0.)
            points.append([x, offset - y, x + lx, offset - y - ly, x + rx, offset - y - ry,
                           fx, -fy, 0., 0., 0.5 if component == 0 else 0.] + [0.] * 8)
        return points

    keys = [{"frame": int(f), "pts": points_at(f)} for f in frames]
    if smooth:
        for f, key in zip(frames, keys):
            key.update(sl=points_at(f, 1), sr=points_at(f, 2))
    attrs = _attrs(item)
    return {"name": item[1], "closed": not flags & 32, "visible": 1 <= attrs.get("vis", 1) < 2,
            "opacity": attrs.get("opc", 1), "pts": keys[0]["pts"] if keys else points_at(0),
            "keys": keys, "interp": 2 if smooth else 0, "xf": _transform(item, height)}


def _attrs(item):
    words = (_find(item, "a") or [])[1:]
    attrs = {}
    for name, word in zip(words[::2], words[1::2]):
        if name in ("opc", "vis") and name not in attrs:
            value, keys = _curve(word)
            if any(v != value for _, v, _ in keys):
                raise ValueError("animated Roto visibility or opacity is unsupported")
            attrs[name] = value
    return attrs


def read_layer(text, height):
    """Read one selected layer's source subtree, with points in folder space."""
    words = _tree(text)
    scripts = [block for word, block in zip(words, words[1:]) if word == "Roto" and isinstance(block, list)]
    if len(scripts) != 1:
        raise ValueError("expected one Roto node from Mocha")
    index = scripts[0].index("curves")
    curve_tree = scripts[0][index + 1]

    def layers(tree):
        for item in tree:
            if isinstance(item, list) and item:
                if item[0] == "layer":
                    yield item
                elif isinstance(item[0], list) or item[0] == "n":
                    yield from layers(item)

    def layer(item):
        attrs = _attrs(item)
        entries = []
        for child in reversed(item):
            if not isinstance(child, list) or not child:
                continue
            if child[0] == "curvegroup":
                entries.append(("shape", _shape(child, height)))
            elif child[0] == "layer":
                entries.append(("layer", layer(child)))
            elif child[0] == "cubiccurve":
                raise ValueError("Roto paint strokes are unsupported")
        return {"name": item[1], "xf": _transform(item, height), "opacity": attrs.get("opc", 1),
                "visible": 1 <= attrs.get("vis", 1) < 2, "entries": entries}

    roots = list(layers(curve_tree))
    if len(roots) != 1:
        raise ValueError("expected a single Roto layer tree")
    root = layer(roots[0])
    # A neutral host Root with a single layer is just the wrapper. A real
    # Root transform or gate stays in the hierarchy.
    if (root["name"] == "Root" and not moves(root["xf"]) and root["visible"] and root["opacity"] == 1
            and len(root["entries"]) == 1 and root["entries"][0][0] == "layer"):
        root = root["entries"][0][1]
    # If the stream repeats the same motion on all contours of a static layer,
    # factor it out once. Each contour still evaluates to the same position.
    if root["entries"] and all(kind == "shape" for kind, _ in root["entries"]) and not moves(root["xf"]):
        shapes = [shape for _, shape in root["entries"]]
        common = shapes[0]["xf"]
        if moves(common) and all(shape["xf"] == common for shape in shapes):
            for shape in shapes:
                shape["xf"] = root["xf"]
            root["xf"] = common
    return root
