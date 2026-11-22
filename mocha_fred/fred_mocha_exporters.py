"""Mocha Pro exporters for Fred: paste a Tracker, PlanarTrack, Transform or CornerPin (0.2.10+), or a
Roto (0.2.21+, where Roto replaced the deprecated Mask node).

Tracking: File > Export Tracking Data > group "Fred" > pick one > Copy to Clipboard.
Shapes: File > Export Shape Data > Fred Roto > Copy to Clipboard. Then Ctrl+V in Fred's
node graph and wire the plate in. Each exporter has one data stream, which is what gives Mocha's
Copy to Clipboard button; the text is Fred's own node-clipboard JSON (fred_blob.py).
Save to file writes the same text (.json).

  Fred Tracker     the four surface corners as four tracks (T, R, S on), reference frame =
                   the frame the export is made on. Since Fred 0.2.10 the Tracker is a tool
                   (it feeds a Viewer only): bake the tracks there with Create Transform or
                   Create CornerPin. Invert has no meaning here and is ignored.
  Fred PlanarTrack the editable tracked surface and reference frame, in the input format.
  Fred Transform   a keyed Transform = what Fred's Tracker > Create Transform makes from those
                   tracks (translate / rotate / scale about the corners' centroid). Invert
                   ticked = stabilise (Fred's Transform Invert).
  Fred CornerPin   the four corners, animated, From = the full frame (as Mocha's Nest Corner
                   Pin). Invert / Stabilize / Remove lens distortion: Mocha's own corner values.
  Fred Roto        the selected layers as one Roto. Fred 0.3.2+ gets one tracked
                   folder per layer, with Mocha's local shape keys inside it.
                   Older Fred builds get the baked Shake Rotoshape geometry.

Register from a Mocha init.py:   import fred_mocha_exporters; fred_mocha_exporters.register()
(init.py beside this file does that: point MOCHA_INIT_SCRIPT at this folder.)
"""

__version__ = "0.1.0"   # Mocha2Fred release; a bug report quotes it (Mocha prints it at start-up)

from mocha.exporters import AbstractShapeDataExporter, AbstractTrackingDataExporter
from PySide6.QtCore import QByteArray

import fred_blob
import fred_roto_curves


def _opts(options):
    return {str(k): bool(v) for k, v in dict(options).items()}


def _corners(project, layer, tracking_file_path, time, view, options, use_mocha_corners):
    """{plate_frame: [[x, y] * 4]} in Mocha order (BL, BR, TL, TR), plate pixels, y up."""
    if use_mocha_corners:
        # Mocha bakes Invert / Stabilize / Remove lens distortion into the corners: take its numbers
        nest_cp = AbstractTrackingDataExporter.registered_exporters()["nuke_corner_pin"]
        res = nest_cp.do_export(project, layer, tracking_file_path, time, view, options)
        if not res:
            raise RuntimeError("Mocha's Nest Corner Pin export failed: %s" % nest_cp.error_string())
        return fred_blob.corners_from_nest_cornerpin(bytes(list(res.values())[0]).decode("utf-8"))
    ffo = project.first_frame_offset
    return {t + ffo: [list(layer.get_surface_position(i, float(t), view)) for i in range(4)]
            for t in range(layer.in_point(view), layer.out_point(view) + 1)}


class _FredExporter(AbstractTrackingDataExporter):
    LABEL = ""
    INTERNAL = ""

    def __init__(self):
        # positional (PySide): name, extension, streams, interlaced, remove_lens_distortion,
        # multiple_views, program, internal_name
        AbstractTrackingDataExporter.__init__(self, "%s (*.json)" % self.LABEL, ".json",
                                              1, False, False, False, "Fred", self.INTERNAL)
        self._error = ""

    def error_string(self):
        return self._error

    def nodes(self, project, layer, tracking_file_path, time, view, options):
        raise NotImplementedError

    def do_export(self, project, layer, tracking_file_path, time, view, options):
        self._error = ""
        try:
            nodes = self.nodes(project, layer, tracking_file_path, time, view, options)
            return {tracking_file_path: QByteArray(fred_blob.clipboard_text(nodes).encode("utf-8"))}
        except Exception as exc:   # shown in Mocha's export dialog
            self._error = "%s: %s" % (self.LABEL, exc)
            return {}


def _size_and_names(project, layer, time):
    width, height = layer.tracking_input_clip().frame_size
    return float(width), float(height), layer.name.replace(" ", "_"), int(round(time)) + project.first_frame_offset


class FredTrackerExporter(_FredExporter):
    LABEL = "Fred Tracker"
    INTERNAL = "fred_tracker"

    def nodes(self, project, layer, tracking_file_path, time, view, options):
        o = _opts(options)
        width, height, name, ref = _size_and_names(project, layer, time)
        corners = _corners(project, layer, tracking_file_path, time, view, options, bool(o.get("RemoveLensDistortion")))
        if ref not in corners:
            ref = min(corners)
        return [fred_blob.tracker_node(corners, height, ref, "Tracker_" + name, 1, 0.0, 0.0, layer.name)]


class FredPlanarTrackExporter(_FredExporter):
    LABEL = "Fred PlanarTrack"
    INTERNAL = "fred_planartrack"

    def nodes(self, project, layer, tracking_file_path, time, view, options):
        width, height, name, ref = _size_and_names(project, layer, time)
        corners = _corners(project, layer, tracking_file_path, time, view, options, use_mocha_corners=False)
        return [fred_blob.planartrack_node(corners, width, height, ref, "PlanarTrack_" + name)]


class FredTransformExporter(_FredExporter):
    LABEL = "Fred Transform"
    INTERNAL = "fred_transform"

    def nodes(self, project, layer, tracking_file_path, time, view, options):
        o = _opts(options)
        width, height, name, ref = _size_and_names(project, layer, time)
        corners = _corners(project, layer, tracking_file_path, time, view, options, bool(o.get("RemoveLensDistortion")))
        stab = bool(o.get("Invert"))
        node, _ = fred_blob.transform_node(corners, width, height, ref, ("Stabilise_" if stab else "MatchMove_") + name,
                                           1, 0.0, 0.0, stab, layer.name)
        return [node]


class FredCornerPinExporter(_FredExporter):
    LABEL = "Fred CornerPin"
    INTERNAL = "fred_cornerpin"

    def nodes(self, project, layer, tracking_file_path, time, view, options):
        o = _opts(options)
        width, height, name, _ = _size_and_names(project, layer, time)
        mocha_corners = any(o.get(k) for k in ("Invert", "Stabilize", "RemoveLensDistortion"))
        corners = _corners(project, layer, tracking_file_path, time, view, options, mocha_corners)
        return [fred_blob.cornerpin_node(corners, width, height, "CornerPin_" + name, 1, 0.0, 0.0, False)]


def _layer_flag(layer, name):
    try:
        return layer.parameter(["Basic", name]).get()
    except Exception:   # an older Mocha without the parameter: the default
        return None


def _layer_options(project, layer, view):
    """fred_blob.roto_node's per-layer options: "life" = the layer's in / out points as plate
    frames (fred_blob.life_from_points: an end at the project's own first / last frame stays
    open), and "visible"."""
    try:
        length = int(project.length)
    except Exception:   # no project length: the out point is kept
        length = None
    try:
        visible = bool(layer.visibility)
    except Exception:
        visible = True
    return {"life": fred_blob.life_from_points(layer.in_point(view), layer.out_point(view),
                                               project.first_frame_offset, length),
            "visible": visible}


class FredRotoExporter(AbstractShapeDataExporter):
    """File > Export Shape Data > Fred Roto: the selected layers' shapes as ONE Fred Roto
    node. Roto 6+ uses Mocha's Transform & Shape stream for editable tracked folders.
    Older Fred builds use Mocha's Shake Rotoshape stream with tracking baked."""
    LABEL = "Fred Roto"
    INTERNAL = "fred_roto"

    def __init__(self):
        # positional (PySide): name, extension, streams, multiple_shapes, multiple_views,
        # open_splines, interlaced, program, internal_name
        AbstractShapeDataExporter.__init__(self, "%s (*.json)" % self.LABEL, ".json",
                                           1, True, False, False, False, "Fred", self.INTERNAL)
        self._error = ""

    def error_string(self):
        return self._error

    def do_export(self, project, layers, shape_file_path, views):
        self._error = ""
        try:
            layers = list(layers)
            if not layers:
                raise RuntimeError("select at least one layer")
            inverted = [bool(_layer_flag(l, "Invert")) for l in layers]
            if any(inverted) and not all(inverted):
                # Fred inverts a Roto's whole matte; a per-layer invert has no Fred equivalent
                raise RuntimeError("some selected layers are inverted and some are not: "
                                   "export the inverted layers as a Roto of their own")
            op_version = fred_blob.roto_op_version() or 0
            folders = op_version >= 6
            source_id = "nuke_roto_transform_and_shape" if folders else "shake_rotoshape"
            source = AbstractShapeDataExporter.registered_exporters().get(source_id)
            label = "Transform & Shape" if folders else "Shake Rotoshape"
            if source is None:
                raise RuntimeError("Mocha's %s exporter is unavailable" % label)
            height = float(layers[0].tracking_input_clip().frame_size[1])
            view = list(views)[0] if views else 0
            parts = []
            for l in layers:
                res = source.do_export(project, [l], shape_file_path, views)
                if not res:
                    raise RuntimeError("Mocha's %s export failed for %s: %s" % (label, l.name, source.error_string()))
                streams = [bytes(data).decode("utf-8") for data in res.values()]
                if folders:
                    if len(streams) != 1:
                        raise ValueError("expected one Transform & Shape stream for %s" % l.name)
                    try:
                        data = fred_roto_curves.read_layer(streams[0], height)
                    except (ValueError, IndexError, TypeError) as exc:
                        raise ValueError("cannot read Transform & Shape for %s: %s" % (l.name, exc)) from exc
                else:
                    data = [shape for stream in streams for shape in fred_blob.shapes_from_mocha_ssf(stream)]
                parts.append((l.name if folders else l.name.replace(" ", "_"), data, _layer_flag(l, "BlendMode") == 1,
                              _layer_options(project, l, view)))
            name = "Roto_" + (parts[0][0].replace(" ", "_") if len(parts) == 1 else "mocha")
            if folders:
                node = fred_blob.roto_folders_node(parts, name, all(inverted), op_version)
            else:
                node = fred_blob.roto_node(parts, height, name, 1, 0.0, 0.0, all(inverted), op_version)
            return {shape_file_path: QByteArray(fred_blob.clipboard_text([node]).encode("utf-8"))}
        except Exception as exc:   # shown in Mocha's export dialog
            self._error = "%s: %s" % (self.LABEL, exc)
            return {}


_INSTANCES = []


def register():
    """Register the exporters once (a second call is a no-op)."""
    if not _INSTANCES:
        for cls in (FredTrackerExporter, FredPlanarTrackExporter, FredTransformExporter,
                    FredCornerPinExporter, FredRotoExporter):
            e = cls()
            e.register()
            _INSTANCES.append(e)
    return list(_INSTANCES)
