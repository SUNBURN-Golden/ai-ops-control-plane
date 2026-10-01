#!/usr/bin/python3 -I
"""Program inspector (감리) template charts, User decision M7, stage 1.

Self-contained on purpose: the tick runs this file alone as the render account
(`python3 -I control_plane_inspect_charts.py render --data F --out D`), so it
imports no other inspector module. It draws five fixed charts from the host-built
AIOPS_INSPECT_CHARTS_V1 document, never trusts that document (validate_chart_data),
imports matplotlib lazily, and writes deterministic PNGs whose text chunks are
stripped and whose size is checked. No network, no secrets, no model code.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import tempfile
import zlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCHEMA = "AIOPS_INSPECT_CHARTS_V1"
DPI = 150
FIG_SIZES = {"c5": (12.0, 6.0), "c1": (12.0, 6.0), "c2": (12.0, 7.0), "c3": (12.0, 7.0), "c4": (12.0, 6.0)}
MAX_WIDTH = 2400
MAX_HEIGHT = 1800
MAX_PNG_BYTES = 2 << 20
MAX_DATA_BYTES = 8 << 20
MAX_DAG_CHARTS = 2
KST = timezone(timedelta(hours=9))

FONT_FAMILIES = ("Noto Sans CJK KR", "Noto Sans KR", "NanumGothic")
FONT_PROBE = "감리정상주의위험완료계획"

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
WHITE = "#ffffff"
LANE_ORDER = ("DEVIN", "GROK_BUILD", "GLM", "CURSOR")
LANE_COLORS = {"DEVIN": "#2a78d6", "GROK_BUILD": "#eb6834", "GLM": "#1baf7a", "CURSOR": "#4a3aa7"}
LEVELS = ("ON_TRACK", "WATCH", "AT_RISK", "PAUSED", "NOT_CONFIGURED", "UNKNOWN")
LEVEL_RANK = {"ON_TRACK": 0, "WATCH": 1, "AT_RISK": 2}
LEVEL_COLORS = {"ON_TRACK": "#0ca30c", "WATCH": "#fab219", "AT_RISK": "#d03b3b",
                "PAUSED": "#c3c2b7", "NOT_CONFIGURED": "#c3c2b7", "UNKNOWN": "#c3c2b7"}
LEVEL_WORDS = {"ON_TRACK": "정상", "WATCH": "주의", "AT_RISK": "위험",
               "PAUSED": "멈춤", "NOT_CONFIGURED": "미설정", "UNKNOWN": "확인 불가"}
SIGNALS = ("S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9")
SIGNAL_NAMES = {"S0": "기록 정합", "S1": "계획 대비", "S2": "반복 실패", "S3": "작은 예외", "S4": "계약 정합",
                "S5": "증거 약화", "S6": "정체", "S7": "레인", "S8": "결정 연속", "S9": "CURSOR"}
# S3 is always GH_TEXT-derived (§3.5), so its cells are always hatched.
GH_TEXT_SIGNALS = ("S3",)
STAGES = ("PLANNED", "MATERIALIZING", "NOT_STARTED", "IN_PROGRESS", "DELIVERED", "DONE")
DAG_STAGES = STAGES + ("UNKNOWN",)
STAGE_WORDS = {"PLANNED": "계획", "MATERIALIZING": "생성 중", "NOT_STARTED": "시작 전", "IN_PROGRESS": "진행 중",
               "DELIVERED": "전달", "DONE": "완료", "UNKNOWN": "확인 불가"}
# Ordinal one-hue ramp (blue 250..650) for the stages before DONE; DONE is the good status colour.
STAGE_COLORS = {"PLANNED": "#86b6ef", "MATERIALIZING": "#5598e7", "NOT_STARTED": "#2a78d6",
                "IN_PROGRESS": "#1c5cab", "DELIVERED": "#104281", "DONE": "#0ca30c", "UNKNOWN": "#c3c2b7"}
LADDER_KEYS = ("planned", "materializing", "not_started", "in_progress", "delivered")
DONE_PARTS = (("PASS", "완료·검사 통과", "#0ca30c"), ("FAIL", "완료·검사 실패", "#d03b3b"),
              ("PENDING", "완료·검사 대기", "#fab219"), ("REST", "완료·검사 미설정", "#006300"))
MERGE_CHECK_KEYS = ("PASS", "FAIL", "PENDING", "NOT_CONFIGURED")
BASES = ("HOST", "GH_SYSTEM", "SHA_CONTENT", "GH_TEXT")
GH_TEXT_NOTE = "(GitHub 글 기준)"
GH_HATCH = "///"
IDLE_HATCH = "\\\\\\"
FOOTER = "사실=기계 집계 · 자문 전용 · 게이트 아님 · snapshot {snapshot}"
LANE_CAPTION = "DEVIN에 먼저 몰리는 것은 정상(고정 순서)"
NOT_RECORDED_TEXT = "배포·실사용 검증: 기록 없음"

PRODUCT_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}(/[A-Za-z0-9_.-]{1,100})?")
NODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
TASK_RE = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,99}")
LANE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,31}")
WORD_RE = re.compile(r"[A-Z][A-Z_]{0,31}")
PREFIX_RE = re.compile(r"[A-Z]{4}")
SNAPSHOT_RE = re.compile(r"[0-9a-f]{12}")
TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_CRITICAL = (b"IHDR", b"PLTE", b"IDAT", b"IEND")
# Allowlist: every other ancillary chunk (tEXt, iTXt, zTXt, tIME, eXIf, ...) is dropped.
PNG_KEEP = PNG_CRITICAL + (b"tRNS", b"pHYs", b"sRGB", b"gAMA", b"cHRM", b"bKGD", b"sBIT")
LIMITS = {"products": 12, "dag_nodes": 300, "dag_edges": 1200, "burnup_points": 2000,
          "intervals": 5000, "idle_spans": 1000, "depth": 200, "row": 500, "count": 100000}


class ChartError(Exception):
    """A render failure with an UPPER_SNAKE reason code; str() is the reason."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail

    def __str__(self) -> str:
        return self.reason


# ---------------------------------------------------------------- validation

def _fail(where: str, what: str) -> None:
    raise ValueError(f"{where}: {what}")


def _dict(value: Any, where: str, required: Sequence[str] = (), optional: Sequence[str] = ()) -> Dict[str, Any]:
    if not isinstance(value, dict):
        _fail(where, "must be an object")
    missing = [k for k in required if k not in value]
    if missing:
        _fail(where, f"missing {missing[0]}")
    allowed = set(required) | set(optional)
    extra = sorted(k for k in value if k not in allowed)
    if extra:
        _fail(where, "unknown key")
    return value


def _list(value: Any, where: str, cap: int) -> List[Any]:
    if not isinstance(value, list):
        _fail(where, "must be a list")
    if len(value) > cap:
        _fail(where, f"more than {cap} items")
    return value


def _str(value: Any, where: str, pattern: "re.Pattern[str]") -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        _fail(where, "bad value")
    return value


def _int(value: Any, where: str, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > high:
        _fail(where, f"must be an integer in 0..{high}")
    return value


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        _fail(where, "must be true or false")
    return value


def _choice(value: Any, where: str, choices: Sequence[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        _fail(where, "bad value")
    return value


def parse_time(value: Any, where: str) -> datetime:
    """Parse an ISO UTC `...Z` timestamp from the chart data; anything else is a ValueError."""
    if not isinstance(value, str) or not TIME_RE.fullmatch(value):
        _fail(where, "must be an ISO UTC time ending in Z")
    text = value[:-1]
    fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in text else "%Y-%m-%dT%H:%M:%S"
    try:
        return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
    except ValueError:
        _fail(where, "not a real time")
    raise AssertionError("unreachable")


def _product_map(value: Any, where: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        _fail(where, "must be an object")
    mapping: Dict[str, Any] = value
    if len(mapping) > LIMITS["products"]:
        _fail(where, f"more than {LIMITS['products']} products")
    for key in mapping:
        _str(key, f"{where} key", PRODUCT_RE)
    return mapping


def _validate_scorecard(sc: Any) -> None:
    # CONTRACT NOTE: optional "basis" {product: {signal: HOST|GH_SYSTEM|SHA_CONTENT|GH_TEXT}} marks GH_TEXT cells.
    sc = _dict(sc, "scorecard", ("products", "signals", "cells", "verdicts"), ("previous", "basis"))
    products = _list(sc["products"], "scorecard.products", LIMITS["products"])
    for i, name in enumerate(products):
        _str(name, f"scorecard.products[{i}]", PRODUCT_RE)
    if len(set(products)) != len(products):
        _fail("scorecard.products", "duplicate product")
    signals = _list(sc["signals"], "scorecard.signals", len(SIGNALS))
    for i, sig in enumerate(signals):
        _choice(sig, f"scorecard.signals[{i}]", SIGNALS)
    if len(set(signals)) != len(signals):
        _fail("scorecard.signals", "duplicate signal")
    known = set(products)
    for field in ("cells", "verdicts", "previous", "basis"):
        if field not in sc:
            continue
        mapping = _product_map(sc[field], f"scorecard.{field}")
        for product, value in mapping.items():
            if product not in known:
                _fail(f"scorecard.{field}", "product not in scorecard.products")
            where = f"scorecard.{field}[{product}]"
            if field == "verdicts":
                _choice(value, where, LEVELS)
            elif field == "previous":
                if value is not None:
                    _choice(value, where, LEVELS)
            else:
                cells = _dict(value, where, optional=signals)
                for sig, level in cells.items():
                    _choice(level, f"{where}.{sig}", LEVELS if field == "cells" else BASES)
    missing = [p for p in products if p not in sc["verdicts"]]
    if missing:
        _fail("scorecard.verdicts", "a product has no verdict")


def _validate_ladder(ladder: Any) -> None:
    for product, row in _product_map(ladder, "ladder").items():
        where = f"ladder[{product}]"
        row = _dict(row, where, LADDER_KEYS + ("done", "merge_checks", "deployed", "verified", "orphans",
                                               "pending_plan_prs"))
        for key in LADDER_KEYS + ("done", "orphans", "pending_plan_prs"):
            _int(row[key], f"{where}.{key}", LIMITS["count"])
        checks = _dict(row["merge_checks"], f"{where}.merge_checks", optional=MERGE_CHECK_KEYS)
        for key, value in checks.items():
            _int(value, f"{where}.merge_checks.{key}", LIMITS["count"])
        if sum(checks.get(k, 0) for k in ("PASS", "FAIL", "PENDING")) > row["done"]:
            _fail(f"{where}.merge_checks", "PASS+FAIL+PENDING exceeds done")
        # CONTRACT NOTE: PR1 has no central deploy/verify record, so only NOT_RECORDED is accepted.
        _choice(row["deployed"], f"{where}.deployed", ("NOT_RECORDED",))
        _choice(row["verified"], f"{where}.verified", ("NOT_RECORDED",))


def _validate_dag(dag: Any) -> None:
    for product, graph in _product_map(dag, "dag").items():
        where = f"dag[{product}]"
        # CONTRACT NOTE: optional "prefix" (^[A-Z]{4}$) names the file c2_dag_<prefix>.png.
        graph = _dict(graph, where, ("nodes", "edges"), ("prefix",))
        if "prefix" in graph:
            _str(graph["prefix"], f"{where}.prefix", PREFIX_RE)
        nodes = _list(graph["nodes"], f"{where}.nodes", LIMITS["dag_nodes"])
        ids = set()
        places = set()
        for i, node in enumerate(nodes):
            nw = f"{where}.nodes[{i}]"
            node = _dict(node, nw, ("id", "stage", "depth", "row"),
                         ("blocked", "critical", "blocked_hours", "basis_blocked"))
            ident = _str(node["id"], f"{nw}.id", NODE_RE)
            _choice(node["stage"], f"{nw}.stage", DAG_STAGES)
            place = (_int(node["depth"], f"{nw}.depth", LIMITS["depth"]), _int(node["row"], f"{nw}.row", LIMITS["row"]))
            if ident in ids:
                _fail(nw, "duplicate id")
            if place in places:
                _fail(nw, "two nodes at one (depth, row)")
            ids.add(ident)
            places.add(place)
            for key in ("blocked", "critical"):
                if key in node:
                    _bool(node[key], f"{nw}.{key}")
            hours = node.get("blocked_hours")
            if hours is not None and (isinstance(hours, bool) or not isinstance(hours, (int, float))
                                      or not math.isfinite(hours) or hours < 0 or hours > 1e6):
                _fail(f"{nw}.blocked_hours", "must be a non-negative number or null")
            if node.get("basis_blocked") is not None:
                _choice(node["basis_blocked"], f"{nw}.basis_blocked", BASES)
        edges = _list(graph["edges"], f"{where}.edges", LIMITS["dag_edges"])
        for i, edge in enumerate(edges):
            if (not isinstance(edge, list) or len(edge) != 2 or edge[0] not in ids or edge[1] not in ids
                    or edge[0] == edge[1]):
                _fail(f"{where}.edges[{i}]", "must be [from_id, to_id] of two known nodes")


def _validate_burnup(burnup: Any) -> None:
    for product, points in _product_map(burnup, "burnup").items():
        where = f"burnup[{product}]"
        for i, point in enumerate(_list(points, where, LIMITS["burnup_points"])):
            point = _dict(point, f"{where}[{i}]", ("t", "done", "planned"))
            parse_time(point["t"], f"{where}[{i}].t")
            _int(point["done"], f"{where}[{i}].done", LIMITS["count"])
            _int(point["planned"], f"{where}[{i}].planned", LIMITS["count"])


def _validate_lanes(lanes: Any) -> None:
    lanes = _dict(lanes, "lanes", ("window_start", "window_end", "order", "enabled", "intervals"), ("idle_waiting",))
    start = parse_time(lanes["window_start"], "lanes.window_start")
    end = parse_time(lanes["window_end"], "lanes.window_end")
    if not start < end or end - start > timedelta(days=62):
        _fail("lanes", "window must be increasing and at most 62 days")
    order = _list(lanes["order"], "lanes.order", len(LANE_ORDER))
    for i, lane in enumerate(order):
        _choice(lane, f"lanes.order[{i}]", LANE_ORDER)
    if len(set(order)) != len(order):
        _fail("lanes.order", "duplicate lane")
    enabled = _dict(lanes["enabled"], "lanes.enabled", optional=LANE_ORDER)
    for lane, value in enabled.items():
        _bool(value, f"lanes.enabled.{lane}")
    for i, item in enumerate(_list(lanes["intervals"], "lanes.intervals", LIMITS["intervals"])):
        where = f"lanes.intervals[{i}]"
        item = _dict(item, where, ("lane", "start", "end", "role", "task", "state"))
        _choice(item["lane"], f"{where}.lane", LANE_ORDER)
        began = parse_time(item["start"], f"{where}.start")
        if item["end"] is not None and parse_time(item["end"], f"{where}.end") < began:
            _fail(where, "end before start")
        _str(item["role"], f"{where}.role", WORD_RE)
        _str(item["task"], f"{where}.task", TASK_RE)
        _str(item["state"], f"{where}.state", WORD_RE)
    for i, span in enumerate(_list(lanes.get("idle_waiting", []), "lanes.idle_waiting", LIMITS["idle_spans"])):
        span = _dict(span, f"lanes.idle_waiting[{i}]", ("start", "end"))
        if parse_time(span["end"], f"lanes.idle_waiting[{i}].end") < parse_time(span["start"],
                                                                              f"lanes.idle_waiting[{i}].start"):
            _fail(f"lanes.idle_waiting[{i}]", "end before start")


def validate_chart_data(doc: Any) -> Dict[str, Any]:
    """Check the AIOPS_INSPECT_CHARTS_V1 shape strictly; raise ValueError on anything malformed."""
    doc = _dict(doc, "chart data", ("schema", "generated_at", "snapshot", "stage", "scorecard", "ladder", "dag",
                                    "burnup", "lanes"))
    if doc["schema"] != SCHEMA:
        _fail("schema", f"must be {SCHEMA}")
    parse_time(doc["generated_at"], "generated_at")
    _str(doc["snapshot"], "snapshot", SNAPSHOT_RE)
    _choice(doc["stage"], "stage", ("DRY", "LIVE"))
    _validate_scorecard(doc["scorecard"])
    _validate_ladder(doc["ladder"])
    _validate_dag(doc["dag"])
    _validate_burnup(doc["burnup"])
    _validate_lanes(doc["lanes"])
    return doc


def _reject_constant(name: str) -> Any:
    raise ValueError(f"JSON constant {name} is not allowed")


def load_chart_data(path: Path) -> Dict[str, Any]:
    """Read and validate a chart data file (regular file, size-capped, strict JSON)."""
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("chart data must be a regular file")
    if info.st_size > MAX_DATA_BYTES:
        raise ValueError("chart data too large")
    with open(path, "rb") as handle:
        raw = handle.read(MAX_DATA_BYTES + 1)
    if len(raw) > MAX_DATA_BYTES:
        raise ValueError("chart data too large")
    doc = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    return validate_chart_data(doc)


# ---------------------------------------------------------------- PNG hygiene

def _png_chunks(data: bytes) -> List[Tuple[bytes, bytes, bytes]]:
    if not isinstance(data, (bytes, bytearray)) or data[:8] != PNG_SIGNATURE:
        raise ValueError("PNG signature missing")
    chunks: List[Tuple[bytes, bytes, bytes]] = []
    pos = 8
    while True:
        if pos + 12 > len(data):
            raise ValueError("PNG truncated")
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = bytes(data[pos + 4:pos + 8])
        if length > 0x7FFFFFFF or pos + 12 + length > len(data):
            raise ValueError("PNG chunk length out of range")
        body = bytes(data[pos + 8:pos + 8 + length])
        crc = bytes(data[pos + 8 + length:pos + 12 + length])
        if not re.fullmatch(rb"[A-Za-z]{4}", kind):
            raise ValueError("PNG chunk type invalid")
        if struct.unpack(">I", crc)[0] != zlib.crc32(kind + body) & 0xFFFFFFFF:
            raise ValueError("PNG chunk CRC mismatch")
        chunks.append((kind, body, crc))
        pos += 12 + length
        if kind == b"IEND":
            break
    if pos != len(data):
        raise ValueError("PNG has bytes after IEND")
    if not chunks or chunks[0][0] != b"IHDR" or len(chunks[0][1]) != 13:
        raise ValueError("PNG must start with a 13-byte IHDR")
    if not any(kind == b"IDAT" for kind, _, _ in chunks):
        raise ValueError("PNG has no IDAT")
    return chunks


def filter_png(data: bytes) -> bytes:
    """Return the PNG with every non-allowlisted ancillary chunk (tEXt, iTXt, zTXt, tIME, ...) removed."""
    out = [PNG_SIGNATURE]
    for kind, body, crc in _png_chunks(data):
        if kind in PNG_KEEP:
            out.append(struct.pack(">I", len(body)) + kind + body + crc)
        elif kind[0:1].isupper():
            raise ValueError("PNG has an unknown critical chunk")
    return b"".join(out)


def validate_png(data: bytes) -> Tuple[int, int]:
    """Check signature, structure, IHDR size limits, file size and absence of text chunks; return (w, h)."""
    if len(data) > MAX_PNG_BYTES:
        raise ValueError("PNG larger than 2 MiB")
    chunks = _png_chunks(data)
    for kind, _, _ in chunks:
        if kind not in PNG_KEEP:
            raise ValueError(f"PNG chunk {kind.decode('ascii')} not allowed")
    width, height = struct.unpack(">II", chunks[0][1][:8])
    if not (0 < width <= MAX_WIDTH and 0 < height <= MAX_HEIGHT):
        raise ValueError("PNG dimensions out of range")
    return width, height


# ---------------------------------------------------------------- matplotlib and font

def _matplotlib() -> Any:
    try:
        import matplotlib
    except ImportError as exc:  # pragma: no cover - depends on the host
        raise ChartError("MATPLOTLIB_MISSING") from exc
    matplotlib.use("Agg")
    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
    return matplotlib


def _font_covers(path: str) -> bool:
    try:
        from matplotlib.ft2font import FT2Font
        face = FT2Font(path)
        return all(face.get_char_index(ord(ch)) for ch in FONT_PROBE)
    except Exception:
        return False


def find_font(font_path: Optional[str] = None) -> Optional[Dict[str, str]]:
    """Return {"family", "path"} of a Korean font, or None. An explicit font_path must be a covering regular file."""
    _matplotlib()
    from matplotlib import font_manager
    if font_path:
        try:
            info = os.stat(font_path)
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode) or not _font_covers(font_path):
            return None
        font_manager.fontManager.addfont(font_path)
        family = font_manager.FontProperties(fname=font_path).get_name()
        return {"family": family, "path": font_path}
    candidates = []
    for entry in font_manager.fontManager.ttflist:
        for rank, wanted in enumerate(FONT_FAMILIES):
            if wanted in entry.name:
                regular = 0 if (entry.style == "normal" and str(entry.weight) in ("400", "normal", "regular")) else 1
                candidates.append((rank, regular, entry.fname, entry.name))
                break
    for _, _, fname, name in sorted(candidates):
        if _font_covers(fname):
            return {"family": name, "path": fname}
    return None


def availability(font_path: Optional[str] = None) -> Dict[str, Any]:
    """For preflight: is matplotlib importable and a Korean font present? Never raises."""
    try:
        font = find_font(font_path)
    except ChartError:
        return {"matplotlib": False, "font": None}
    except Exception:
        return {"matplotlib": True, "font": None}
    return {"matplotlib": True, "font": font["family"] if font else None}


def _rc(family: str) -> Dict[str, Any]:
    params = {
        "font.family": [family], "font.sans-serif": [family], "font.size": 10.0,
        "axes.unicode_minus": False, "text.usetex": False, "figure.dpi": DPI, "savefig.dpi": DPI,
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "axes.edgecolor": GRID, "axes.linewidth": 0.8, "axes.labelcolor": INK_2, "axes.titlecolor": INK,
        "xtick.color": MUTED, "ytick.color": MUTED, "xtick.labelcolor": INK_2, "ytick.labelcolor": INK_2,
        "xtick.labelsize": 9.0, "ytick.labelsize": 9.0, "grid.color": GRID, "grid.linewidth": 0.6,
        "grid.linestyle": "-", "hatch.linewidth": 0.8, "hatch.color": INK_2, "path.simplify": True,
        "svg.hashsalt": "aiops-inspect", "legend.frameon": False, "legend.fontsize": 9.0,
    }
    return params


# ---------------------------------------------------------------- drawing helpers

def _text_px(text: str, size: float) -> float:
    """Rough rendered width in pixels at DPI: CJK glyphs ~1 em, others ~0.6 em."""
    em = size * DPI / 72.0
    return sum(em * (1.0 if ord(ch) >= 0x2E80 else 0.62) for ch in text)


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:max(1, limit - 2)] + ".."


def _ink_on(fill: str) -> str:
    """Text colour for a filled cell: white on dark fills, primary ink otherwise."""
    r, g, b = (int(fill[i:i + 2], 16) / 255.0 for i in (1, 3, 5))

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    lum = 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
    return WHITE if (1.05 / (lum + 0.05)) > ((lum + 0.05) / 0.0537) else INK


def _figure(key: str) -> Any:
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    fig = Figure(figsize=FIG_SIZES[key], dpi=DPI, facecolor=SURFACE)
    FigureCanvasAgg(fig)
    return fig


def _frame(fig: Any, title: str, doc: Dict[str, Any], note: str = "") -> None:
    stage = " · 시험(DRY)" if doc["stage"] == "DRY" else ""
    fig.text(0.015, 0.965, title + stage, ha="left", va="top", fontsize=14, color=INK, fontweight="bold")
    if note:
        fig.text(0.015, 0.915, note, ha="left", va="top", fontsize=9, color=INK_2)
    fig.text(0.015, 0.015, FOOTER.format(snapshot=doc["snapshot"]), ha="left", va="bottom", fontsize=8.5, color=MUTED)


def _png_bytes(fig: Any) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, facecolor=SURFACE, metadata={"Software": None})
    return buf.getvalue()


def _patch(ax: Any, x: float, y: float, w: float, h: float, fill: str, hatch: Optional[str] = None,
           edge: str = SURFACE, lw: float = 1.0, z: float = 2.0) -> None:
    from matplotlib.patches import Rectangle
    ax.add_patch(Rectangle((x, y), w, h, facecolor=fill, edgecolor=edge, linewidth=lw, zorder=z))
    if hatch:
        ax.add_patch(Rectangle((x, y), w, h, facecolor="none", edgecolor=_hatch_ink(fill), linewidth=0.0,
                               hatch=hatch, zorder=z + 0.1))


def _hatch_ink(fill: str) -> str:
    return WHITE if _ink_on(fill) == WHITE else INK_2


def _legend_row(fig: Any, y: float, items: List[Tuple[str, str, Optional[str]]], x: float = 0.015) -> None:
    """Draw a one-line legend of (colour, word, hatch) swatches in figure coordinates."""
    from matplotlib.patches import Rectangle
    fig_w_px = fig.get_figwidth() * DPI
    for fill, word, hatch in items:
        fig.add_artist(Rectangle((x, y - 0.012), 0.012, 0.024, transform=fig.transFigure, facecolor=fill,
                                 edgecolor=INK_2 if fill in (SURFACE, "none") else fill, linewidth=0.6,
                                 hatch=hatch))
        fig.text(x + 0.016, y, word, ha="left", va="center", fontsize=9, color=INK_2)
        x += 0.016 + (_text_px(word, 9) + 26) / fig_w_px


def _products(doc: Dict[str, Any], section: str) -> List[str]:
    """Products of a section: scorecard order first, then the rest sorted."""
    order = [p for p in doc["scorecard"]["products"] if p in doc[section]]
    return order + sorted(p for p in doc[section] if p not in order)


def _kst(dt: datetime) -> datetime:
    return dt.astimezone(KST).replace(tzinfo=None)


# ---------------------------------------------------------------- C5 scorecard

def _change_words(verdict: str, previous: Optional[str]) -> str:
    if previous is None:
        return "첫 점검"
    before = LEVEL_WORDS[previous]
    if verdict == previous:
        return f"변화 없음 (전: {before})"
    if verdict in LEVEL_RANK and previous in LEVEL_RANK:
        return ("악화" if LEVEL_RANK[verdict] > LEVEL_RANK[previous] else "개선") + f" (전: {before})"
    return f"바뀜 (전: {before})"


def draw_scorecard(doc: Dict[str, Any]) -> Any:
    sc = doc["scorecard"]
    products = list(sc["products"])
    signals = list(sc["signals"])
    fig = _figure("c5")
    _frame(fig, "감리 점수표 (제품 × 신호)", doc, "칸마다 판정 단어를 적는다 (색만으로 구분하지 않음) · 굵은 테두리 칸 = 게시 판정")
    ax = fig.add_axes((0.015, 0.14, 0.97, 0.72))
    ax.set_axis_off()
    name_w, sig_w, verdict_w, change_w = 2.3, 1.0, 1.25, 2.3
    # Fixed minimum geometry so one product or few signals do not stretch the cells.
    total_w = name_w + sig_w * max(len(signals), len(SIGNALS)) + verdict_w + change_w
    rows = max(1, len(products))
    ax.set_xlim(0, total_w)
    ax.set_ylim(-(max(rows, 5) + 1.1), 0.05)
    if not products:
        ax.text(0.05, -1.6, "제품 없음", ha="left", va="center", fontsize=10, color=MUTED)
    head_fs = 8.5
    ax.text(0.05, -0.55, "제품", ha="left", va="center", fontsize=head_fs, color=INK_2, fontweight="bold")
    for j, sig in enumerate(signals):
        cx = name_w + sig_w * (j + 0.5)
        ax.text(cx, -0.55, f"{sig}\n{SIGNAL_NAMES[sig]}", ha="center", va="center", fontsize=head_fs, color=INK_2,
                linespacing=1.2)
    vx = name_w + sig_w * len(signals)
    ax.text(vx + verdict_w / 2, -0.55, "판정", ha="center", va="center", fontsize=head_fs + 0.5, color=INK,
            fontweight="bold")
    ax.text(vx + verdict_w + 0.12, -0.55, "직전 대비", ha="left", va="center", fontsize=head_fs, color=INK_2)
    cells = sc["cells"]
    basis = sc.get("basis", {})
    previous = sc.get("previous", {})
    hatched_any = False
    cell_fs = 9.0 if rows <= 8 else 7.5
    for i, product in enumerate(products):
        top = -(i + 1.1)
        ax.text(0.05, top - 0.5, _short(product, 24), ha="left", va="center", fontsize=9.5, color=INK)
        for j, sig in enumerate(signals):
            x = name_w + sig_w * j
            level = cells.get(product, {}).get(sig)
            if level is None:
                # CONTRACT NOTE: a missing cell is drawn as "—" on the surface (no data), not as a level.
                _patch(ax, x + 0.03, top - 0.94, sig_w - 0.06, 0.88, SURFACE, edge=GRID, lw=0.6)
                ax.text(x + sig_w / 2, top - 0.5, "—", ha="center", va="center", fontsize=cell_fs, color=MUTED)
                continue
            gh_text = sig in GH_TEXT_SIGNALS or basis.get(product, {}).get(sig) == "GH_TEXT"
            hatched_any = hatched_any or gh_text
            fill = LEVEL_COLORS[level]
            _patch(ax, x + 0.03, top - 0.94, sig_w - 0.06, 0.88, fill, GH_HATCH if gh_text else None)
            ax.text(x + sig_w / 2, top - 0.5, LEVEL_WORDS[level], ha="center", va="center", fontsize=cell_fs,
                    color=_ink_on(fill), zorder=5,
                    bbox={"boxstyle": "round,pad=0.25", "facecolor": fill, "edgecolor": "none"} if gh_text else None)
        verdict = sc["verdicts"][product]
        fill = LEVEL_COLORS[verdict]
        _patch(ax, vx + 0.03, top - 0.94, verdict_w - 0.06, 0.88, fill, edge=INK, lw=2.0)
        ax.text(vx + verdict_w / 2, top - 0.5, LEVEL_WORDS[verdict], ha="center", va="center", fontsize=cell_fs + 1,
                color=_ink_on(fill), fontweight="bold", zorder=5)
        ax.text(vx + verdict_w + 0.12, top - 0.5, _change_words(verdict, previous.get(product)), ha="left",
                va="center", fontsize=8.5, color=INK_2)
    items = [(LEVEL_COLORS[k], LEVEL_WORDS[k], None) for k in ("ON_TRACK", "WATCH", "AT_RISK")]
    items.append((LEVEL_COLORS["UNKNOWN"], "멈춤·미설정·확인 불가", None))
    if hatched_any:
        items.append((SURFACE, "빗금 = " + GH_TEXT_NOTE, GH_HATCH))
    _legend_row(fig, 0.09, items)
    return fig


# ---------------------------------------------------------------- C1 ladder

def _ladder_segments(row: Dict[str, Any]) -> List[Tuple[str, int, str]]:
    segments = [(STAGE_WORDS[s], row[k], STAGE_COLORS[s]) for s, k in zip(STAGES[:-1], LADDER_KEYS)]
    checks = row["merge_checks"]
    rest = row["done"] - sum(checks.get(k, 0) for k in ("PASS", "FAIL", "PENDING"))
    for key, word, colour in DONE_PARTS:
        segments.append((word, rest if key == "REST" else checks.get(key, 0), colour))
    return segments


def draw_ladder(doc: Dict[str, Any]) -> Any:
    from matplotlib.ticker import MaxNLocator
    products = _products(doc, "ladder")
    fig = _figure("c1")
    _frame(fig, "진행 사다리 (노드 수)", doc, "계획 → 생성 → 시작 전 → 진행 → 전달 → 완료, 완료는 병합 후 필수 검사로 나눔")
    ax = fig.add_axes((0.16, 0.2, 0.5, 0.64))
    # At least five row slots, so one product does not become one giant bar.
    rows = max(5, len(products))
    totals = [sum(n for _, n, _ in _ladder_segments(doc["ladder"][p])) for p in products] or [0]
    xmax = max(1, max(totals))
    ax.set_xlim(0, xmax * 1.02)
    ax.set_ylim(rows - 0.5, -0.5)
    if not products:
        ax.text(0.02, 0.5, "제품 없음", ha="left", va="center", fontsize=10, color=MUTED, transform=ax.transAxes)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.grid(axis="x", zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.set_yticks(range(len(products)))
    ax.set_yticklabels([_short(p, 22) for p in products], fontsize=9.5, color=INK)
    ax.tick_params(axis="y", length=0)
    px_per_unit = 0.5 * FIG_SIZES["c1"][0] * DPI / (xmax * 1.02)
    bar_h = 0.56
    for i, product in enumerate(products):
        row = doc["ladder"][product]
        left = 0
        for word, count, colour in _ladder_segments(row):
            if count <= 0:
                continue
            _patch(ax, left, i - bar_h / 2, count, bar_h, colour, edge=SURFACE, lw=1.5)
            width_px = count * px_per_unit
            ink = _ink_on(colour)
            if _text_px(f"{word} {count}", 8.5) + 8 < width_px:
                ax.text(left + count / 2, i, f"{word} {count}", ha="center", va="center", fontsize=8.5, color=ink,
                        zorder=5)
            elif _text_px(str(count), 8.5) + 6 < width_px:
                ax.text(left + count / 2, i, str(count), ha="center", va="center", fontsize=8.5, color=ink, zorder=5)
            left += count
        if left == 0:
            ax.text(0.02 * xmax, i, "노드 없음", ha="left", va="center", fontsize=9, color=MUTED)
        side = fig.add_axes((0.68, 0.2 + 0.64 * (1 - (i + 1) / rows), 0.3, 0.64 / rows))
        side.set_axis_off()
        side.set_xlim(0, 1)
        side.set_ylim(0, 1)
        side.text(0.0, 0.62, f"계획 밖 병합 {row['orphans']} · 계획 변경 PR 대기 {row['pending_plan_prs']}",
                  ha="left", va="center", fontsize=9, color=INK)
        side.text(0.0, 0.3, NOT_RECORDED_TEXT, ha="left", va="center", fontsize=8.5, color=INK_2)
    ax.set_xlabel("노드 수", fontsize=9, color=INK_2)
    items = [(STAGE_COLORS[s], STAGE_WORDS[s], None) for s in STAGES[:-1]]
    items += [(colour, word, None) for _, word, colour in DONE_PARTS]
    _legend_row(fig, 0.075, items)
    return fig


# ---------------------------------------------------------------- C2 DAG

def dag_choices(doc: Dict[str, Any]) -> List[str]:
    """At most two products with the most non-DONE nodes (ties by name); products with none are skipped."""
    # CONTRACT NOTE: a product whose nodes are all DONE has nothing stuck to show, so it gets no C2.
    ranked = []
    for product, graph in doc["dag"].items():
        open_nodes = sum(1 for n in graph["nodes"] if n["stage"] != "DONE")
        if open_nodes:
            ranked.append((-open_nodes, product))
    return [p for _, p in sorted(ranked)[:MAX_DAG_CHARTS]]


def dag_file_name(product: str, graph: Dict[str, Any]) -> str:
    tag = graph.get("prefix") or re.sub(r"[^A-Za-z0-9_-]", "_", product.split("/")[-1])[:40]
    return f"c2_dag_{tag}.png"


def _fit_size(lines: List[str], box_w: float, box_h: float, largest: float = 10.5) -> float:
    size = largest
    while size > 4.5:
        wide = max(_text_px(line, size) for line in lines)
        tall = len(lines) * size * DPI / 72.0 * 1.3
        if wide <= box_w * 0.9 and tall <= box_h * 0.9:
            break
        size -= 0.5
    return size


def draw_dag(doc: Dict[str, Any], product: str) -> Any:
    graph = doc["dag"][product]
    nodes = sorted(graph["nodes"], key=lambda n: (n["depth"], n["row"], n["id"]))
    fig = _figure("c2")
    _frame(fig, f"계획 DAG · {_short(product, 40)}", doc, "굵은 선 = 임계 경로 · 굵은 테두리 = 임계 경로 노드")
    rect = (0.02, 0.12, 0.96, 0.74)
    ax = fig.add_axes(rect)
    ax.set_axis_off()
    depths = max(n["depth"] for n in nodes) + 1
    rows = max(n["row"] for n in nodes) + 1
    ax.set_xlim(-0.55, depths - 0.45)
    ax.set_ylim(rows - 0.5, -0.5)
    px_x = rect[2] * FIG_SIZES["c2"][0] * DPI / depths
    px_y = rect[3] * FIG_SIZES["c2"][1] * DPI / rows
    # Boxes keep a readable size: at most ~240 x 110 px however few nodes there are.
    box_w, box_h = min(0.8, 240.0 / px_x), min(0.72, 110.0 / px_y)
    pos = {n["id"]: (n["depth"], n["row"]) for n in nodes}
    crit = {n["id"] for n in nodes if n.get("critical")}
    for a, b in sorted(tuple(e) for e in graph["edges"]):
        bold = a in crit and b in crit
        (x1, y1), (x2, y2) = pos[a], pos[b]
        ax.annotate("", xy=(x2 - box_w / 2, y2), xytext=(x1 + box_w / 2, y1),
                    arrowprops={"arrowstyle": "-|>", "color": INK if bold else MUTED, "lw": 2.4 if bold else 0.9,
                                "shrinkA": 0, "shrinkB": 1, "mutation_scale": 9}, zorder=1)
    gh_blocked = False
    other_blocked = False
    for n in nodes:
        x, y = n["depth"], n["row"]
        fill = STAGE_COLORS[n["stage"]]
        blocked = bool(n.get("blocked"))
        hatch = GH_HATCH if blocked else None
        if blocked:
            if n.get("basis_blocked") == "GH_TEXT":
                gh_blocked = True
            else:
                other_blocked = True
        _patch(ax, x - box_w / 2, y - box_h / 2, box_w, box_h, fill, hatch,
               edge=INK if n.get("critical") else SURFACE, lw=2.2 if n.get("critical") else 1.0, z=3)
        lines = [n["id"], STAGE_WORDS[n["stage"]]]
        if blocked:
            hours = n.get("blocked_hours")
            lines.append("막힘" + (f" {int(hours)}시간" if hours is not None else ""))
        size = _fit_size(lines, box_w * px_x, box_h * px_y)
        ink = _ink_on(fill)
        ax.text(x, y, "\n".join(lines), ha="center", va="center", fontsize=size, color=ink, zorder=6,
                linespacing=1.15, bbox={"boxstyle": "round,pad=0.25", "facecolor": fill, "edgecolor": "none"}
                if blocked else None)
    items = [(STAGE_COLORS[s], STAGE_WORDS[s], None) for s in STAGES]
    if gh_blocked:
        items.append((SURFACE, "빗금 = 막힘 " + GH_TEXT_NOTE, GH_HATCH))
    if other_blocked:
        items.append((SURFACE, "빗금 = 막힘", GH_HATCH))
    _legend_row(fig, 0.075, items)
    return fig


# ---------------------------------------------------------------- C3 burn-up

def _grid_shape(count: int) -> Tuple[int, int]:
    if count <= 3:
        return 1, max(1, count)
    if count <= 6:
        return 2, 3
    if count <= 9:
        return 3, 3
    return 3, 4


def draw_burnup(doc: Dict[str, Any]) -> Any:
    from matplotlib.dates import DateFormatter, AutoDateLocator, date2num
    from matplotlib.ticker import MaxNLocator
    products = _products(doc, "burnup")
    fig = _figure("c3")
    _frame(fig, "누적 완료 (번업, 최근 30일)", doc, "실선 = 완료 노드 · 점선 = 계획 노드 (계획이 바뀌면 점선이 움직인다)")
    nrows, ncols = _grid_shape(len(products))
    left, bottom, width, height = 0.05, 0.1, 0.93, 0.74
    gap_x, gap_y = 0.045, 0.1
    cell_w = (width - gap_x * (ncols - 1)) / ncols
    cell_h = (height - gap_y * (nrows - 1)) / nrows
    if not products:
        fig.text(0.5, 0.5, "기록 없음", ha="center", va="center", fontsize=12, color=MUTED)
    for idx, product in enumerate(products):
        r, c = divmod(idx, ncols)
        ax = fig.add_axes((left + c * (cell_w + gap_x), bottom + (nrows - 1 - r) * (cell_h + gap_y), cell_w,
                           cell_h * 0.86))
        ax.set_title(_short(product, 30), loc="left", fontsize=10, color=INK, pad=4)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.grid(axis="y", zorder=0)
        ax.set_axisbelow(True)
        points = sorted(doc["burnup"][product], key=lambda p: parse_time(p["t"], "t"))
        if not points:
            ax.set_xticks([])
            ax.set_yticks([])
            ax.text(0.5, 0.5, "기록 없음", ha="center", va="center", fontsize=10, color=MUTED, transform=ax.transAxes)
            continue
        xs = [date2num(_kst(parse_time(p["t"], "t"))) for p in points]
        done = [p["done"] for p in points]
        planned = [p["planned"] for p in points]
        ax.step(xs, planned, where="post", color=MUTED, linewidth=1.6, linestyle=(0, (4, 2)), zorder=3)
        ax.step(xs, done, where="post", color=LANE_COLORS["DEVIN"], linewidth=2.0, zorder=4)
        if len(xs) == 1:
            ax.plot(xs, done, marker="o", markersize=5, color=LANE_COLORS["DEVIN"], zorder=5)
            ax.set_xlim(xs[0] - 1, xs[0] + 1)
        top = max(max(planned), max(done), 1)
        ax.set_ylim(0, top * 1.18 + 0.5)
        ax.yaxis.set_major_locator(MaxNLocator(integer=True, nbins=5))
        locator = AutoDateLocator(minticks=2, maxticks=5)
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(DateFormatter("%m/%d"))
        ax.tick_params(axis="x", labelsize=8)
        ax.tick_params(axis="y", labelsize=8)
        ax.text(0.99, 0.97, f"완료 {done[-1]} / 계획 {planned[-1]}", ha="right", va="top", fontsize=9, color=INK,
                transform=ax.transAxes)
    from matplotlib.lines import Line2D
    fig.legend(handles=[Line2D([], [], color=LANE_COLORS["DEVIN"], linewidth=2.0, label="완료"),
                        Line2D([], [], color=MUTED, linewidth=1.6, linestyle=(0, (4, 2)), label="계획 노드")],
               loc="upper right", bbox_to_anchor=(0.985, 0.975), ncol=2, fontsize=9)
    fig.text(0.985, 0.055, "시간 = KST", ha="right", va="bottom", fontsize=8, color=MUTED)
    return fig


# ---------------------------------------------------------------- C4 lanes

def draw_lanes(doc: Dict[str, Any]) -> Any:
    from matplotlib.dates import DateFormatter, DayLocator, date2num
    from matplotlib.patches import Rectangle
    lanes = doc["lanes"]
    start = parse_time(lanes["window_start"], "window_start")
    end = parse_time(lanes["window_end"], "window_end")
    x0, x1 = date2num(_kst(start)), date2num(_kst(end))
    fig = _figure("c4")
    _frame(fig, "레인 사용 (최근 7일)", doc, "막대 = 세션(작업 번호) · 옅은 막대 = 리뷰 · 검은 끝선 = 진행 중")
    rect = (0.12, 0.2, 0.855, 0.64)
    ax = fig.add_axes(rect)
    idle_row = len(LANE_ORDER)
    ax.set_xlim(x0, x1)
    ax.set_ylim(idle_row + 0.5, -0.5)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.grid(axis="x", zorder=0)
    ax.set_axisbelow(True)
    enabled = lanes["enabled"]
    labels = [lane + ("" if enabled.get(lane, True) else "\n(꺼짐)") for lane in LANE_ORDER]
    ax.set_yticks(range(idle_row + 1))
    ax.set_yticklabels(labels + ["빈 레인·대기"], fontsize=10, color=INK)
    ax.get_yticklabels()[-1].set_color(INK_2)
    ax.get_yticklabels()[-1].set_fontsize(9)
    ax.tick_params(axis="y", length=0)
    ax.axhline(idle_row - 0.5, color=GRID, linewidth=0.8, zorder=0)
    ax.xaxis.set_major_locator(DayLocator())
    ax.xaxis.set_major_formatter(DateFormatter("%m/%d"))
    px_per_day = rect[2] * FIG_SIZES["c4"][0] * DPI / max(x1 - x0, 1e-9)
    right_px = x1 * px_per_day
    # Idle-while-waiting spans get their own hatched strip so they never cover session labels.
    has_idle = False
    for span in lanes.get("idle_waiting", []):
        a = max(x0, date2num(_kst(parse_time(span["start"], "start"))))
        b = min(x1, date2num(_kst(parse_time(span["end"], "end"))))
        if b > a:
            has_idle = True
            ax.add_patch(Rectangle((a, idle_row - 0.2), b - a, 0.4, facecolor=SURFACE, edgecolor=INK_2,
                                   hatch=IDLE_HATCH, linewidth=0.6, zorder=3))
    if not has_idle:
        ax.text(x0 + (x1 - x0) * 0.005, idle_row, "없음", ha="left", va="center", fontsize=8.5, color=MUTED)
    bar_h = 0.5
    ordered = sorted(lanes["intervals"], key=lambda it: (LANE_ORDER.index(it["lane"]), it["start"], it["task"]))
    above_end = {lane: -math.inf for lane in LANE_ORDER}
    reviewer_seen = False
    for item in ordered:
        row = LANE_ORDER.index(item["lane"])
        a = date2num(_kst(parse_time(item["start"], "start")))
        open_end = item["end"] is None
        b = x1 if open_end else date2num(_kst(parse_time(item["end"], "end")))
        a, b = max(a, x0), min(b, x1)
        if b <= a:
            continue
        colour = LANE_COLORS[item["lane"]]
        reviewer = item["role"] != "WRITER"
        reviewer_seen = reviewer_seen or reviewer
        cursor = item["lane"] == "CURSOR"
        ax.add_patch(Rectangle((a, row - bar_h / 2), b - a, bar_h, facecolor=colour, alpha=0.45 if reviewer else 1.0,
                               edgecolor=INK if cursor else SURFACE, linewidth=1.0 if cursor else 0.8, zorder=3))
        if open_end:
            ax.plot([b, b], [row - bar_h / 2 - 0.06, row + bar_h / 2 + 0.06], color=INK, linewidth=1.6, zorder=4)
        width_px = (b - a) * px_per_day
        start_px = a * px_per_day
        if _text_px(item["task"], 8.0) + 8 < width_px:
            ax.text((a + b) / 2, row, item["task"], ha="center", va="center", fontsize=8.0,
                    color=INK if reviewer else _ink_on(colour), zorder=5)
            continue
        # Narrow bar: label above it, only when it neither hits the previous label nor leaves the plot.
        label_px = _text_px(item["task"], 7.0)
        if start_px < above_end[item["lane"]] + 6:
            continue
        if start_px + label_px <= right_px:
            ax.text(a, row - bar_h / 2 - 0.03, item["task"], ha="left", va="bottom", fontsize=7.0, color=INK_2,
                    zorder=5)
            above_end[item["lane"]] = start_px + label_px
        elif b * px_per_day - label_px >= above_end[item["lane"]] + 6:
            ax.text(b, row - bar_h / 2 - 0.03, item["task"], ha="right", va="bottom", fontsize=7.0, color=INK_2,
                    zorder=5)
            above_end[item["lane"]] = right_px
    for row, lane in enumerate(LANE_ORDER):
        if not any(it["lane"] == lane for it in ordered):
            ax.text(x0 + (x1 - x0) * 0.005, row, "세션 없음", ha="left", va="center", fontsize=8.5, color=MUTED)
    items: List[Tuple[str, str, Optional[str]]] = [(LANE_COLORS[lane], lane, None) for lane in LANE_ORDER]
    if reviewer_seen:
        items.append(("#d9d8d2", "옅은 막대 = 리뷰", None))
    items.append((SURFACE, "빗금 = 빈 레인이 있는데 대기 노드 있음", IDLE_HATCH))
    _legend_row(fig, 0.105, items)
    fig.text(0.015, 0.06, LANE_CAPTION + " · 시간 = KST", ha="left", va="center", fontsize=9, color=INK_2)
    return fig


# ---------------------------------------------------------------- render

def _write_file(out_dir: Path, name: str, data: bytes) -> None:
    target = out_dir / name
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(out_dir))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _finish(fig: Any) -> Tuple[bytes, int, int]:
    raw = _png_bytes(fig)
    try:
        data = filter_png(raw)
        width, height = validate_png(data)
    except ValueError as exc:
        raise ChartError("PNG_INVALID", str(exc)) from exc
    return data, width, height


def render(chart_data_path: Any, out_dir: Any, font_path: Optional[str] = None) -> Dict[str, Any]:
    """Draw C5, C1, C2 (≤2), C3, C4 into out_dir; return {"status": "OK", "pngs": [...], "font": family}.

    Returns {"status": "FONT_MISSING"} (no files) when no Korean font is found. Raises ValueError on bad
    chart data and ChartError on render failures."""
    out = Path(out_dir)
    info = os.lstat(out)
    if not stat.S_ISDIR(info.st_mode):
        raise ChartError("OUT_DIR", "output must be an existing directory, not a link")
    doc = load_chart_data(Path(chart_data_path))
    matplotlib = _matplotlib()
    font = find_font(font_path)
    if font is None:
        return {"status": "FONT_MISSING"}
    jobs: List[Tuple[str, Any]] = [("c5_scorecard.png", lambda: draw_scorecard(doc)),
                                   ("c1_ladder.png", lambda: draw_ladder(doc))]
    for product in dag_choices(doc):
        jobs.append((dag_file_name(product, doc["dag"][product]), (lambda p=product: draw_dag(doc, p))))
    jobs += [("c3_burnup.png", lambda: draw_burnup(doc)), ("c4_lanes.png", lambda: draw_lanes(doc))]
    rendered: List[Tuple[str, bytes, int, int]] = []
    names = set()
    with matplotlib.rc_context(_rc(font["family"])):
        if "text.parse_math" in matplotlib.rcParams:
            matplotlib.rcParams["text.parse_math"] = False
        for name, build in jobs:
            if name in names:
                raise ChartError("NAME_CLASH", name)
            names.add(name)
            fig = build()
            try:
                rendered.append((name,) + _finish(fig))
            finally:
                fig.clear()
    pngs = []
    for name, data, width, height in rendered:
        _write_file(out, name, data)
        pngs.append({"name": name, "sha256": hashlib.sha256(data).hexdigest(), "w": width, "h": height})
    manifest = {"pngs": pngs, "font": font["family"]}
    _write_file(out, "render.json", json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return {"status": "OK", "pngs": pngs, "font": font["family"]}


# ---------------------------------------------------------------- command mode

class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # one JSON line on stdout, exit 2, no usage text
        print(json.dumps({"status": "ERROR", "reason": "ARGS"}))
        raise SystemExit(2)


def main(argv: Optional[List[str]] = None) -> int:
    parser = _Parser(prog="control_plane_inspect_charts.py", add_help=False)
    sub = parser.add_subparsers(dest="command")
    cmd = sub.add_parser("render", add_help=False)
    cmd.add_argument("--data", required=True)
    cmd.add_argument("--out", required=True)
    cmd.add_argument("--font")
    args = parser.parse_args(argv)
    if args.command != "render":
        parser.error("command required")
    own_cache = None
    if "MPLCONFIGDIR" not in os.environ:
        own_cache = tempfile.mkdtemp(prefix="aiops-mpl-")
        os.environ["MPLCONFIGDIR"] = own_cache
    try:
        result = render(args.data, args.out, args.font)
        code = 0
    except ChartError as exc:
        result, code = {"status": "ERROR", "reason": exc.reason}, 1
    except (ValueError, UnicodeDecodeError) as exc:
        result, code = {"status": "ERROR", "reason": "CHART_DATA", "detail": str(exc)[:200]}, 1
    except OSError:
        result, code = {"status": "ERROR", "reason": "IO"}, 1
    except Exception:
        result, code = {"status": "ERROR", "reason": "RENDER"}, 1
    finally:
        if own_cache:
            os.environ.pop("MPLCONFIGDIR", None)
            shutil.rmtree(own_cache, ignore_errors=True)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
