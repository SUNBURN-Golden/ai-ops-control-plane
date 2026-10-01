"""Tests for the inspector's template charts (control_plane_inspect_charts.py).

The PNG filter/validator and chart-data validation are pure standard library and
always run. Render tests run only where matplotlib (and, for real renders, a font
covering Korean) is installed; CI has neither, so they skip there.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from contextlib import redirect_stdout

sys.path.insert(0, str(Path(__file__).resolve().parent))

import control_plane_inspect_charts as charts  # noqa: E402

MODULE = Path(charts.__file__).resolve()
HAS_MPL = importlib.util.find_spec("matplotlib") is not None
FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
)


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def make_png(width: int = 3, height: int = 2, extra: tuple = (), ihdr_size: tuple = None) -> bytes:
    """A tiny RGB PNG built with zlib/struct; `extra` chunks go between IHDR and IDAT."""
    w, h = ihdr_size or (width, height)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xfc\xfc\xfb" * width for _ in range(height))
    parts = [charts.PNG_SIGNATURE, _chunk(b"IHDR", ihdr)]
    parts += [_chunk(kind, body) for kind, body in extra]
    parts += [_chunk(b"IDAT", zlib.compress(raw)), _chunk(b"IEND", b"")]
    return b"".join(parts)


def sample_doc() -> dict:
    """A small valid AIOPS_INSPECT_CHARTS_V1 document."""
    ladder_row = {"planned": 1, "materializing": 0, "not_started": 2, "in_progress": 1, "delivered": 1, "done": 3,
                  "merge_checks": {"PASS": 2, "FAIL": 0, "PENDING": 1, "NOT_CONFIGURED": 0},
                  "deployed": "NOT_RECORDED", "verified": "NOT_RECORDED", "orphans": 1, "pending_plan_prs": 0}
    return {
        "schema": "AIOPS_INSPECT_CHARTS_V1", "generated_at": "2026-10-01T05:17:00Z", "snapshot": "0123456789ab",
        "stage": "DRY",
        "scorecard": {"products": ["kix-protocol", "ZARI", "CTRL"], "signals": list(charts.SIGNALS),
                      "cells": {"kix-protocol": {s: "ON_TRACK" for s in charts.SIGNALS},
                                "ZARI": {"S1": "WATCH", "S3": "WATCH", "S6": "AT_RISK", "S8": "NOT_CONFIGURED",
                                         "S4": "UNKNOWN"},
                                "CTRL": {"S0": "ON_TRACK", "S7": "WATCH"}},
                      "verdicts": {"kix-protocol": "ON_TRACK", "ZARI": "AT_RISK", "CTRL": "WATCH"},
                      "previous": {"kix-protocol": None, "ZARI": "WATCH", "CTRL": "WATCH"},
                      "basis": {"ZARI": {"S6": "GH_TEXT"}}},
        "ladder": {"kix-protocol": dict(ladder_row), "ZARI": dict(ladder_row, done=0,
                                                                  merge_checks={"NOT_CONFIGURED": 4})},
        "dag": {
            "kix-protocol": {"prefix": "KIXP", "nodes": [
                {"id": "N1", "stage": "DONE", "depth": 0, "row": 0, "blocked": False, "critical": True,
                 "blocked_hours": None, "basis_blocked": None},
                {"id": "N2", "stage": "IN_PROGRESS", "depth": 1, "row": 0, "blocked": True, "critical": True,
                 "blocked_hours": 30, "basis_blocked": "GH_TEXT"},
                {"id": "N3", "stage": "PLANNED", "depth": 1, "row": 1}], "edges": [["N1", "N2"], ["N1", "N3"]]},
            "ZARI": {"prefix": "ZARI", "nodes": [
                {"id": "A", "stage": "NOT_STARTED", "depth": 0, "row": 0},
                {"id": "B", "stage": "PLANNED", "depth": 1, "row": 0},
                {"id": "C", "stage": "MATERIALIZING", "depth": 1, "row": 1}], "edges": [["A", "B"]]},
            "maeum-gyeol": {"nodes": [{"id": "N1", "stage": "DELIVERED", "depth": 0, "row": 0}], "edges": []},
            "done-only": {"nodes": [{"id": "N1", "stage": "DONE", "depth": 0, "row": 0}], "edges": []},
        },
        "burnup": {"kix-protocol": [{"t": "2026-09-20T00:00:00Z", "done": 0, "planned": 5},
                                    {"t": "2026-09-25T00:00:00Z", "done": 2, "planned": 6},
                                    {"t": "2026-10-01T05:00:00Z", "done": 3, "planned": 6}],
                   "ZARI": [{"t": "2026-10-01T05:00:00Z", "done": 0, "planned": 3}], "CTRL": []},
        "lanes": {"window_start": "2026-09-24T05:17:00Z", "window_end": "2026-10-01T05:17:00Z",
                  "order": list(charts.LANE_ORDER), "enabled": {"DEVIN": True, "GLM": False},
                  "intervals": [
                      {"lane": "DEVIN", "start": "2026-09-25T01:00:00Z", "end": "2026-09-25T20:00:00Z",
                       "role": "WRITER", "task": "KIXP-N1", "state": "RECONCILED"},
                      {"lane": "GROK_BUILD", "start": "2026-09-26T01:00:00Z", "end": "2026-09-26T02:00:00Z",
                       "role": "REVIEWER", "task": "KIXP-N1", "state": "RECONCILED"},
                      {"lane": "CURSOR", "start": "2026-09-30T01:00:00Z", "end": None,
                       "role": "WRITER", "task": "ZARI-A", "state": "CONFIRMED"}],
                  "idle_waiting": [{"start": "2026-09-27T00:00:00Z", "end": "2026-09-27T06:00:00Z"}]},
    }


def korean_font() -> str:
    for path in FONT_CANDIDATES:
        if os.path.isfile(path):
            return path
    return ""


class PngTests(unittest.TestCase):
    def test_filter_strips_text_and_time_chunks(self):
        dirty = make_png(extra=((b"tEXt", b"Software\x00matplotlib"), (b"tIME", b"\x07\xea\x0a\x01\x05\x11\x00"),
                                (b"iTXt", b"Title\x00\x00\x00\x00\x00x"), (b"zTXt", b"k\x00\x00" + zlib.compress(b"v")),
                                (b"pHYs", struct.pack(">IIB", 5906, 5906, 1))))
        with self.assertRaises(ValueError):
            charts.validate_png(dirty)
        clean = charts.filter_png(dirty)
        self.assertEqual(charts.validate_png(clean), (3, 2))
        for kind in (b"tEXt", b"tIME", b"iTXt", b"zTXt"):
            self.assertNotIn(kind, clean)
        self.assertIn(b"pHYs", clean)
        self.assertEqual(charts.filter_png(clean), clean)

    def test_filter_drops_unknown_ancillary_and_refuses_unknown_critical(self):
        clean = charts.filter_png(make_png(extra=((b"eXIf", b"MM\x00*"),)))
        self.assertNotIn(b"eXIf", clean)
        with self.assertRaises(ValueError):
            charts.filter_png(make_png(extra=((b"ABCD", b"x"),)))

    def test_validator_rejects_malformed(self):
        good = make_png()
        self.assertEqual(charts.validate_png(good), (3, 2))
        bad_crc = bytearray(good)
        bad_crc[29] ^= 0xFF  # inside the IHDR CRC
        cases = {
            "signature": b"\x89PNX" + good[4:],
            "crc": bytes(bad_crc),
            "truncated": good[:-6],
            "trailing": good + b"junk",
            "not bytes": "text",
            "empty": b"",
            "wide": make_png(ihdr_size=(2401, 2)),
            "tall": make_png(ihdr_size=(3, 1801)),
            "zero": make_png(ihdr_size=(0, 2)),
        }
        for name, data in cases.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                charts.validate_png(data)
        no_idat = charts.PNG_SIGNATURE + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + \
            _chunk(b"IEND", b"")
        with self.assertRaises(ValueError):
            charts.validate_png(no_idat)
        first_not_ihdr = charts.PNG_SIGNATURE + _chunk(b"IDAT", b"") + _chunk(b"IEND", b"")
        with self.assertRaises(ValueError):
            charts.validate_png(first_not_ihdr)

    def test_limits_accept_maximum_and_reject_large_file(self):
        self.assertEqual(charts.validate_png(make_png(ihdr_size=(2400, 1800))), (2400, 1800))
        big = make_png(extra=((b"pHYs", b"\x00" * (2 << 20)),))
        with self.assertRaises(ValueError):
            charts.validate_png(big)


class ChartDataTests(unittest.TestCase):
    def test_sample_is_valid(self):
        doc = sample_doc()
        self.assertIs(charts.validate_chart_data(doc), doc)

    def test_minimal_empty_document_is_valid(self):
        doc = sample_doc()
        doc["scorecard"] = {"products": [], "signals": [], "cells": {}, "verdicts": {}}
        doc.update(ladder={}, dag={}, burnup={})
        doc["lanes"] = {"window_start": "2026-09-24T05:17:00Z", "window_end": "2026-10-01T05:17:00Z",
                        "order": [], "enabled": {}, "intervals": []}
        charts.validate_chart_data(doc)

    def mutate(self, change):
        doc = sample_doc()
        change(doc)
        with self.assertRaises(ValueError):
            charts.validate_chart_data(doc)

    def test_malformed_documents_are_rejected(self):
        def setp(path, value):
            def change(doc):
                target = doc
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
            return change

        def delp(path):
            def change(doc):
                target = doc
                for key in path[:-1]:
                    target = target[key]
                del target[path[-1]]
            return change

        cases = {
            "not a dict": lambda d: d.clear() or d.update(x=1),
            "schema": setp(["schema"], "AIOPS_INSPECT_CHARTS_V2"),
            "unknown top key": setp(["extra"], 1),
            "missing lanes": delp(["lanes"]),
            "snapshot": setp(["snapshot"], "0123456789AB"),
            "stage": setp(["stage"], "PROD"),
            "generated_at": setp(["generated_at"], "2026-10-01 05:17:00"),
            "bad date": setp(["generated_at"], "2026-02-30T00:00:00Z"),
            "product charset": setp(["scorecard", "products"], ["kix protocol"]),
            "mathtext product": setp(["scorecard", "products"], ["$x$"]),
            "duplicate product": setp(["scorecard", "products"], ["ZARI", "ZARI"]),
            "unknown signal": setp(["scorecard", "signals"], ["S10"]),
            "level": setp(["scorecard", "cells", "ZARI", "S1"], "DIVERGED"),
            "cell outside signals": lambda d: d["scorecard"].update(signals=["S0"]),
            "cell product unknown": setp(["scorecard", "cells", "OTHER"], {}),
            "missing verdict": delp(["scorecard", "verdicts", "ZARI"]),
            "previous level": setp(["scorecard", "previous", "ZARI"], "BAD"),
            "basis": setp(["scorecard", "basis", "ZARI", "S6"], "SLACK"),
            "bool count": setp(["ladder", "ZARI", "planned"], True),
            "negative count": setp(["ladder", "ZARI", "orphans"], -1),
            "float count": setp(["ladder", "ZARI", "done"], 1.5),
            "checks exceed done": setp(["ladder", "kix-protocol", "merge_checks", "FAIL"], 5),
            "check key": setp(["ladder", "ZARI", "merge_checks", "N/A"], 1),
            "deployed": setp(["ladder", "ZARI", "deployed"], "DEPLOYED"),
            "ladder missing key": delp(["ladder", "ZARI", "pending_plan_prs"]),
            "dag dup id": setp(["dag", "ZARI", "nodes", 1, "id"], "A"),
            "dag overlap": setp(["dag", "ZARI", "nodes", 2, "row"], 0),
            "dag stage": setp(["dag", "ZARI", "nodes", 0, "stage"], "WAITING"),
            "dag edge unknown": setp(["dag", "ZARI", "edges"], [["A", "Z"]]),
            "dag self edge": setp(["dag", "ZARI", "edges"], [["A", "A"]]),
            "dag edge shape": setp(["dag", "ZARI", "edges"], [["A", "B", "C"]]),
            "dag prefix": setp(["dag", "ZARI", "prefix"], "zari"),
            "dag hours nan": setp(["dag", "kix-protocol", "nodes", 1, "blocked_hours"], float("nan")),
            "dag node key": setp(["dag", "ZARI", "nodes", 0, "title"], "free text"),
            "node id charset": setp(["dag", "ZARI", "nodes", 0, "id"], "a b"),
            "burnup time": setp(["burnup", "ZARI", 0, "t"], "yesterday"),
            "burnup shape": setp(["burnup", "ZARI"], {"t": 1}),
            "lane unknown": setp(["lanes", "intervals", 0, "lane"], "NOLANE"),
            "order unknown": setp(["lanes", "order"], ["DEVIN", "NOLANE"]),
            "interval backwards": setp(["lanes", "intervals", 0, "end"], "2026-09-24T00:00:00Z"),
            "task charset": setp(["lanes", "intervals", 0, "task"], "zari-a"),
            "window backwards": setp(["lanes", "window_end"], "2026-09-01T00:00:00Z"),
            "window huge": setp(["lanes", "window_start"], "2025-01-01T00:00:00Z"),
            "enabled type": setp(["lanes", "enabled", "DEVIN"], "yes"),
            "idle backwards": setp(["lanes", "idle_waiting", 0, "end"], "2026-09-26T00:00:00Z"),
        }
        for name, change in cases.items():
            with self.subTest(name=name):
                self.mutate(change)

    def test_caps(self):
        self.mutate(lambda d: d["scorecard"].update(products=[f"p{i}" for i in range(13)]))
        self.mutate(lambda d: d["lanes"].update(intervals=[d["lanes"]["intervals"][0]] * 5001))

    def test_error_text_does_not_echo_values(self):
        doc = sample_doc()
        doc["xoxb-secret-looking-key"] = 1
        with self.assertRaises(ValueError) as ctx:
            charts.validate_chart_data(doc)
        self.assertNotIn("xoxb", str(ctx.exception))

    def test_load_rejects_nan_symlink_and_oversize(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp, "d.json")
            good.write_text(json.dumps(sample_doc()), encoding="utf-8")
            self.assertEqual(charts.load_chart_data(good)["snapshot"], "0123456789ab")
            nan = Path(tmp, "nan.json")
            nan.write_text(json.dumps(sample_doc()).replace('"blocked_hours": 30', '"blocked_hours": NaN'),
                           encoding="utf-8")
            with self.assertRaises(ValueError):
                charts.load_chart_data(nan)
            link = Path(tmp, "link.json")
            link.symlink_to(good)
            with self.assertRaises(ValueError):
                charts.load_chart_data(link)
            big = Path(tmp, "big.json")
            big.write_bytes(b" " * (charts.MAX_DATA_BYTES + 1))
            with self.assertRaises(ValueError):
                charts.load_chart_data(big)

    def test_dag_choice_and_names(self):
        doc = sample_doc()
        self.assertEqual(charts.dag_choices(doc), ["ZARI", "kix-protocol"])
        self.assertEqual(charts.dag_file_name("ZARI", doc["dag"]["ZARI"]), "c2_dag_zari.png")
        self.assertEqual(charts.dag_file_name("BeautifulMind-JT/maeum-gyeol", {}), "c2_dag_maeum-gyeol.png")
        doc["dag"]["kix-protocol"]["nodes"][0]["stage"] = "PLANNED"
        doc["dag"]["maeum-gyeol"]["nodes"].append({"id": "N2", "stage": "PLANNED", "depth": 1, "row": 0})
        doc["dag"]["maeum-gyeol"]["nodes"].append({"id": "N3", "stage": "PLANNED", "depth": 2, "row": 0})
        # kix-protocol 3 open, ZARI 3 open, maeum-gyeol 3 open: ties break by name, at most two.
        self.assertEqual(charts.dag_choices(doc), ["ZARI", "kix-protocol"])

    def test_change_words(self):
        self.assertEqual(charts._change_words("WATCH", None), "첫 점검")
        self.assertEqual(charts._change_words("AT_RISK", "WATCH"), "악화 (전: 주의)")
        self.assertEqual(charts._change_words("ON_TRACK", "AT_RISK"), "개선 (전: 위험)")
        self.assertEqual(charts._change_words("WATCH", "WATCH"), "변화 없음 (전: 주의)")
        self.assertEqual(charts._change_words("PAUSED", "WATCH"), "바뀜 (전: 주의)")

    def test_every_level_and_stage_has_a_word(self):
        for level in charts.LEVELS:
            self.assertTrue(charts.LEVEL_WORDS[level])
            self.assertIn(level, charts.LEVEL_COLORS)
        for stage in charts.DAG_STAGES:
            self.assertTrue(charts.STAGE_WORDS[stage])
            self.assertIn(stage, charts.STAGE_COLORS)


class ModuleShapeTests(unittest.TestCase):
    def test_self_contained_and_lazy(self):
        source = MODULE.read_text(encoding="utf-8")
        self.assertNotIn("import control_plane", source)
        self.assertNotIn("from control_plane", source)
        for word in ("urllib", "socket", "http.client", "requests", "subprocess"):
            self.assertNotIn(f"import {word}", source)
        probe = ("import sys; sys.path.insert(0, sys.argv[1]); import control_plane_inspect_charts; "
                 "print('matplotlib' in sys.modules)")
        out = subprocess.run([sys.executable, "-I", "-c", probe, str(MODULE.parent)], capture_output=True, text=True,
                             timeout=60, check=True)
        self.assertEqual(out.stdout.strip(), "False")

    def test_footer_and_fixed_texts(self):
        self.assertEqual(charts.FOOTER.format(snapshot="0123456789ab"),
                         "사실=기계 집계 · 자문 전용 · 게이트 아님 · snapshot 0123456789ab")
        self.assertEqual(charts.LANE_CAPTION, "DEVIN에 먼저 몰리는 것은 정상(고정 순서)")
        self.assertEqual(charts.NOT_RECORDED_TEXT, "배포·실사용 검증: 기록 없음")
        self.assertEqual(charts.GH_TEXT_NOTE, "(GitHub 글 기준)")
        self.assertEqual((charts.LEVEL_COLORS["ON_TRACK"], charts.LEVEL_COLORS["WATCH"],
                          charts.LEVEL_COLORS["AT_RISK"]), ("#0ca30c", "#fab219", "#d03b3b"))
        self.assertEqual(charts.LANE_COLORS, {"DEVIN": "#2a78d6", "GROK_BUILD": "#eb6834", "GLM": "#1baf7a",
                                              "CURSOR": "#4a3aa7"})


class CommandModeTests(unittest.TestCase):
    def run_main(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = charts.main(argv)
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        return code, json.loads(lines[0])

    def test_argument_errors_print_one_json_line(self):
        for argv in ([], ["render"], ["render", "--data", "x"], ["draw", "--data", "x", "--out", "y"]):
            with self.subTest(argv=argv):
                buf = io.StringIO()
                with redirect_stdout(buf), self.assertRaises(SystemExit) as ctx:
                    charts.main(argv)
                self.assertEqual(ctx.exception.code, 2)
                self.assertEqual(json.loads(buf.getvalue()), {"status": "ERROR", "reason": "ARGS"})

    def test_bad_data_and_bad_out_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp, "d.json")
            data.write_text('{"schema": "nope"}', encoding="utf-8")
            code, out = self.run_main(["render", "--data", str(data), "--out", tmp])
            self.assertEqual((code, out["status"], out["reason"]), (1, "ERROR", "CHART_DATA"))
            code, out = self.run_main(["render", "--data", str(data), "--out", str(Path(tmp, "missing"))])
            self.assertEqual((code, out["reason"]), (1, "IO"))
            not_dir = Path(tmp, "file")
            not_dir.write_text("x", encoding="utf-8")
            code, out = self.run_main(["render", "--data", str(data), "--out", str(not_dir)])
            self.assertEqual((code, out["reason"]), (1, "OUT_DIR"))
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["d.json", "file"])

    def test_main_restores_environment(self):
        saved = os.environ.pop("MPLCONFIGDIR", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                data = Path(tmp, "d.json")
                data.write_text('{"schema": "nope"}', encoding="utf-8")
                self.run_main(["render", "--data", str(data), "--out", tmp])
                self.assertIsNone(os.environ.get("MPLCONFIGDIR"))
                os.environ["MPLCONFIGDIR"] = tmp  # a caller's own setting is left alone
                self.run_main(["render", "--data", str(data), "--out", tmp])
                self.assertEqual(os.environ.get("MPLCONFIGDIR"), tmp)
                self.assertTrue(Path(tmp).is_dir())
        finally:
            os.environ.pop("MPLCONFIGDIR", None)
            if saved is not None:
                os.environ["MPLCONFIGDIR"] = saved

    @unittest.skipIf(HAS_MPL, "matplotlib is installed here")
    def test_missing_matplotlib_is_an_error_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp, "d.json")
            data.write_text(json.dumps(sample_doc()), encoding="utf-8")
            code, out = self.run_main(["render", "--data", str(data), "--out", tmp])
            self.assertEqual((code, out), (1, {"status": "ERROR", "reason": "MATPLOTLIB_MISSING"}))
            self.assertEqual(charts.availability(), {"matplotlib": False, "font": None})


@unittest.skipUnless(HAS_MPL, "matplotlib not installed")
class RenderTests(unittest.TestCase):
    def write_doc(self, tmp: str, doc: dict) -> Path:
        path = Path(tmp, "chart.json")
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return path

    def test_font_missing_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self.write_doc(tmp, sample_doc())
            not_font = Path(tmp, "not-a-font.ttf")
            not_font.write_bytes(b"\x00" * 64)
            out = Path(tmp, "out")
            out.mkdir()
            self.assertEqual(charts.render(data, out, font_path=str(not_font)), {"status": "FONT_MISSING"})
            self.assertEqual(list(out.iterdir()), [])
            self.assertIsNone(charts.find_font(str(Path(tmp, "absent.ttf"))))

    @unittest.skipUnless(korean_font(), "no Korean font file on this machine")
    def test_render_all_charts_valid_and_deterministic(self):
        font = korean_font()
        with tempfile.TemporaryDirectory() as tmp:
            data = self.write_doc(tmp, sample_doc())
            first, second = Path(tmp, "a"), Path(tmp, "b")
            first.mkdir()
            second.mkdir()
            result = charts.render(data, first, font_path=font)
            self.assertEqual(result["status"], "OK")
            names = [p["name"] for p in result["pngs"]]
            self.assertEqual(names, ["c5_scorecard.png", "c1_ladder.png", "c2_dag_zari.png", "c2_dag_kixp.png",
                                     "c3_burnup.png", "c4_lanes.png"])
            manifest = json.loads(Path(first, "render.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest, {"pngs": result["pngs"], "font": result["font"]})
            for entry in result["pngs"]:
                blob = Path(first, entry["name"]).read_bytes()
                self.assertEqual(hashlib.sha256(blob).hexdigest(), entry["sha256"])
                self.assertEqual(charts.validate_png(blob), (entry["w"], entry["h"]))
                for kind in (b"tEXt", b"iTXt", b"zTXt", b"tIME"):
                    self.assertNotIn(kind, blob)
                self.assertLessEqual(len(blob), charts.MAX_PNG_BYTES)
            self.assertEqual([(p["w"], p["h"]) for p in result["pngs"]],
                             [(1800, 900), (1800, 900), (1800, 1050), (1800, 1050), (1800, 1050), (1800, 900)])
            again = charts.render(data, second, font_path=font)
            self.assertEqual(again["pngs"], result["pngs"])
            self.assertEqual(sorted(p.name for p in first.iterdir()), sorted(names + ["render.json"]))

    @unittest.skipUnless(korean_font(), "no Korean font file on this machine")
    def test_render_empty_and_large_documents(self):
        font = korean_font()
        empty = sample_doc()
        empty["scorecard"] = {"products": [], "signals": [], "cells": {}, "verdicts": {}}
        empty.update(ladder={}, dag={}, burnup={})
        empty["lanes"].update(intervals=[], idle_waiting=[])
        large = sample_doc()
        nodes = [{"id": f"N{k:03d}", "stage": charts.DAG_STAGES[k % 7], "depth": k // 15, "row": k % 15,
                  "critical": k % 15 == 0, "blocked": k % 11 == 0, "blocked_hours": 99.5,
                  "basis_blocked": "GH_TEXT"} for k in range(300)]
        edges = [[f"N{k - 15:03d}", f"N{k:03d}"] for k in range(15, 300)]
        large["dag"] = {"big": {"nodes": nodes, "edges": edges}}
        large["scorecard"]["products"] += [f"product-{i}" for i in range(9)]
        large["scorecard"]["verdicts"].update({f"product-{i}": "UNKNOWN" for i in range(9)})
        with tempfile.TemporaryDirectory() as tmp:
            for name, doc, expected in (("empty", empty, 4), ("large", large, 5)):
                out = Path(tmp, name)
                out.mkdir()
                result = charts.render(self.write_doc(tmp, doc), out, font_path=font)
                self.assertEqual(len(result["pngs"]), expected, name)
                for entry in result["pngs"]:
                    charts.validate_png(Path(out, entry["name"]).read_bytes())

    @unittest.skipUnless(korean_font(), "no Korean font file on this machine")
    def test_command_mode_subprocess_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = self.write_doc(tmp, sample_doc())
            out = Path(tmp, "out")
            out.mkdir()
            proc = subprocess.run([sys.executable, "-I", str(MODULE), "render", "--data", str(data), "--out",
                                   str(out), "--font", korean_font()], capture_output=True, text=True, timeout=300,
                                  env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": tmp})
            self.assertEqual(proc.returncode, 0, proc.stdout)
            lines = proc.stdout.splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["status"], "OK")


if __name__ == "__main__":
    unittest.main()
