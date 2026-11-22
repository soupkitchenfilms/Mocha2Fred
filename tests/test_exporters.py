"""Exercise Mocha's exporter entry points using synthetic layers and API stubs."""
import json
import ntpath
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, mock_open, patch


EXPORTER_DIR = Path(__file__).resolve().parents[1] / "mocha_fred"
TESTDATA = Path(__file__).resolve().parent / "testdata"


class ExporterStub:
    def __init__(self, *args):
        pass


class ShapeExporterStub(ExporterStub):
    @classmethod
    def registered_exporters(cls):
        return {"shake_rotoshape": cls(), "nuke_roto_transform_and_shape": TransformShapeStub()}

    def do_export(self, project, layers, path, views):
        data = (TESTDATA / "mocha_shake_rotoshape_feather_sample.ssf").read_text(encoding="utf-8")
        data = data.replace("\n", "\r\n").encode("utf-8")
        return {path: data * layers[0].contours}


class TransformShapeStub:
    def do_export(self, project, layers, path, views):
        if len(layers) != 1:
            raise AssertionError("one export per selected layer")
        data = (TESTDATA / "mocha_transform_and_shape_sample.txt").read_text(encoding="utf-8")
        return {path: data.replace("\n", "\r\n").encode("utf-8")}


mocha_api = ModuleType("mocha.exporters")
mocha_api.AbstractShapeDataExporter = ShapeExporterStub
mocha_api.AbstractTrackingDataExporter = ExporterStub
qt = ModuleType("PySide6.QtCore")
qt.QByteArray = bytes
with patch.object(sys, "path", [str(EXPORTER_DIR), *sys.path]):
    import fred_blob
    import fred_roto_curves  # Keep the parser available for the lazy import in fred_blob.

    with patch.dict(sys.modules, {"mocha.exporters": mocha_api, "PySide6.QtCore": qt}):
        import fred_mocha_exporters as exporters


class Layer:
    visibility = True

    def __init__(self, name="Layer", subtract=False, contours=1):
        self.name = name
        self.subtract = subtract
        self.contours = contours

    def in_point(self, view):
        return 0

    def out_point(self, view):
        return 1

    def tracking_input_clip(self):
        return SimpleNamespace(frame_size=(100, 50))

    def get_surface_position(self, index, time, view):
        x, y = ((10, 10), (30, 10), (10, 40), (30, 40))[index]
        return x + 10 * time, y

    def parameter(self, path):
        return SimpleNamespace(get=lambda: int(self.subtract) if path[-1] == "BlendMode" else False)


class ExportersTest(unittest.TestCase):
    project = SimpleNamespace(first_frame_offset=1001, length=3)
    path = "export.json"

    def exported_node(self, cls, version="0.3.22", *, layers=None, time=1, options=None):
        exporter = cls()
        with patch.dict(os.environ, {"FRED_MOCHA_VERSION": version, "FRED_MOCHA_VERSION_FILE": ""}):
            if layers is None:
                result = exporter.do_export(self.project, Layer(), self.path, time, 0, options or {})
            else:
                result = exporter.do_export(self.project, layers, self.path, [0])
        self.assertTrue(result, exporter.error_string())
        nodes = json.loads(result[self.path])["nodes"]
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["inputs"], [])
        return nodes[0]

    def test_latest_transform(self):
        node = self.exported_node(exporters.FredTransformExporter, options={"Invert": True})
        self.assertEqual((node["typeKey"], node["opVersion"]), ("Transform", 3))
        self.assertEqual(set(node["anim"]), {"tx", "ty", "rotate", "scaleF", "scaleY"})
        self.assertEqual([key["v"] for key in node["anim"]["tx"]["keys"]], [-10, 0])
        self.assertEqual(node["centerX"], -20)
        self.assertTrue(node["xformInvert"])
        self.assertEqual((node["xformFilter"], node["mbShutter"], node["mbSamples"]), (4, 0, 8))
        self.assertNotIn("xformNoClip", node)

    def test_latest_cornerpin(self):
        node = self.exported_node(exporters.FredCornerPinExporter)
        self.assertEqual((node["typeKey"], node["opVersion"]), ("CornerPin", 4))
        self.assertEqual((node["cpFrom1x"], node["cpFrom1y"], node["cpFrom3x"], node["cpFrom3y"]), (0, 0, 1, 1))
        self.assertAlmostEqual(node["cpTo1y"], 0.2)
        self.assertEqual(node["anim"]["to3.x"]["keys"][1], {
            "t": 1002, "v": 0.4, "i": 1, "idx": 0, "idy": 0, "odx": 0, "ody": 0,
        })
        self.assertEqual((node["cpFilter"], node["mbShutter"], node["mbSamples"]), (4, 0, 8))

    def test_cornerpin_reads_mocha_curves_with_crlf(self):
        source = "\r\n".join(
            "to%d {{curve x1001 %d %d}\r\n {curve x1001 10 20}}" % (i, i * 10, i * 10 + 5)
            for i in range(1, 5)
        )
        path = r"C:\Fred Exports\surface.json"
        mocha_pin = SimpleNamespace(do_export=Mock(return_value={path: source.encode("utf-8")}))
        with patch.object(ExporterStub, "registered_exporters", return_value={"nuke_corner_pin": mocha_pin},
                          create=True), patch.dict(os.environ, {"FRED_MOCHA_VERSION": "0.3.22"}):
            result = exporters.FredCornerPinExporter().do_export(
                self.project, Layer("Café"), path, 1, 0, {"Invert": True}
            )
        self.assertNotIn(b"\r", result[path])
        self.assertNotIn(b"\n", result[path])
        node = json.loads(result[path].decode("utf-8"))["nodes"][0]
        self.assertEqual(node["name"], "CornerPin_Café")
        self.assertEqual([key["v"] for key in node["anim"]["to1.x"]["keys"]], [0.4, 0.45])

    def test_latest_tracker(self):
        node = self.exported_node(exporters.FredTrackerExporter)
        self.assertEqual((node["type"], node["pluginType"], node["opVersion"]), (139, "com.skf.tracker", 1))
        self.assertEqual(node["typeKey"], "Plugin")
        params = node["pluginParams"]
        self.assertEqual(params["nums"], {"refFrame": 1002})
        doc = json.loads(params["vars"]["tracks"])
        self.assertEqual(doc["active"], 0)
        self.assertEqual([track["name"] for track in doc["tracks"]], ["mocha_TL", "mocha_TR", "mocha_BR", "mocha_BL"])
        track = doc["tracks"][0]
        self.assertEqual(set(track), {"id", "name", "on", "pw", "ph", "sx", "sy", "ox", "oy", "t", "r", "s", "keys"})
        self.assertEqual(track["keys"], [[1001, 10, 10, 1, 0], [1002, 20, 10, 1, 0]])

    def test_latest_planartrack(self):
        node = self.exported_node(exporters.FredPlanarTrackExporter)
        self.assertEqual((node["type"], node["typeKey"], node["opVersion"]), (65, "PlanarTrack", 3))
        self.assertEqual((node["planarRefFrame"], node["planarAlpha"], node["planarOutMode"]), (1002, 1, 0))
        self.assertEqual(node["maskShapes"], [])
        self.assertEqual(node["planarCorners"], node["planarTracked"]["1002"])
        self.assertEqual(list(node["planarTracked"]), ["1001", "1002"])
        for actual, expected in zip(node["planarCorners"], [0.2, 0.2, 0.4, 0.2, 0.4, 0.8, 0.2, 0.8]):
            self.assertAlmostEqual(actual, expected)
        fallback = self.exported_node(exporters.FredPlanarTrackExporter, time=10)
        self.assertEqual(fallback["planarRefFrame"], 1001)

    def test_latest_roto(self):
        layers = [Layer("Hole", subtract=True), Layer("Add")]
        node = self.exported_node(exporters.FredRotoExporter, layers=layers)
        self.assertEqual((node["typeKey"], node["opVersion"]), ("Roto", 9))
        self.assertTrue(node["maskReplaceAlpha"])
        self.assertFalse(node["maskPremult"])
        root, hole_folder, add_folder = node["maskGroups"]
        self.assertTrue(root["root"])
        self.assertEqual([f["name"] for f in (hole_folder, add_folder)], ["Hole", "Add"])
        self.assertEqual([f["parent"] for f in (hole_folder, add_folder)], [root["id"]] * 2)
        add, hole = node["maskShapes"]
        self.assertEqual((add["sid"], hole["sid"]), (2, 1))
        self.assertEqual((add["group"], hole["group"]), (add_folder["id"], hole_folder["id"]))
        self.assertNotIn("blend", add)
        self.assertEqual(hole["blend"], "stencil")
        self.assertFalse(hole["invert"])
        self.assertEqual(len(hole["pts"][0]), 19)
        self.assertEqual(hole["paintRGBA"], [1, 1, 1, 1])
        xf = add_folder["xf"]
        self.assertEqual({k: xf[k] for k in ("cx", "cy", "tx", "ty", "rot", "sx", "sy", "skx", "sky", "skewOrder")},
                         {"cx": 25, "cy": 20, "tx": 0, "ty": -2, "rot": -15,
                          "sx": 1.5, "sy": .8, "skx": -.2, "sky": -.1, "skewOrder": 0})
        self.assertEqual(xf["extra"], [1, 0, 0, 0, 1, 0, 0, 0, 1])
        anim = node["anim"]
        tx = anim["rotoLayer.3.tx"]["keys"]
        self.assertEqual([(k["t"], k["v"]) for k in tx], [(1001, 0), (1002, 8)])
        self.assertAlmostEqual(anim["rotoLayer.3.m3"]["keys"][1]["v"], .05)
        self.assertAlmostEqual(anim["rotoLayer.3.m6"]["keys"][1]["v"], .001)
        self.assertEqual([k["frame"] for k in add["keys"]], [1001, 1003])
        self.assertEqual(add["keys"][0]["pts"][0][:8], [10, 20, 8, 20, 12, 20, 0, -3])
        self.assertEqual(add["keys"][1]["pts"][0][:8], [14, 20, 12, 20, 16, 20, 0, -3])
        self.assertNotIn("xforms", add)
        life = node["anim"]["rotoOp.1"]["keys"]
        self.assertEqual([(k["t"], k["v"], k["i"]) for k in life], [(1002, .75, 0), (1003, 0, 0)])

    def test_roto_older_fred_keeps_shake_geometry(self):
        node = self.exported_node(exporters.FredRotoExporter, version="0.3.1", layers=[Layer(contours=2)])
        self.assertEqual(node["opVersion"], 5)
        self.assertEqual(len(node["maskShapes"]), 2)
        self.assertEqual(len(node["maskShapes"][0]["pts"]), 76)
        self.assertFalse(any(k.startswith("rotoLayer.") for k in node["anim"]))

    def test_roto_smooth_shape_and_hold_folder(self):
        path = TESTDATA / "mocha_transform_and_shape_sample.txt"
        text = path.read_text(encoding="utf-8").replace("1001 0 0 0 0 0 258", "1001 0 0 0 0 0 257", 1)
        text = text.replace("{1001 1003}", "{1001 1003 1005}").replace(
            "{14 0 0 0 0 258}", "{14 0 0 0 0 256} {16 0 0 0 0 258}"
        )
        with patch.object(TransformShapeStub, "do_export", return_value={self.path: text.encode("utf-8")}):
            node = self.exported_node(exporters.FredRotoExporter, layers=[Layer()])
        self.assertEqual(node["anim"]["rotoLayer.2.tx"]["keys"][0]["i"], 0)
        shape = node["maskShapes"][0]
        self.assertEqual(shape["interp"], 2)
        self.assertEqual([key["frame"] for key in shape["keys"]], [1001, 1003, 1005])
        self.assertEqual(shape["keys"][1]["sl"][0][:2], [1.5, 0])
        self.assertEqual(shape["keys"][1]["sr"][0][:2], [1.5, 0])

    def test_roto_missing_transform_and_shape_reports_error(self):
        exporter = exporters.FredRotoExporter()
        with patch.dict(os.environ, {"FRED_MOCHA_VERSION": "0.3.22"}), patch.object(
            ShapeExporterStub, "registered_exporters", return_value={"shake_rotoshape": ShapeExporterStub()}
        ):
            self.assertEqual(exporter.do_export(self.project, [Layer()], self.path, [0]), {})
        self.assertIn("Transform & Shape exporter is unavailable", exporter.error_string())

    def test_release_gates(self):
        cases = [
            ("0.2.10", (None, None, None, None, None), 111),
            ("0.2.22", (None, None, None, None, None), 121),
            ("0.2.23", (3, 1, 1, 1, 1), 121),
            ("0.2.24", (3, 1, 1, 1, 1), 121),
            ("0.2.25", (4, 1, 1, 1, 1), 121),
            ("0.2.28", (4, 1, 1, 1, 1), 121),
            ("0.2.29", (4, 1, 2, 2, 1), 123),
            ("0.2.33", (4, 1, 2, 2, 1), 124),
            ("0.2.34", (5, 1, 2, 3, 1), 124),
            ("0.2.35", (5, 1, 2, 3, 1), 126),
            ("0.3.1", (5, 1, 2, 3, 1), 126),
            ("0.3.2", (8, 1, 2, 3, 2), 126),
            ("0.3.21", (9, 1, 3, 4, 3), 136),
            ("0.3.22", (9, 1, 3, 4, 3), 139),
            ("", (9, 1, 3, 4, 3), 139),
        ]
        for version, expected, plugin_id in cases:
            with self.subTest(version=version):
                # The old Roto entry point requires Fred 0.2.21 or newer.
                nodes = [self.exported_node(cls, version) for cls in (
                    exporters.FredTrackerExporter, exporters.FredTransformExporter,
                    exporters.FredCornerPinExporter, exporters.FredPlanarTrackExporter,
                )]
                tracker, transform, pin, planar = nodes
                self.assertEqual(tuple(n.get("opVersion") for n in nodes), expected[1:])
                self.assertEqual(tracker["type"], plugin_id)
                self.assertEqual("typeKey" in tracker, version != "0.2.10")
                self.assertEqual("mbSamples" in transform, expected[2] == 3)
                self.assertEqual("mbSamples" in pin, expected[3] == 4)
                self.assertEqual("maskShapes" in planar, (expected[4] or 0) >= 2)
                self.assertEqual("planarAlpha" in planar, expected[4] == 3)
                if version == "0.2.10":
                    continue
                roto = self.exported_node(exporters.FredRotoExporter, version, layers=[Layer(subtract=True)])
                self.assertEqual(roto.get("opVersion"), expected[0])
                shape = roto["maskShapes"][0]
                modern = (expected[0] or 0) >= 6
                self.assertEqual(shape.get("blend"), "stencil" if modern else None)
                self.assertEqual(shape["invert"], not modern)
                self.assertEqual("maskGroups" in roto, modern)
                self.assertEqual(any(k.startswith("rotoLayer.") for k in roto.get("anim", {})), modern)


class WindowsPathsTest(unittest.TestCase):
    def test_startup_hook_uses_windows_script_directory(self):
        folder = r"C:\Fred Tools\mocha_fred"
        script = EXPORTER_DIR / "init.py"
        code = compile(script.read_text(encoding="utf-8"), str(script), "exec")
        register = Mock()
        with patch.object(sys, "path", sys.path.copy()), patch.dict(sys.modules, {
            "os": SimpleNamespace(path=ntpath),
            "fred_mocha_exporters": SimpleNamespace(register=register),
        }):
            exec(code, {"__file__": ntpath.join(folder, "init.py")})
            self.assertEqual(sys.path[0], folder)
        register.assert_called_once_with()

    def test_windows_version_file_path_and_fallback(self):
        path = r"C:\Fred Tools\Café\VERSION"
        env = {"FRED_MOCHA_VERSION": "", "FRED_MOCHA_VERSION_FILE": path}
        read = mock_open(read_data="0.3.2-build1 abc\r\n")
        with patch.dict(os.environ, env), patch("builtins.open", read):
            self.assertEqual(fred_blob.fred_version(), (0, 3, 2))
        read.assert_called_once_with(path, encoding="utf-8-sig")
        for error in (FileNotFoundError(), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")):
            with self.subTest(error=error), patch.dict(os.environ, env), patch("builtins.open", side_effect=error):
                self.assertEqual(fred_blob.fred_version(), ())
                self.assertEqual(fred_blob.roto_op_version(), 9)


if __name__ == "__main__":
    unittest.main()
