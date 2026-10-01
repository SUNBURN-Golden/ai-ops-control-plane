"""Program inspector signals: floors, verdicts, findings lifecycle and chart data (docs/INSPECTOR.md §6-8).

The read-only program inspector (User decision M7) is advisory only and never a gate. This
module is pure: it reads one AIOPS_INSPECT_FACTS_V1 document, the previous findings state and
the history rows, and returns signals S0-S9, verdicts, findings with stable INS-<PFX>-<NNNN>
ids, and the AIOPS_INSPECT_CHARTS_V1 document. It does no I/O, never computes completion itself
(the facts carry control_plane_program.node_completion output) and never copies free GitHub text
(titles, bodies, comments) into findings: every finding text is a fixed Korean template filled
only with ids, numbers, lane names and times.

Three-valued logic: a signal whose inputs are missing is UNKNOWN; its findings are frozen
(shown 확인 불가) and never resolved on missing data.
"""
from __future__ import annotations

import hashlib
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import control_plane_program as cp_program
import control_plane_inspect_core as core

# ---------------------------------------------------------------------------- constants

STATE_SCHEMA = "AIOPS_INSPECT_FINDINGS_V1"
EVALUATION_SCHEMA = "AIOPS_INSPECT_SIGNALS_V1"
CHARTS_SCHEMA = "AIOPS_INSPECT_CHARTS_V1"
CTRL = "CTRL"
DEFAULT_CONTROL_REPOSITORY = "BeautifulMind-JT/ai-ops-control-plane"

THRESHOLDS: Dict[str, int] = {
    "orphan_watch": 1, "orphan_at_risk": 2, "resumes_watch": 2, "resumes_at_risk": 3,
    "review_fail_watch": 2, "review_fail_at_risk": 3, "prestart_watch": 2, "exceptions_node_watch": 3,
    "exceptions_product_watch": 6, "blocked_watch_h": 24, "done_gap_watch_d": 3, "done_gap_at_risk_d": 7,
    "confirmed_watch_h": 6, "confirmed_at_risk_h": 12, "cleanup_watch_h": 24, "idle_waiting_snapshots": 2,
    "idle_waiting_min_span_h": 1, "pending_checks_watch_h": 24,
}

ON_TRACK, WATCH, AT_RISK = "ON_TRACK", "WATCH", "AT_RISK"
PAUSED, NOT_CONFIGURED, UNKNOWN = "PAUSED", "NOT_CONFIGURED", "UNKNOWN"
LEVELS = (ON_TRACK, WATCH, AT_RISK)  # DIVERGED is reserved for model judgment (PR2); never produced here.
LEVEL_RANK = {ON_TRACK: 0, WATCH: 1, AT_RISK: 2}
SEVERITIES = (WATCH, AT_RISK)
LEVEL_WORDS = {ON_TRACK: "정상", WATCH: "주의", AT_RISK: "위험", PAUSED: "멈춤",
               NOT_CONFIGURED: "미설정", UNKNOWN: "확인 불가"}
SIGNALS = ("S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9")
SIGNAL_NAMES = {"S0": "기록 정합", "S1": "계획 대비 진행", "S2": "반복 실패", "S3": "작은 예외 누적",
                "S4": "계약 정합", "S5": "증거 약화", "S6": "정체", "S7": "레인", "S8": "결정 연속성",
                "S9": "CURSOR 카나리아"}
CTRL_SIGNALS = ("S0", "S7")
PRODUCT_SIGNALS = ("S1", "S2", "S3", "S4", "S5", "S6", "S8", "S9")
BASES = ("HOST", "GH_SYSTEM", "SHA_CONTENT", "GH_TEXT")
DEFAULT_BASIS = {"S0": "HOST", "S1": "GH_SYSTEM", "S2": "HOST", "S3": "GH_TEXT", "S4": "GH_SYSTEM",
                 "S5": "GH_SYSTEM", "S6": "GH_SYSTEM", "S7": "HOST", "S8": "HOST", "S9": "HOST"}
S8_VALUE = "2단계(모델)에서 구현"
LIFECYCLE_STATES = ("NEW", "OPEN", "WORSENED", "REOPENED", "RESOLVED")
CHANGE_STATES = ("NEW", "WORSENED", "REOPENED", "RESOLVED")
RESOLVE_AFTER = 2  # consecutive evaluated absences

STAGES = cp_program.COMPLETION_STAGES
STAGE_KEYS = {"PLANNED": "planned", "MATERIALIZING": "materializing", "NOT_STARTED": "not_started",
              "IN_PROGRESS": "in_progress", "DELIVERED": "delivered", "DONE": "done"}
STAGE_WORDS = {"PLANNED": "계획", "MATERIALIZING": "생성 중", "NOT_STARTED": "시작 전", "IN_PROGRESS": "진행 중",
               "DELIVERED": "전달", "DONE": "완료", UNKNOWN: "확인 불가"}
MERGE_CHECK_KEYS = ("PASS", "FAIL", "PENDING", "NOT_CONFIGURED")
NOT_RECORDED = cp_program.POST_MERGE_NOT_RECORDED
LANE_ORDER = cp_program.LANE_ORDER
ACTIVE_ROW_STATES = ("SUBMITTING", "CONFIRMED", "UNKNOWN")
BLOCKING_LABELS = ("needs-user", "needs-operator", "needs-lane-cleanup", "blocked", "decision-required")
CLEANUP_LABEL = "needs-lane-cleanup"
WORKFLOWS_DIR = ".github/workflows/"
AIOPS_DIR = ".aiops/"

# Collection groups a facts product may list in "unknown", and the signals each one feeds.
# CONTRACT NOTE: §3.4 names only "host" and "plan"; these names are this module's vocabulary.
# Any group name not listed here makes EVERY signal of that product UNKNOWN (the safe reading).
SIGNAL_DEPS: Dict[str, Tuple[str, ...]] = {
    "S1": ("host", "plan", "pulls", "commits", "issues", "delivery"),
    "S2": ("host", "plan"),
    "S3": ("plan", "comments"),
    "S4": ("plan", "pulls", "files"),
    "S5": ("host", "plan", "pulls", "files", "checks", "required_checks", "delivery"),
    "S6": ("host", "plan", "issues", "events", "delivery"),
    "S8": (),
    "S9": ("host", "plan"),
}
# Product groups that S7 (CTRL) reads for the needs-lane-cleanup part.
S7_PRODUCT_DEPS = ("issues", "events", "plan")
KNOWN_GROUPS = frozenset(g for deps in SIGNAL_DEPS.values() for g in deps) | {"lanes", "ledger", "control"}

WINDOW_LANES = timedelta(days=7)
WINDOW_BURNUP = timedelta(days=30)
IDLE_SERIES_KEEP = timedelta(days=8)
MAX_IDLE_POINTS = 400
MAX_BURNUP_POINTS = 2000
MAX_INTERVALS = 5000

REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
NODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
TASK_RE = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,99}")
SHA_RE = re.compile(r"[0-9a-f]{7,64}")
HEX64_RE = re.compile(r"[0-9a-f]{64}")
PREFIX_RE = re.compile(r"[A-Z]{4}")
WORD_RE = re.compile(r"[A-Z][A-Z_]{0,31}")
ID_RE = re.compile(r"INS-([A-Z]{4})-([0-9]{4,})")


# ---------------------------------------------------------------------------- small helpers

def thresholds(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    """THRESHOLDS with config overrides of known keys (positive integers) applied."""
    merged = dict(THRESHOLDS)
    for key, value in (overrides or {}).items():
        if key not in THRESHOLDS:
            raise core.InspectError("CONFIG", "unknown threshold key")
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10000:
            raise core.InspectError("CONFIG", "threshold must be an integer in 1..10000")
        merged[key] = value
    return merged


def max_level(levels: Iterable[str]) -> str:
    """The highest of ON_TRACK < WATCH < AT_RISK; other words are ignored; ON_TRACK when none."""
    best = ON_TRACK
    for level in levels:
        if level in LEVEL_RANK and LEVEL_RANK[level] > LEVEL_RANK[best]:
            best = level
    return best


def finding_key(signal: str, product: str, subject_key: str) -> str:
    return hashlib.sha256(f"{signal}|{product}|{subject_key}".encode("utf-8")).hexdigest()[:16]


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _int(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _time(value: Any) -> Optional[datetime]:
    """An aware UTC datetime from an ISO string or epoch seconds, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or value < 0 or value > 1e11:
            return None
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return core.parse_iso(value)
        except core.InspectError:
            return None
    return None


def _hours(now: datetime, since: Optional[datetime]) -> Optional[float]:
    return None if since is None else max(0.0, (now - since).total_seconds() / 3600.0)


def _ok(value: Any, regex: "re.Pattern[str]") -> Optional[str]:
    return value if isinstance(value, str) and regex.fullmatch(value) else None


def _t_node(value: Any) -> str:
    return _ok(value, NODE_RE) or "?"


def _t_task(value: Any) -> str:
    return _ok(value, TASK_RE) or "?"


def _t_repo(value: Any) -> str:
    return _ok(value, REPO_RE) or "?"


def _t_sha7(value: Any) -> str:
    sha = _ok(value, SHA_RE)
    return sha[:7] if sha else "?"


def _t_lane(value: Any) -> str:
    return value if value in LANE_ORDER else "?"


def _t_num(value: Any) -> str:
    number = _int(value)
    return str(number) if number is not None else "?"


def _t_when(dt: Optional[datetime]) -> str:
    return core.fmt_kst(dt) if dt is not None else "?"


def _labels(issue: Dict[str, Any]) -> List[str]:
    names = []
    for label in _list(issue.get("labels")):
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str):
            names.append(name)
    return names


def short_name(repository: str, all_repositories: Iterable[str] = ()) -> str:
    """The display/product key in chart data: the repo name, or owner/repo when two share a name."""
    name = repository.split("/", 1)[-1]
    clashes = [r for r in all_repositories if r != repository and r.split("/", 1)[-1] == name]
    return repository if clashes else name


# ---------------------------------------------------------------------------- finding texts

# Fixed Korean templates. Fields are filled ONLY with validated ids, numbers, lane names and times.
TEMPLATES: Dict[str, Tuple[str, str]] = {
    "ledger_edited": ("감리 기록 댓글 변경", "원장 댓글 {comment}의 본문 해시가 호스트 기록과 다르다(호스트 기록 기준)."),
    "ledger_missing": ("감리 기록 댓글 없음", "원장 댓글 {comment}을 읽을 수 없다(호스트 기록에는 있음)."),
    "orphan_pr": ("계획 밖 병합", "PR #{number}이 {when}에 기본 브랜치에 병합되었으나 어떤 노드의 전달 고정에도 없다. "
                  "최근 7일 계획 밖 변경 {count}건."),
    "direct_push": ("기본 브랜치 직접 푸시", "커밋 {sha}({when})은 병합된 PR 없이 기본 브랜치에 들어왔다. "
                    "최근 7일 계획 밖 변경 {count}건."),
    "completion_mismatch": ("완료 불일치", "노드 {node}의 이슈 #{issue}는 완료로 닫혔으나 중앙 완료 단계는 {stage}다"
                            "(GitHub 글 기준)."),
    "resumes": ("작성 재개 반복", "작업 {task}의 작성 세션이 {count}회 재개되었다(주의 {watch}, 위험 {risk})."),
    "review_fail": ("검토 실패 반복", "작업 {task}의 전달 헤드 {sha}에 검토 FAIL 고정 {count}건(주의 {watch}, "
                    "위험 {risk})."),
    "review_sessions": ("검토 세션 한도", "작업 {task}의 검토 요청 하나에 헤드 {sha} 기준 세션 {count}개(한도 {limit})."),
    "prestart": ("시작 전 실패 반복", "작업 {task}의 FAILED_PRESTART 기록 {count}건(주의 {watch})."),
    "exceptions_node": ("작은 예외 누적", "노드 {node}에 최근 14일 작은 예외 승인 {count}건(주의 {watch}, "
                        "GitHub 글 기준)."),
    "exceptions_product": ("작은 예외 누적", "제품 전체 최근 14일 작은 예외 승인 {count}건(주의 {watch}, GitHub 글 기준)."),
    "contract_pair": ("계약 짝 불일치", "짝 {pair}: {repo}의 계약 경로가 최근 7일 PR {prs}에서 바뀌었으나 "
                      "{partner}의 짝 경로는 바뀌지 않았다."),
    "tests_removed": ("테스트 파일 삭제", "전달 PR #{number}에서 테스트 파일 {count}개가 삭제되거나 다른 경로로 옮겨졌다."),
    "workflows_touched": ("워크플로 변경", "전달 PR #{number}이 .github/workflows/ 아래 파일 {count}개를 바꿨다."),
    "merge_checks_fail": ("병합 후 필수 검사 실패", "노드 {node}의 병합 커밋 {sha}에서 필수 검사가 실패했다."),
    "merge_checks_pending": ("병합 후 필수 검사 대기", "노드 {node}의 병합 커밋 {sha} 필수 검사가 {when} 병합 뒤 "
                             "{hours}시간 넘게 끝나지 않았다."),
    "checks_shrank": ("필수 검사 축소", "필수 검사 목록이 처음 본 목록보다 줄었다(현재 {count}개, 파일 내용 기준)."),
    "host_unknown": ("호스트 상태 불명", "노드 {node}: {what} 상태가 {state}다(호스트 기록 기준)."),
    "cp_blocked": ("핵심 경로 막힘", "핵심 경로 노드 {node}가 {when}부터 {hours}시간 넘게 막힘 라벨 상태다(GitHub 글 기준)."),
    "done_gap": ("완료 공백", "마지막 완료({when}) 뒤 {days}일이 지났다. 남은 노드 {count}개."),
    "confirmed_age": ("세션 장기 실행", "레인 {lane}의 세션(작업 {task})이 {when}부터 {hours}시간 넘게 CONFIRMED다."),
    "cleanup_label": ("레인 정리 대기", "작업 {task}의 needs-lane-cleanup 라벨이 {when}부터 {hours}시간 넘게 남아 있다"
                      "(GitHub 글 기준)."),
    "idle_waiting": ("유휴 레인과 대기 작업", "유휴 레인 {lanes}, 시작 가능 노드 {count}개가 {snapshots}회 연속 점검"
                     "({start}~{end})에서 함께 관측되었다."),
    "cursor_canary": ("CURSOR 검토 불일치", "작업 {task}: CURSOR 검토 {verdict} 뒤 같은 헤드 {sha}에서 {lane} 검토 FAIL."),
    "cursor_prestart": ("CURSOR 시작 전 실패", "CURSOR 레인의 FAILED_PRESTART 기록 {count}건(주의 2)."),
}
VERDICT_WORDS = {"PASS": "PASS", "PASS_WITH_NOTES": "PASS_WITH_NOTES"}


def _candidate(signal: str, product: str, subject_key: str, severity: str, basis: str, template: str,
               evidence: List[Dict[str, Any]], **fields: str) -> Dict[str, Any]:
    title, detail = TEMPLATES[template]
    return {"signal": signal, "product": product, "subject_key": subject_key, "severity": severity,
            "basis": basis, "title_ko": title, "detail_ko": detail.format(**fields),
            "evidence": [e for e in evidence if e]}


def _ev(kind: str, repository: Any, **ref: Any) -> Optional[Dict[str, Any]]:
    """One evidence reference; None when the repository or the reference is not a clean id."""
    repo = _ok(repository, REPO_RE)
    if repo is None:
        return None
    item: Dict[str, Any] = {"kind": kind, "repository": repo}
    if "number" in ref:
        number = _int(ref["number"])
        if number is None or number <= 0:
            return None
        item["number"] = number
    elif "sha" in ref:
        sha = _ok(ref["sha"], SHA_RE)
        if sha is None:
            return None
        item["sha"] = sha
    else:
        value = ref.get("ref")
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:#-]{0,99}", value):
            return None
        item["ref"] = value
    return item


def _result(signal: str, candidates: List[Dict[str, Any]], value: Any, *, level: Optional[str] = None,
            unknown: bool = False, basis: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    if level is None:
        level = max_level(c["severity"] for c in candidates)
    if basis is None:
        top = [c for c in candidates if c["severity"] == level]
        basis = top[0]["basis"] if top else DEFAULT_BASIS[signal]
    out = {"level": level, "basis": basis, "value": value, "candidates": candidates, "unknown": unknown}
    out.update(extra)
    return out


def _unknown_result(signal: str, why: Sequence[str]) -> Dict[str, Any]:
    return _result(signal, [], {"unknown_groups": sorted(set(why))}, level=UNKNOWN, unknown=True)


# ---------------------------------------------------------------------------- per product view

class Product:
    """Read-only view of one facts product with the derived per-node facts the signals share."""

    def __init__(self, repository: str, facts: Dict[str, Any]):
        self.repository = repository
        self.facts = facts
        self.prefix = _ok(facts.get("prefix"), PREFIX_RE) or "XXXX"
        plan = _dict(facts.get("plan"))
        self.plan = plan
        self.plan_state = plan.get("state") if plan.get("state") in ("PRESENT", "NONE", "INVALID") else UNKNOWN
        self.unknown_groups = {g for g in _list(facts.get("unknown")) if isinstance(g, str)}
        self.plan_nodes: List[Dict[str, Any]] = []
        for node in _list(plan.get("nodes")):
            if isinstance(node, dict) and _ok(node.get("id"), NODE_RE):
                self.plan_nodes.append(node)
        self.ids = [n["id"] for n in self.plan_nodes]
        id_set = set(self.ids)
        self.deps = {n["id"]: sorted({d for d in _list(n.get("depends_on")) if d in id_set and d != n["id"]})
                     for n in self.plan_nodes}
        self.nodes = _dict(facts.get("nodes"))
        self.stage: Dict[str, str] = {}
        for node_id in self.ids:
            completion = _dict(_dict(self.nodes.get(node_id)).get("completion"))
            stage = completion.get("stage")
            self.stage[node_id] = stage if stage in STAGES else UNKNOWN
        if self.plan_state == "PRESENT":
            # A plan node with no collected node facts, or a CREATED task without host rows, is a host gap.
            for node_id in self.ids:
                node = self.nodes.get(node_id)
                if not isinstance(node, dict) or self.stage[node_id] == UNKNOWN:
                    self.unknown_groups.add("host")
                    continue
                mstatus = _dict(node.get("materialization"))
                if mstatus.get("status") == "CREATED" and not isinstance(node.get("rows"), list):
                    self.unknown_groups.add("host")
        elif self.plan_state == UNKNOWN:
            self.unknown_groups.add("plan")

    @property
    def paused(self) -> bool:
        return self.plan_state in ("NONE", "INVALID")

    def node(self, node_id: str) -> Dict[str, Any]:
        return _dict(self.nodes.get(node_id))

    def rows(self, node_id: str) -> List[Dict[str, Any]]:
        return [r for r in _list(self.node(node_id).get("rows")) if isinstance(r, dict)]

    def task_id(self, node_id: str) -> str:
        task = self.node(node_id).get("task_id")
        if isinstance(task, str) and TASK_RE.fullmatch(task):
            return task
        program = self.plan.get("program")
        return cp_program.task_id_for(program, node_id) if isinstance(program, str) else node_id.upper()

    def issue(self, node_id: str) -> Dict[str, Any]:
        return _dict(self.node(node_id).get("issue"))

    def missing_for(self, signal: str) -> List[str]:
        """The unknown groups that make ``signal`` UNKNOWN for this product (empty when evaluable)."""
        deps = set(SIGNAL_DEPS.get(signal, ()))
        if not deps:
            return []
        foreign = {g for g in self.unknown_groups if g not in KNOWN_GROUPS}
        return sorted((self.unknown_groups & deps) | foreign | ({"plan"} & self.unknown_groups))

    def done(self, node_id: str) -> bool:
        return self.stage.get(node_id) == "DONE"

    def remaining(self) -> List[str]:
        return [n for n in self.ids if not self.done(n)]

    def waiting(self) -> List[str]:
        """Nodes ready to start: every dependency DONE and the node PLANNED or NOT_STARTED."""
        return [n for n in self.ids if self.stage[n] in ("PLANNED", "NOT_STARTED")
                and all(self.done(d) for d in self.deps[n])]

    def orphans(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        prs = [pr for pr in _list(self.facts.get("merged_7d"))
               if isinstance(pr, dict) and pr.get("pinned") is not True and pr.get("aiops_only") is not True]
        pushes = [c for c in _list(self.facts.get("direct_pushes_7d")) if isinstance(c, dict)]
        return prs, pushes


def ladder_counts(product: Product) -> Dict[str, Any]:
    """Completion ladder counts from the central node_completion stages in the facts."""
    counts: Dict[str, Any] = {key: 0 for key in STAGE_KEYS.values()}
    checks = {key: 0 for key in MERGE_CHECK_KEYS}
    unknown = 0
    for node_id in product.ids:
        stage = product.stage[node_id]
        if stage == UNKNOWN:
            unknown += 1
            continue
        counts[STAGE_KEYS[stage]] += 1
        if stage == "DONE":
            state = _dict(product.node(node_id).get("completion")).get("merge_checks")
            if state in checks:
                checks[state] += 1
    prs, pushes = product.orphans()
    counts.update(merge_checks=checks, deployed=NOT_RECORDED, verified=NOT_RECORDED, orphans=len(prs) + len(pushes),
                  pending_plan_prs=len(_list(product.plan.get("pending_plan_prs"))), unknown_nodes=unknown)
    return counts


# ---------------------------------------------------------------------------- S0 (CTRL)

def _journal_map(journal: Any) -> Dict[str, str]:
    """{comment id: body sha256} from a dict or a list of journal entries."""
    out: Dict[str, str] = {}
    items: List[Tuple[Any, Any]] = []
    if isinstance(journal, dict):
        items = list(journal.items())
    else:
        for entry in _list(journal):
            if isinstance(entry, dict):
                items.append((entry.get("comment_id", entry.get("id")), entry.get("body_sha256", entry.get("sha256"))))
    for cid, sha in items:
        number = _int(cid) if not isinstance(cid, str) else (int(cid) if cid.isdigit() else None)
        if number is not None and number > 0 and isinstance(sha, str) and HEX64_RE.fullmatch(sha):
            out[str(number)] = sha
    return out


def signal_s0(facts: Dict[str, Any], journal: Any, control_repository: str) -> Dict[str, Any]:
    """S0 기록 정합: the inspector's own ledger comments against the host journal (AT_RISK on edit)."""
    ledger = facts.get("ledger")
    if not isinstance(ledger, dict) or not isinstance(ledger.get("comments"), list):
        return _unknown_result("S0", ["ledger"])
    expected = _journal_map(journal)
    seen = {}
    for comment in ledger["comments"]:
        if isinstance(comment, dict) and _int(comment.get("id")):
            seen[str(comment["id"])] = comment
    candidates = []
    checked = 0
    for cid in sorted(expected, key=int):
        if cid not in seen:
            continue  # not read this time: nothing to compare (the journal is the authority)
        checked += 1
        comment = seen[cid]
        # CONTRACT NOTE: a comment the reader saw as deleted (body_sha256 null, or "deleted": true) is
        # treated like an edit: the posted record no longer matches the host journal.
        sha = comment.get("body_sha256")
        if comment.get("deleted") is True or sha is None:
            template = "ledger_missing"
        elif sha != expected[cid]:
            template = "ledger_edited"
        else:
            continue
        candidates.append(_candidate("S0", CTRL, f"ledger:{cid}", AT_RISK, "HOST", template,
                                     [_ev("issue", control_repository, ref=f"issuecomment-{cid}")], comment=cid))
    return _result("S0", candidates, {"checked": checked, "mismatched": len(candidates)})


# ---------------------------------------------------------------------------- S1

def signal_s1(product: Product, th: Dict[str, int]) -> Dict[str, Any]:
    """S1 계획 대비 진행: orphan merges and direct pushes, completion claims that the host does not hold."""
    missing = product.missing_for("S1")
    if missing:
        return _unknown_result("S1", missing)
    repo = product.repository
    prs, pushes = product.orphans()
    count = len(prs) + len(pushes)
    level = AT_RISK if count >= th["orphan_at_risk"] else (WATCH if count >= th["orphan_watch"] else ON_TRACK)
    candidates = []
    if level != ON_TRACK:
        for pr in sorted(prs, key=lambda p: _int(p.get("number")) or 0):
            number = _int(pr.get("number"))
            candidates.append(_candidate(
                "S1", repo, f"pr:{repo}#{_t_num(number)}", level, "GH_SYSTEM", "orphan_pr",
                [_ev("pr", repo, number=number), _ev("commit", repo, sha=pr.get("merge_commit_sha"))],
                number=_t_num(number), when=_t_when(_time(pr.get("merged_at"))), count=str(count)))
        for push in sorted(pushes, key=lambda c: str(c.get("sha"))):
            sha = push.get("sha")
            candidates.append(_candidate(
                "S1", repo, f"commit:{repo}@{_t_sha7(sha)}", level, "GH_SYSTEM", "direct_push",
                [_ev("commit", repo, sha=sha)], sha=_t_sha7(sha), when=_t_when(_time(push.get("date"))),
                count=str(count)))
    for node_id in product.ids:
        issue = product.issue(node_id)
        if (issue.get("state") == "closed" and issue.get("state_reason") == "completed"
                and product.stage[node_id] != "DONE"):
            number = _int(issue.get("number"))
            candidates.append(_candidate(
                "S1", repo, f"node:{node_id}", WATCH, "GH_TEXT", "completion_mismatch",
                [_ev("issue", repo, number=number)], node=_t_node(node_id), issue=_t_num(number),
                stage=STAGE_WORDS.get(product.stage[node_id], "?")))
    ladder = ladder_counts(product)
    value = {key: ladder[key] for key in STAGE_KEYS.values()}
    value.update(merge_checks_pass=ladder["merge_checks"]["PASS"], merge_checks_fail=ladder["merge_checks"]["FAIL"],
                 merge_checks_pending=ladder["merge_checks"]["PENDING"], deployed=NOT_RECORDED,
                 verified=NOT_RECORDED, pending_plan_prs=ladder["pending_plan_prs"], orphans=count,
                 unknown_nodes=ladder["unknown_nodes"])
    return _result("S1", candidates, value)


# ---------------------------------------------------------------------------- S2

def review_pin(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    return cp_program.pin_of(row, "REVIEW") if row.get("role") == "REVIEWER" else None


def signal_s2(product: Product, th: Dict[str, int]) -> Dict[str, Any]:
    """S2 반복 실패 (HOST): writer resumes, review FAILs at the delivered head, review session cap, prestarts."""
    missing = product.missing_for("S2")
    if missing:
        return _unknown_result("S2", missing)
    repo = product.repository
    candidates = []
    per_task = {}
    for node_id in product.ids:
        rows = product.rows(node_id)
        if not rows:
            continue
        task = product.task_id(node_id)
        subject = f"task:{task}"
        host_ev = _ev("host", repo, ref=task)
        writers = cp_program.writer_rows(rows)
        resumes = max(0, len(writers) - 1)
        writer = cp_program.current_writer(rows)
        delivery = cp_program.pin_of(writer, "DELIVERY")
        head = delivery.get("head") if delivery else None
        # CONTRACT NOTE: "review FAIL pins at the current delivered head" counts every host REVIEW pin with
        # verdict FAIL whose head equals the current writer's delivered head (any writer attempt, any slot).
        fails = 0
        if isinstance(head, str):
            fails = sum(1 for r in rows if (review_pin(r) or {}).get("verdict") == "FAIL"
                        and review_pin(r).get("head") == head)
        sessions = 0
        if isinstance(head, str) and isinstance(writer.get("launch_request_id"), str):
            for slot in (1, 2):
                rid = cp_program.review_request_id(repo, task, writer["launch_request_id"], head, slot)
                count = len([r for r in cp_program.review_rows(rows, rid) if r.get("state") != "FAILED_PRESTART"])
                sessions = max(sessions, count)
        prestarts = sum(1 for r in rows if r.get("state") == "FAILED_PRESTART")
        per_task[task] = {"resumes": resumes, "review_fail_at_head": fails, "review_sessions_max": sessions,
                          "failed_prestart": prestarts}
        level = AT_RISK if resumes >= th["resumes_at_risk"] else (WATCH if resumes >= th["resumes_watch"] else None)
        sub_candidates = []
        if level:
            sub_candidates.append((level, "resumes", dict(count=str(resumes), watch=str(th["resumes_watch"]),
                                                          risk=str(th["resumes_at_risk"]))))
        level = (AT_RISK if fails >= th["review_fail_at_risk"]
                 else (WATCH if fails >= th["review_fail_watch"] else None))
        if level:
            sub_candidates.append((level, "review_fail", dict(sha=_t_sha7(head), count=str(fails),
                                                              watch=str(th["review_fail_watch"]),
                                                              risk=str(th["review_fail_at_risk"]))))
        if sessions >= cp_program.MAX_REVIEW_SESSIONS:
            sub_candidates.append((AT_RISK, "review_sessions", dict(sha=_t_sha7(head), count=str(sessions),
                                                                    limit=str(cp_program.MAX_REVIEW_SESSIONS))))
        if prestarts >= th["prestart_watch"]:
            sub_candidates.append((WATCH, "prestart", dict(count=str(prestarts), watch=str(th["prestart_watch"]))))
        if sub_candidates:
            # One finding per task: the worst condition names it; the others are in the value.
            sub_candidates.sort(key=lambda item: -LEVEL_RANK[item[0]])
            level, template, fields = sub_candidates[0]
            evidence = [host_ev]
            if delivery and _int(delivery.get("pr")):
                evidence.append(_ev("pr", repo, number=delivery["pr"]))
            candidates.append(_candidate("S2", repo, subject, level, "HOST", template, evidence,
                                         task=_t_task(task), **fields))
    return _result("S2", candidates, {"tasks": per_task})


# ---------------------------------------------------------------------------- S3

def signal_s3(product: Product, th: Dict[str, int]) -> Dict[str, Any]:
    """S3 작은 예외 누적 (GH_TEXT, claimed, capped at WATCH)."""
    missing = product.missing_for("S3")
    if missing:
        return _unknown_result("S3", missing)
    repo = product.repository
    candidates = []
    total = 0
    per_node = {}
    for node_id in product.ids:
        count = _int(product.node(node_id).get("exceptions_14d")) or 0
        per_node[node_id] = count
        total += count
        if count >= th["exceptions_node_watch"]:
            number = _int(product.issue(node_id).get("number"))
            candidates.append(_candidate("S3", repo, f"node:{node_id}", WATCH, "GH_TEXT", "exceptions_node",
                                         [_ev("issue", repo, number=number)], node=_t_node(node_id),
                                         count=str(count), watch=str(th["exceptions_node_watch"])))
    if total >= th["exceptions_product_watch"]:
        issues = [_ev("issue", repo, number=product.issue(n).get("number")) for n in product.ids if per_node[n]]
        candidates.append(_candidate("S3", repo, f"plan:{repo}", WATCH, "GH_TEXT", "exceptions_product",
                                     issues[:10], count=str(total),
                                     watch=str(th["exceptions_product_watch"])))
    return _result("S3", candidates, {"total_14d": total, "per_node": per_node}, basis="GH_TEXT")


# ---------------------------------------------------------------------------- S4

def _touches(files: Any, path: str) -> bool:
    for item in _list(files):
        if not isinstance(item, dict):
            continue
        for name in (item.get("filename"), item.get("previous_filename")):
            if isinstance(name, str) and (name == path or name.startswith(path.rstrip("/") + "/")):
                return True
    return False


def signal_s4(product: Product, products: Dict[str, Product], pairs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """S4 계약 정합 (GH_SYSTEM): one side of a configured contract pair changed in 7 days, its partner did not."""
    involved = []
    for index, pair in enumerate(pairs):
        if not isinstance(pair, dict):
            continue
        sides = [_dict(pair.get("a")), _dict(pair.get("b"))]
        for me, other in ((0, 1), (1, 0)):
            if sides[me].get("repository") == product.repository:
                involved.append((index, sides[me], sides[other]))
    if not involved:
        return _result("S4", [], "NOT_CONFIGURED", level=NOT_CONFIGURED)
    missing = product.missing_for("S4")
    candidates = []
    value = []
    for index, mine, theirs in involved:
        partner = products.get(theirs.get("repository"))
        partner_missing = ["partner"] if partner is None else partner.missing_for("S4")
        if missing or partner_missing:
            return _unknown_result("S4", list(missing) + list(partner_missing))
        path, partner_path = mine.get("path"), theirs.get("path")
        if not isinstance(path, str) or not isinstance(partner_path, str):
            continue
        changed = sorted({_int(pr.get("number")) for pr in _list(product.facts.get("merged_7d"))
                          if isinstance(pr, dict) and _int(pr.get("number")) and _touches(pr.get("files"), path)})
        partner_changed = any(isinstance(pr, dict) and _touches(pr.get("files"), partner_path)
                              for pr in _list(partner.facts.get("merged_7d")))
        value.append({"pair": index, "changed_prs": changed, "partner_changed": partner_changed})
        # CONTRACT NOTE: "within 7 days" is read as the observed 7-day merge window of both repositories.
        if changed and not partner_changed:
            candidates.append(_candidate(
                "S4", product.repository, f"pair:{index}", WATCH, "GH_SYSTEM", "contract_pair",
                [_ev("pr", product.repository, number=n) for n in changed[:10]],
                pair=str(index), repo=_t_repo(product.repository), partner=_t_repo(theirs.get("repository")),
                prs=", ".join(f"#{n}" for n in changed[:10])))
    return _result("S4", candidates, value)


# ---------------------------------------------------------------------------- S5

def signal_s5(product: Product, th: Dict[str, int], test_regex: "re.Pattern[str]", now: datetime) -> Dict[str, Any]:
    """S5 증거 약화: test removal, workflow edits, post-merge required checks, shrunk required check list."""
    missing = product.missing_for("S5")
    if missing:
        return _unknown_result("S5", missing)
    repo = product.repository
    candidates = []
    for pr in sorted((p for p in _list(product.facts.get("merged_7d")) if isinstance(p, dict)),
                     key=lambda p: _int(p.get("number")) or 0):
        if pr.get("pinned") is not True:
            continue
        number = _int(pr.get("number"))
        files = [f for f in _list(pr.get("files")) if isinstance(f, dict)]
        removed = 0
        workflows = 0
        for item in files:
            name, previous, status = item.get("filename"), item.get("previous_filename"), item.get("status")
            name = name if isinstance(name, str) else ""
            previous = previous if isinstance(previous, str) else ""
            if status == "removed" and test_regex.search(name):
                removed += 1
            elif status == "renamed" and previous and test_regex.search(previous) and not test_regex.search(name):
                removed += 1
            if name.startswith(WORKFLOWS_DIR) or previous.startswith(WORKFLOWS_DIR):
                workflows += 1
        evidence = [_ev("pr", repo, number=number)]
        if removed:
            candidates.append(_candidate("S5", repo, f"pr:{repo}#{_t_num(number)}", WATCH, "GH_SYSTEM",
                                         "tests_removed", evidence, number=_t_num(number), count=str(removed)))
        if workflows:
            # CONTRACT NOTE: a second subject form for the same PR would collide; workflow edits use
            # "pr:<repo>#<n>/workflows" so both findings keep their own ids.
            candidates.append(_candidate("S5", repo, f"pr:{repo}#{_t_num(number)}/workflows", WATCH, "GH_SYSTEM",
                                         "workflows_touched", evidence, number=_t_num(number),
                                         count=str(workflows)))
    for node_id in product.ids:
        if product.stage[node_id] != "DONE":
            continue
        node = product.node(node_id)
        state = _dict(node.get("completion")).get("merge_checks")
        pr = _dict(node.get("delivery_pr"))
        sha = pr.get("merge_commit_sha")
        evidence = [_ev("commit", repo, sha=sha), _ev("pr", repo, number=pr.get("number"))]
        if state == "FAIL":
            candidates.append(_candidate("S5", repo, f"node:{node_id}", AT_RISK, "GH_SYSTEM", "merge_checks_fail",
                                         evidence, node=_t_node(node_id), sha=_t_sha7(sha)))
        elif state == "PENDING":
            merged_at = _time(pr.get("merged_at"))
            hours = _hours(now, merged_at)
            if hours is not None and hours >= th["pending_checks_watch_h"]:
                candidates.append(_candidate("S5", repo, f"node:{node_id}", WATCH, "GH_SYSTEM",
                                             "merge_checks_pending", evidence, node=_t_node(node_id),
                                             sha=_t_sha7(sha), when=_t_when(merged_at),
                                             hours=str(th["pending_checks_watch_h"])))
    required = product.facts.get("required_checks")
    configured = isinstance(required, list) and bool(required)
    if product.facts.get("required_checks_shrank") is True:
        candidates.append(_candidate("S5", repo, f"plan:{repo}", AT_RISK, "SHA_CONTENT", "checks_shrank",
                                     [_ev("host", repo, ref="required_checks")],
                                     count=str(len(required) if isinstance(required, list) else 0)))
    value = {"required_checks": "CONFIGURED" if configured else NOT_CONFIGURED}
    return _result("S5", candidates, value)


# ---------------------------------------------------------------------------- S6 and the plan DAG

def critical_path(ids: Sequence[str], deps: Dict[str, List[str]], remaining: Iterable[str]) -> List[str]:
    """Longest path (node count) in the plan DAG restricted to ``remaining``; ties: lexicographic ids."""
    alive = set(remaining)
    children: Dict[str, List[str]] = {n: [] for n in ids}
    for node, parents in deps.items():
        for parent in parents:
            if parent in children:
                children[parent].append(node)
    memo: Dict[str, List[str]] = {}
    visiting: Set[str] = set()

    def best_from(node: str) -> List[str]:
        if node in memo:
            return memo[node]
        if node in visiting:  # a cycle (never in a valid plan): stop here
            return [node]
        visiting.add(node)
        best: List[str] = []
        for child in sorted(children.get(node, [])):
            if child in alive:
                path = best_from(child)
                if len(path) > len(best) or (len(path) == len(best) and path < best):
                    best = path
        visiting.discard(node)
        memo[node] = [node] + best
        return memo[node]

    winner: List[str] = []
    for node in sorted(alive):
        if node not in children:
            continue
        path = best_from(node)
        if len(path) > len(winner) or (len(path) == len(winner) and path < winner):
            winner = path
    return winner


def dag_layout(ids: Sequence[str], deps: Dict[str, List[str]]) -> Dict[str, Tuple[int, int]]:
    """{node: (depth, row)}: depth = longest-path depth from the roots, row = order by id within a depth."""
    depth: Dict[str, int] = {}
    visiting: Set[str] = set()

    def depth_of(node: str) -> int:
        if node in depth:
            return depth[node]
        if node in visiting:
            return 0
        visiting.add(node)
        value = max((depth_of(p) + 1 for p in deps.get(node, []) if p in deps), default=0)
        visiting.discard(node)
        depth[node] = value
        return value

    for node in ids:
        depth_of(node)
    layout: Dict[str, Tuple[int, int]] = {}
    for level in sorted(set(depth.values())):
        for row, node in enumerate(sorted(n for n in ids if depth[n] == level)):
            layout[node] = (level, row)
    return layout


def _blocked_since(product: Product, node_id: str) -> Optional[datetime]:
    """Since when the node's open issue carries a blocking label (GH_TEXT), else None."""
    issue = product.issue(node_id)
    # CONTRACT NOTE: a closed issue does not block work, so its leftover labels are ignored.
    if issue.get("state") == "closed" or not any(label in BLOCKING_LABELS for label in _labels(issue)):
        return None
    return _time(product.node(node_id).get("blocked_since"))


def signal_s6(product: Product, th: Dict[str, int], now: datetime) -> Dict[str, Any]:
    """S6 정체: host UNKNOWN/SUBMITTING, blocked critical-path nodes, days since the last DONE."""
    missing = product.missing_for("S6")
    if missing:
        return _unknown_result("S6", missing)
    repo = product.repository
    candidates = []
    remaining = product.remaining()
    path = critical_path(product.ids, product.deps, remaining)
    for node_id in product.ids:
        status = _dict(product.node(node_id).get("materialization")).get("status")
        what = state = None
        if status in ("UNKNOWN", "SUBMITTING"):
            what, state = "생성", status
        elif any(r.get("role") == "WRITER" and r.get("state") == "UNKNOWN" for r in product.rows(node_id)):
            what, state = "작성 세션", "UNKNOWN"
        if what:
            candidates.append(_candidate("S6", repo, f"node:{node_id}", AT_RISK, "HOST", "host_unknown",
                                         [_ev("host", repo, ref=product.task_id(node_id))], node=_t_node(node_id),
                                         what=what, state=state))
    for node_id in path:
        since = _blocked_since(product, node_id)
        hours = _hours(now, since)
        if hours is not None and hours >= th["blocked_watch_h"]:
            number = _int(product.issue(node_id).get("number"))
            candidates.append(_candidate("S6", repo, f"node:{node_id}", WATCH, "GH_TEXT", "cp_blocked",
                                         [_ev("issue", repo, number=number)], node=_t_node(node_id),
                                         when=_t_when(since), hours=str(th["blocked_watch_h"])))
    done_times = [_time(_dict(product.node(n).get("delivery_pr")).get("merged_at")) for n in product.ids
                  if product.done(n)]
    done_times = [t for t in done_times if t is not None]
    last_done = max(done_times) if done_times else _time(product.plan.get("committed_at"))
    materialized = any(product.stage[n] not in ("PLANNED", UNKNOWN) for n in product.ids)
    days = None
    if last_done is not None:
        days = max(0.0, (now - last_done).total_seconds() / 86400.0)
        if remaining and materialized:
            level = (AT_RISK if days >= th["done_gap_at_risk_d"]
                     else (WATCH if days >= th["done_gap_watch_d"] else None))
            if level:
                candidates.append(_candidate("S6", repo, f"plan:{repo}", level, "GH_SYSTEM", "done_gap",
                                             [_ev("commit", repo, sha=product.plan.get("plan_commit"))],
                                             when=_t_when(last_done), days=str(int(days)), count=str(len(remaining))))
    # One subject may appear twice (host state and blocked label): keep the more severe one.
    by_subject: Dict[str, Dict[str, Any]] = {}
    for candidate in candidates:
        kept = by_subject.get(candidate["subject_key"])
        if kept is None or LEVEL_RANK[candidate["severity"]] > LEVEL_RANK[kept["severity"]]:
            by_subject[candidate["subject_key"]] = candidate
    candidates = [c for c in candidates if by_subject[c["subject_key"]] is c]
    value = {"critical_path": path, "remaining": len(remaining),
             "days_since_done": None if days is None else round(days, 2),
             "last_done_at": core.iso(last_done) if last_done else None}
    return _result("S6", candidates, value)


# ---------------------------------------------------------------------------- S7 (CTRL) and idle-while-waiting

def lanes_view(facts: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The host lanes fact group when well formed, else None (S7 UNKNOWN)."""
    lanes = facts.get("lanes")
    if (not isinstance(lanes, dict) or not isinstance(lanes.get("lanes"), list)
            or _int(lanes.get("active_total")) is None or _int(lanes.get("max_active_sessions")) is None):
        return None
    if "lanes" in {g for g in _list(facts.get("unknown")) if isinstance(g, str)}:
        return None
    return lanes


def idle_lanes(lanes: Dict[str, Any]) -> List[str]:
    """Enabled lanes with no active row, counted only while the host has free session capacity."""
    if lanes["active_total"] >= lanes["max_active_sessions"]:
        return []
    out = []
    for lane in lanes["lanes"]:
        if isinstance(lane, dict) and lane.get("lane") in LANE_ORDER and lane.get("enabled") is True \
                and not _list(lane.get("active")):
            out.append(lane["lane"])
    return sorted(out, key=LANE_ORDER.index)


def idle_point(facts: Dict[str, Any], products: Dict[str, Product], now: datetime) -> Optional[Dict[str, Any]]:
    """This snapshot's idle lanes and ready-to-start node count. ``partial`` when a product could not be read."""
    lanes = lanes_view(facts)
    if lanes is None:
        return None
    waiting = 0
    partial = False
    for product in products.values():
        if product.paused:
            continue
        if product.missing_for("S6") or product.plan_state != "PRESENT":
            partial = True
            continue
        waiting += len(product.waiting())
    idle = idle_lanes(lanes)
    return {"t": core.iso(now), "idle": len(idle), "idle_lanes": idle, "waiting": waiting, "partial": partial}


def _idle_hit(point: Dict[str, Any]) -> bool:
    """Idle lanes and waiting work seen together. ``waiting`` is a lower bound when the point is partial,
    so a partial point can still prove a hit; it can never prove a miss."""
    return point.get("idle", 0) > 0 and point.get("waiting", 0) > 0


def idle_waiting_hit(facts: Dict[str, Any], now: datetime) -> bool:
    """Whether one facts snapshot is an idle-while-waiting point (S7). The facts module uses it to
    schedule the next T1 while such a run is still too short to count."""
    products = {repo: Product(repo, data) for repo, data in _dict(facts.get("products")).items()
                if isinstance(repo, str) and REPO_RE.fullmatch(repo) and isinstance(data, dict)}
    point = idle_point(facts, products, core.utc(now))
    return point is not None and _idle_hit(point)


def _idle_run(series: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The trailing run of consecutive idle-while-waiting points (newest last)."""
    run: List[Dict[str, Any]] = []
    for point in reversed(series):
        if not _idle_hit(point):
            break
        run.append(point)
    return list(reversed(run))


def signal_s7(facts: Dict[str, Any], products: Dict[str, Product], th: Dict[str, int], now: datetime,
              series: List[Dict[str, Any]], control_repository: str) -> Dict[str, Any]:
    """S7 레인 (CTRL): long CONFIRMED sessions, lingering needs-lane-cleanup, idle lanes while work waits.

    Front-loading on DEVIN is never a signal: lanes are filled in a fixed order.
    """
    lanes = lanes_view(facts)
    if lanes is None:
        return _unknown_result("S7", ["lanes"])
    candidates = []
    for lane in lanes["lanes"]:
        if not isinstance(lane, dict) or lane.get("lane") not in LANE_ORDER:
            continue
        for row in _list(lane.get("active")):
            if not isinstance(row, dict) or row.get("state") != "CONFIRMED":
                continue
            since = _time(row.get("created"))
            hours = _hours(now, since)
            if hours is None:
                continue
            # Reached, not exceeded: the same boundary as the facts buckets and next_recheck_at, so the T1
            # scheduled at exactly start + threshold raises the level (no one-tick or one-day delay).
            level = (AT_RISK if hours >= th["confirmed_at_risk_h"]
                     else (WATCH if hours >= th["confirmed_watch_h"] else None))
            if level:
                limit = th["confirmed_at_risk_h"] if level == AT_RISK else th["confirmed_watch_h"]
                candidates.append(_candidate("S7", CTRL, f"lane:{lane['lane']}", level, "HOST", "confirmed_age",
                                             [_ev("host", row.get("repository"), ref=_t_task(row.get("task")))],
                                             lane=_t_lane(lane["lane"]), task=_t_task(row.get("task")),
                                             when=_t_when(since), hours=str(limit)))
    unknown_repositories = []
    for repo in sorted(products):
        product = products[repo]
        foreign = {g for g in product.unknown_groups if g not in KNOWN_GROUPS}
        if foreign or product.unknown_groups & set(S7_PRODUCT_DEPS):
            unknown_repositories.append(repo)
            continue
        for node_id in product.ids:
            issue = product.issue(node_id)
            if issue.get("state") == "closed" or CLEANUP_LABEL not in _labels(issue):
                continue
            # CONTRACT NOTE: the age is that of needs-lane-cleanup itself (facts label_since), not
            # blocked_since (the earliest of all blocking labels); facts without label_since fall back
            # to blocked_since.
            node = product.node(node_id)
            label_since = node.get("label_since")
            since = _time(label_since.get(CLEANUP_LABEL) if isinstance(label_since, dict)
                          else node.get("blocked_since"))
            hours = _hours(now, since)
            if hours is not None and hours >= th["cleanup_watch_h"]:
                task = product.task_id(node_id)
                candidates.append(_candidate("S7", CTRL, f"task:{task}", WATCH, "GH_TEXT", "cleanup_label",
                                             [_ev("issue", repo, number=issue.get("number"))], task=_t_task(task),
                                             when=_t_when(since), hours=str(th["cleanup_watch_h"])))
    unknown_subjects = []
    run = _idle_run(series)
    current = series[-1] if series else None
    if current is not None and current.get("partial") and not _idle_hit(current):
        unknown_subjects.append("lane:IDLE")
    if len(run) >= th["idle_waiting_snapshots"]:
        start, end = _time(run[0]["t"]), _time(run[-1]["t"])
        span = _hours(end, start) if start and end else None
        if span is not None and span >= th["idle_waiting_min_span_h"]:
            last = run[-1]
            # CONTRACT NOTE: idle-while-waiting is one CTRL subject "lane:IDLE" (it concerns the lane set).
            candidates.append(_candidate("S7", CTRL, "lane:IDLE", WATCH, "HOST", "idle_waiting",
                                         [_ev("host", control_repository, ref="lanes")],
                                         lanes=", ".join(_t_lane(x) for x in last.get("idle_lanes", [])) or "?",
                                         count=str(last.get("waiting", 0)), snapshots=str(len(run)),
                                         start=_t_when(start), end=_t_when(end)))
    value = {"active_total": lanes["active_total"], "max_active_sessions": lanes["max_active_sessions"],
             "idle_lanes": current.get("idle_lanes", []) if current else [],
             "waiting": current.get("waiting", 0) if current else 0, "idle_waiting_run": len(run)}
    return _result("S7", candidates, value, unknown_repositories=unknown_repositories,
                   unknown_subjects=unknown_subjects)


# ---------------------------------------------------------------------------- S8, S9

def signal_s8() -> Dict[str, Any]:
    """S8 결정 연속성: model stage only (PR2)."""
    return _result("S8", [], S8_VALUE, level=NOT_CONFIGURED)


def cursor_rows(product: Product) -> List[Dict[str, Any]]:
    out = []
    for node_id in product.ids:
        for row in product.rows(node_id):
            if row.get("lane") == "CURSOR" and row.get("role") in ("WRITER", "REVIEWER"):
                out.append({"task": _t_task(product.task_id(node_id)), "role": row.get("role"),
                            "state": _ok(row.get("state"), WORD_RE) or "?",
                            "attempt": _int(row.get("attempt_id")),
                            "reserved_at": row.get("reserved_at") if _time(row.get("reserved_at")) else None})
    return out


def signal_s9(product: Product) -> Dict[str, Any]:
    """S9 CURSOR canary (HOST): CURSOR review passes another lane's FAIL at the same head; prestart failures."""
    missing = product.missing_for("S9")
    if missing:
        return _unknown_result("S9", missing)
    repo = product.repository
    candidates = []
    prestarts = 0
    for node_id in product.ids:
        rows = product.rows(node_id)
        task = product.task_id(node_id)
        prestarts += sum(1 for r in rows if r.get("lane") == "CURSOR" and r.get("state") == "FAILED_PRESTART")
        passes: Dict[str, str] = {}
        fails: Dict[str, str] = {}
        for row in rows:
            pin = review_pin(row)
            if not pin or not isinstance(pin.get("head"), str):
                continue
            if row.get("lane") == "CURSOR" and pin.get("verdict") in ("PASS", "PASS_WITH_NOTES"):
                passes.setdefault(pin["head"], pin["verdict"])
            elif row.get("lane") != "CURSOR" and pin.get("verdict") == "FAIL":
                fails.setdefault(pin["head"], row.get("lane"))
        for head in sorted(set(passes) & set(fails)):
            candidates.append(_candidate("S9", repo, f"task:{task}", WATCH, "HOST", "cursor_canary",
                                         [_ev("host", repo, ref=_t_task(task)), _ev("commit", repo, sha=head)],
                                         task=_t_task(task), verdict=VERDICT_WORDS[passes[head]],
                                         sha=_t_sha7(head), lane=_t_lane(fails[head])))
            break
    if prestarts >= 2:
        candidates.append(_candidate("S9", repo, "lane:CURSOR", WATCH, "HOST", "cursor_prestart",
                                     [_ev("host", repo, ref="CURSOR")], count=str(prestarts)))
    return _result("S9", candidates, {"rows": cursor_rows(product), "failed_prestart": prestarts})


# ---------------------------------------------------------------------------- evaluation

def product_verdict(product: Product, signals: Dict[str, Dict[str, Any]]) -> str:
    """max over the product's signals (UNKNOWN/NOT_CONFIGURED ignored); PAUSED with no valid plan."""
    if product.paused:
        return PAUSED
    levels = [s["level"] for s in signals.values()]
    if not any(level in LEVEL_RANK for level in levels):
        # CONTRACT NOTE: a product with no evaluable signal at all shows UNKNOWN instead of ON_TRACK.
        return UNKNOWN if UNKNOWN in levels else ON_TRACK
    return max_level(levels)


def _config_order(config: Dict[str, Any], repositories: Iterable[str]) -> List[str]:
    order = [t.get("repository") for t in _list(config.get("targets")) if isinstance(t, dict)]
    known = set(repositories)
    ordered = [r for r in order if r in known]
    return ordered + sorted(known - set(ordered))


def evaluate(facts: Dict[str, Any], config: Optional[Dict[str, Any]], now: datetime, *,
             journal: Any = None, state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """All signals, verdicts and finding candidates for one committed T1 snapshot (pure)."""
    if not isinstance(facts, dict) or facts.get("schema") != "AIOPS_INSPECT_FACTS_V1":
        raise core.InspectError("FACTS_SCHEMA", "not an AIOPS_INSPECT_FACTS_V1 document")
    config = config or {}
    now = core.utc(now)
    th = thresholds(config.get("thresholds"))
    regex_text = config.get("test_path_regex") or core.DEFAULT_TEST_PATH_REGEX
    test_regex = re.compile(regex_text)
    pairs = _list(config.get("contract_pairs"))
    # CONTRACT NOTE: evidence for CTRL findings names config control_repository (the facts carry none).
    control_repository = config.get("control_repository")
    if not isinstance(control_repository, str) or not REPO_RE.fullmatch(control_repository):
        control_repository = DEFAULT_CONTROL_REPOSITORY
    state = state if isinstance(state, dict) else {}

    products = {repo: Product(repo, data) for repo, data in _dict(facts.get("products")).items()
                if isinstance(repo, str) and REPO_RE.fullmatch(repo) and isinstance(data, dict)}
    for target in _list(config.get("targets")):
        target = _dict(target)
        product = products.get(target.get("repository"))
        if product is not None and product.prefix == "XXXX" and _ok(target.get("prefix"), PREFIX_RE):
            product.prefix = target["prefix"]
    order = _config_order(config, products)
    out_products: Dict[str, Any] = {}
    candidates: List[Dict[str, Any]] = []
    for repo in order:
        product = products[repo]
        if product.paused:
            signals = {s: _result(s, [], None, level=PAUSED) for s in PRODUCT_SIGNALS}
        else:
            signals = {
                "S1": signal_s1(product, th), "S2": signal_s2(product, th), "S3": signal_s3(product, th),
                "S4": (signal_s4(product, products, pairs) if pairs
                       else _result("S4", [], "NOT_CONFIGURED", level=NOT_CONFIGURED)),
                "S5": signal_s5(product, th, test_regex, now), "S6": signal_s6(product, th, now),
                "S8": signal_s8(), "S9": signal_s9(product),
            }
        for result in signals.values():
            candidates.extend(result["candidates"])
        out_products[repo] = {"prefix": product.prefix, "name": short_name(repo, products),
                              "plan_state": product.plan_state, "paused": product.paused,
                              "verdict": product_verdict(product, signals), "signals": signals,
                              "ladder": ladder_counts(product), "unknown_groups": sorted(product.unknown_groups)}

    series = [p for p in _list(state.get("idle_series")) if isinstance(p, dict) and _time(p.get("t"))]
    point = idle_point(facts, products, now)
    if point is not None:
        series = [p for p in series if _time(p["t"]) < now] + [point]
    series = [p for p in series if now - _time(p["t"]) <= IDLE_SERIES_KEEP][-MAX_IDLE_POINTS:]
    ctrl_signals = {"S0": signal_s0(facts, journal, control_repository),
                    "S7": signal_s7(facts, products, th, now, series if point is not None else [],
                                    control_repository)}
    for result in ctrl_signals.values():
        candidates.extend(result["candidates"])
    ctrl_levels = [s["level"] for s in ctrl_signals.values()]
    ctrl_verdict = (max_level(ctrl_levels) if any(level in LEVEL_RANK for level in ctrl_levels) else UNKNOWN)

    info = []
    cursor_first = state.get("cursor_first") if isinstance(state.get("cursor_first"), dict) else None
    if cursor_first is None:
        for repo in order:
            rows = cursor_rows(products[repo])
            if rows:
                first = sorted(rows, key=lambda r: (r.get("reserved_at") or "", r["task"]))[0]
                cursor_first = {"repository": repo, "task": first["task"], "t": core.iso(now)}
                info.append({"kind": "cursor_first", "text_ko": f"CURSOR 첫 작업 관측: {_t_repo(repo)} "
                                                                f"작업 {first['task']} ({core.fmt_kst(now)})"})
                break

    return {"schema": EVALUATION_SCHEMA, "at": core.iso(now), "thresholds": th, "order": order,
            "products": out_products,
            "ctrl": {"prefix": CTRL, "verdict": ctrl_verdict, "signals": ctrl_signals},
            "candidates": candidates, "idle_series": series, "cursor_first": cursor_first, "info": info}


def verdicts_of(evaluation: Dict[str, Any]) -> Dict[str, str]:
    """{repository or CTRL: verdict}."""
    out = {repo: p["verdict"] for repo, p in evaluation["products"].items()}
    out[CTRL] = evaluation["ctrl"]["verdict"]
    return out


# ---------------------------------------------------------------------------- findings lifecycle

def new_state() -> Dict[str, Any]:
    return {"schema": STATE_SCHEMA, "counters": {}, "findings": {}, "idle_series": [], "cursor_first": None,
            "verdicts": {}, "updated_at": None}


def _check_state(state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if state is None:
        return new_state()
    if (not isinstance(state, dict) or state.get("schema") != STATE_SCHEMA
            or not isinstance(state.get("counters"), dict) or not isinstance(state.get("findings"), dict)):
        # Never start over silently: a lost counter would reuse finding ids.
        raise core.InspectError("STATE_FINDINGS", "findings state is malformed")
    return state


def _evaluated(evaluation: Dict[str, Any], finding: Dict[str, Any]) -> bool:
    """Was this finding's signal evaluated (not UNKNOWN/PAUSED) for its product and inputs this tick?"""
    product, signal = finding.get("product"), finding.get("signal")
    if product == CTRL:
        result = evaluation["ctrl"]["signals"].get(signal)
    else:
        entry = evaluation["products"].get(product)
        result = entry["signals"].get(signal) if entry else None
    # NOT_CONFIGURED is a definite answer (the signal is off), so its old findings may resolve.
    if not isinstance(result, dict) or result.get("unknown") or \
            (result.get("level") not in LEVEL_RANK and result.get("level") != NOT_CONFIGURED):
        return False
    if finding.get("subject_key") in result.get("unknown_subjects", []):
        return False
    if finding.get("repository") in result.get("unknown_repositories", []):
        return False
    return True


def _prefix_for(evaluation: Dict[str, Any], product: str) -> str:
    if product == CTRL:
        return CTRL
    entry = evaluation["products"].get(product)
    return entry["prefix"] if entry else "XXXX"


def update_findings(state: Optional[Dict[str, Any]], evaluation: Dict[str, Any],
                    now: datetime) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Apply one committed snapshot's candidates to the findings state. Returns (new state, changes).

    present: NEW (first time) / OPEN / WORSENED (severity up) / REOPENED (was RESOLVED);
    absent: RESOLVED after RESOLVE_AFTER consecutive evaluated absences; UNKNOWN inputs freeze it.
    """
    state = _check_state(state)
    stamp = core.iso(now)
    out = {**state, "counters": dict(state["counters"]),
           "findings": {k: dict(v) for k, v in state["findings"].items() if isinstance(v, dict)}}
    findings = out["findings"]
    for record in findings.values():
        match = ID_RE.fullmatch(str(record.get("id")))
        if match:  # counters never fall behind an id already issued
            out["counters"][match.group(1)] = max(out["counters"].get(match.group(1), 0), int(match.group(2)))
        record["tick_change"] = None
    changes = []
    present = {}
    for candidate in evaluation["candidates"]:
        key = finding_key(candidate["signal"], candidate["product"], candidate["subject_key"])
        kept = present.get(key)
        if kept is None or LEVEL_RANK[candidate["severity"]] > LEVEL_RANK[kept["severity"]]:
            present[key] = candidate
    for key, cand in sorted(present.items()):
        record = findings.get(key)
        repository = next((e.get("repository") for e in cand["evidence"] if e.get("repository")), None)
        body = {"signal": cand["signal"], "product": cand["product"], "subject_key": cand["subject_key"],
                "severity": cand["severity"], "basis": cand["basis"], "title_ko": cand["title_ko"],
                "detail_ko": cand["detail_ko"], "evidence": cand["evidence"],
                "repository": cand["product"] if cand["product"] != CTRL else repository}
        if record is None:
            prefix = _prefix_for(evaluation, cand["product"])
            number = out["counters"].get(prefix, 0) + 1
            out["counters"][prefix] = number
            record = {"id": f"INS-{prefix}-{number:04d}", "key": key, "prefix": prefix, "first_seen": stamp,
                      "resolved_at": None, "ack": None}
            change = "NEW"
        elif record.get("state") == "RESOLVED":
            change = "REOPENED"
        elif LEVEL_RANK.get(cand["severity"], 0) > LEVEL_RANK.get(record.get("severity"), 0):
            change = "WORSENED"
        else:
            change = None
        record.update(body)
        record.update(state=change or "OPEN", tick_change=change, last_seen=stamp, absent_count=0, unknown=False,
                      resolved_at=None)
        if change:
            record["changed_at"] = stamp
            changes.append({"id": record["id"], "key": key, "change": change, "severity": record["severity"]})
        findings[key] = record
    for key, record in sorted(findings.items()):
        if key in present or record.get("state") == "RESOLVED":
            continue
        if not _evaluated(evaluation, record):
            # CONTRACT NOTE: an UNKNOWN tick freezes the finding and restarts the absence count, so two
            # evaluated absences must be consecutive committed snapshots with no gap in between.
            record.update(unknown=True, absent_count=0)
            if record.get("state") in ("NEW", "WORSENED", "REOPENED"):
                record["state"] = "OPEN"
            continue
        record["unknown"] = False
        record["absent_count"] = int(record.get("absent_count") or 0) + 1
        if record["absent_count"] >= RESOLVE_AFTER:
            record.update(state="RESOLVED", tick_change="RESOLVED", resolved_at=stamp, changed_at=stamp)
            changes.append({"id": record["id"], "key": key, "change": "RESOLVED", "severity": record["severity"]})
        elif record.get("state") in ("NEW", "WORSENED", "REOPENED"):
            record["state"] = "OPEN"
    out["idle_series"] = evaluation.get("idle_series", [])
    out["cursor_first"] = evaluation.get("cursor_first")
    out["previous_verdicts"] = dict(state.get("verdicts") or {})
    out["verdicts"] = verdicts_of(evaluation)
    out["updated_at"] = stamp
    changes.sort(key=lambda c: (CHANGE_STATES.index(c["change"]), c["id"]))
    return out, changes


def unannounced_record(run: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """What a prepared journal announces: its changes and the verdicts the User saw before it.

    Kept in findings.json as ``unannounced``; carry_unannounced() reads it back when that journal's
    ledger comment was never sent.
    """
    changes = [{"id": c.get("id"), "key": c.get("key"), "change": c.get("change")}
               for c in result.get("changes") or [] if isinstance(c, dict)]
    return {"run": run, "changes": changes, "previous_verdicts": dict(result.get("previous_verdicts") or {})}


def carry_unannounced(result: Dict[str, Any], record: Any) -> int:
    """Re-announce the changes of a journal whose ledger comment was never sent (FAILED, or superseded).

    A carried change is shown again only when the finding still is what the change said and this tick
    has no newer change for it; the verdict arrows start from what the User saw before. Returns how many
    changes were carried.
    """
    if not isinstance(record, dict):
        return 0
    findings = result["state"]["findings"]
    changes = result["changes"]
    have = {c.get("key") for c in changes}
    carried = 0
    for item in record.get("changes") or []:
        if not isinstance(item, dict):
            continue
        key, change = item.get("key"), item.get("change")
        if not isinstance(key, str) or change not in CHANGE_STATES or key in have:
            continue
        finding = findings.get(key)
        if not isinstance(finding, dict) or finding.get("tick_change"):
            continue
        if (change == "RESOLVED") != (finding.get("state") == "RESOLVED"):
            continue
        finding.update(state=change, tick_change=change)
        changes.append({"id": finding["id"], "key": key, "change": change, "severity": finding.get("severity")})
        have.add(key)
        carried += 1
    changes.sort(key=lambda c: (CHANGE_STATES.index(c["change"]), c["id"]))
    previous = record.get("previous_verdicts")
    if isinstance(previous, dict) and previous:
        result["previous_verdicts"] = result["state"]["previous_verdicts"] = dict(previous)
    return carried


def ordered_findings(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Findings to show: this tick's NEW/WORSENED/RESOLVED/REOPENED first, then OPEN (AT_RISK first, then id)."""
    shown = [f for f in state.get("findings", {}).values()
             if f.get("state") != "RESOLVED" or f.get("tick_change") == "RESOLVED"]

    def order(f: Dict[str, Any]) -> Tuple[int, int, str]:
        changed = 0 if f.get("tick_change") in CHANGE_STATES else 1
        return (changed, -LEVEL_RANK.get(f.get("severity"), 0), str(f.get("id")))

    return sorted(shown, key=order)


def open_counts(state: Dict[str, Any]) -> Dict[str, int]:
    """{"open": n, "at_risk": k, "unknown": u} over findings that are not RESOLVED."""
    active = [f for f in state.get("findings", {}).values() if f.get("state") != "RESOLVED"]
    return {"open": len(active), "at_risk": sum(1 for f in active if f.get("severity") == AT_RISK),
            "unknown": sum(1 for f in active if f.get("unknown"))}


# ---------------------------------------------------------------------------- chart data

def history_row(facts: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """The history.jsonl row for this snapshot: {"t", "products": {repo: {"planned", "done"}}}."""
    products = {}
    for repo, data in sorted(_dict(facts.get("products")).items()):
        if not isinstance(data, dict):
            continue
        product = Product(repo, data)
        if product.plan_state != "PRESENT":
            continue
        products[repo] = {"planned": len(product.ids), "done": sum(1 for n in product.ids if product.done(n))}
    return {"t": core.iso(now), "products": products}


def burnup(history: Iterable[Any], names: Dict[str, str], now: datetime) -> Dict[str, List[Dict[str, Any]]]:
    """Done and planned per product over the last 30 days of history rows (one point per row time)."""
    points: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for row in history:
        if not isinstance(row, dict):
            continue
        t = _time(row.get("t"))
        if t is None or t > now or now - t > WINDOW_BURNUP:
            continue
        for repo, counts in _dict(row.get("products")).items():
            if repo not in names or not isinstance(counts, dict):
                continue
            done, planned = _int(counts.get("done")), _int(counts.get("planned"))
            if done is None or planned is None or done < 0 or planned < 0:
                continue
            points.setdefault(names[repo], {})[core.iso(t)] = {"t": core.iso(t), "done": done, "planned": planned}
    return {name: [by_t[k] for k in sorted(by_t)][-MAX_BURNUP_POINTS:] for name, by_t in points.items()}


def lane_intervals(facts: Dict[str, Any], now: datetime) -> List[Dict[str, Any]]:
    """Session intervals per lane over the last 7 days: every task row (reserved..released or open) plus
    current active rows from the lane board."""
    start_window = now - WINDOW_LANES
    seen: Set[str] = set()
    out = []

    def add(lane: Any, role: Any, task: Any, state: Any, start: Optional[datetime], end: Optional[datetime],
            request: Any) -> None:
        if lane not in LANE_ORDER or start is None or not _ok(task, TASK_RE) or not _ok(state, WORD_RE) \
                or not _ok(role, WORD_RE):
            return
        if isinstance(request, str):
            if request in seen:
                return
            seen.add(request)
        if end is not None and end < start_window or start > now:
            return
        if end is not None and end < start:
            end = start
        out.append({"lane": lane, "start": core.iso(max(start, start_window)),
                    "end": core.iso(min(end, now)) if end is not None else None,
                    "role": role, "task": task, "state": state})

    for data in _dict(facts.get("products")).values():
        for node in _dict(_dict(data).get("nodes")).values():
            for row in _list(_dict(node).get("rows")):
                if not isinstance(row, dict) or row.get("state") == "FAILED_PRESTART":
                    continue  # a prestart failure never had a session
                start = _time(row.get("reserved_ts", row.get("reserved_at")))
                end = _time(row.get("released_ts", row.get("released_at")))
                if end is None and row.get("state") not in ACTIVE_ROW_STATES:
                    continue  # ended without a recorded release time: no interval to draw
                add(row.get("lane"), row.get("role"), row.get("task"), row.get("state"), start, end,
                    row.get("launch_request_id"))
    lanes = lanes_view(facts)
    for lane in _list((lanes or {}).get("lanes")):
        for row in _list(_dict(lane).get("active")):
            if isinstance(row, dict):
                add(_dict(lane).get("lane"), row.get("role"), row.get("task"), row.get("state"),
                    _time(row.get("created")), None, row.get("request"))
    out.sort(key=lambda i: (LANE_ORDER.index(i["lane"]), i["start"], i["task"]))
    return out[-MAX_INTERVALS:]


def idle_spans(series: List[Dict[str, Any]], now: datetime) -> List[Dict[str, str]]:
    """Maximal runs of consecutive idle-while-waiting snapshots in the last 7 days (start < end only)."""
    spans = []
    run: List[datetime] = []
    for point in series + [{"t": None}]:
        t = _time(point.get("t"))
        hit = t is not None and _idle_hit(point)
        if hit:
            run.append(t)
            continue
        if len(run) >= 2 and run[-1] > run[0] and now - run[-1] <= WINDOW_LANES:
            spans.append({"start": core.iso(max(run[0], now - WINDOW_LANES)), "end": core.iso(run[-1])})
        run = []
    return spans


def chart_data(facts: Dict[str, Any], evaluation: Dict[str, Any], *, now: datetime, snapshot: str, stage: str,
               history: Iterable[Any] = (), previous_verdicts: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """The AIOPS_INSPECT_CHARTS_V1 document (input of control_plane_inspect_charts)."""
    if not isinstance(snapshot, str) or not re.fullmatch(r"[0-9a-f]{12,64}", snapshot):
        raise core.InspectError("SNAPSHOT", "snapshot must be hex")
    if stage not in ("DRY", "LIVE"):
        raise core.InspectError("CONFIG", "stage must be DRY or LIVE")
    now = core.utc(now)
    previous_verdicts = previous_verdicts or {}
    order = [r for r in evaluation["order"] if r in evaluation["products"]]
    names = {repo: evaluation["products"][repo]["name"] for repo in order}
    rows = [names[r] for r in order] + [CTRL]
    cells: Dict[str, Dict[str, str]] = {}
    basis: Dict[str, Dict[str, str]] = {}
    verdicts: Dict[str, str] = {}
    previous: Dict[str, Optional[str]] = {}
    for repo in order:
        entry = evaluation["products"][repo]
        name = names[repo]
        cells[name] = {s: entry["signals"][s]["level"] for s in PRODUCT_SIGNALS}
        basis[name] = {s: entry["signals"][s]["basis"] for s in PRODUCT_SIGNALS
                       if entry["signals"][s]["basis"] in BASES}
        verdicts[name] = entry["verdict"]
        previous[name] = previous_verdicts.get(repo)
    cells[CTRL] = {s: evaluation["ctrl"]["signals"][s]["level"] for s in CTRL_SIGNALS}
    basis[CTRL] = {s: evaluation["ctrl"]["signals"][s]["basis"] for s in CTRL_SIGNALS}
    verdicts[CTRL] = evaluation["ctrl"]["verdict"]
    previous[CTRL] = previous_verdicts.get(CTRL)
    previous = {k: (v if v in LEVEL_WORDS else None) for k, v in previous.items()}

    ladder = {}
    dag = {}
    products = {repo: Product(repo, _dict(_dict(facts.get("products")).get(repo))) for repo in order}
    for repo in order:
        name, product = names[repo], products[repo]
        counts = evaluation["products"][repo]["ladder"]
        ladder[name] = {key: counts[key] for key in STAGE_KEYS.values()}
        ladder[name].update(merge_checks=dict(counts["merge_checks"]), deployed=NOT_RECORDED, verified=NOT_RECORDED,
                            orphans=counts["orphans"], pending_plan_prs=counts["pending_plan_prs"])
        if product.plan_state != "PRESENT" or not product.ids:
            continue
        s6 = evaluation["products"][repo]["signals"].get("S6", {})
        critical = set(_dict(s6.get("value")).get("critical_path") or
                       critical_path(product.ids, product.deps, product.remaining()))
        layout = dag_layout(product.ids, product.deps)
        nodes = []
        for node_id in sorted(product.ids, key=lambda n: layout[n]):
            since = _blocked_since(product, node_id)
            hours = _hours(now, since)
            nodes.append({"id": node_id, "stage": product.stage[node_id], "depth": layout[node_id][0],
                          "row": layout[node_id][1], "blocked": since is not None, "critical": node_id in critical,
                          "blocked_hours": round(hours, 1) if hours is not None else None,
                          "basis_blocked": "GH_TEXT"})
        edges = [[dep, node_id] for node_id in product.ids for dep in product.deps[node_id]]
        dag[name] = {"nodes": nodes, "edges": edges, "prefix": product.prefix}

    lanes = lanes_view(facts) or {"lanes": []}
    enabled = {}
    for lane in _list(lanes.get("lanes")):
        if isinstance(lane, dict) and lane.get("lane") in LANE_ORDER and isinstance(lane.get("enabled"), bool):
            enabled[lane["lane"]] = lane["enabled"]
    return {
        "schema": CHARTS_SCHEMA, "generated_at": core.iso(now), "snapshot": snapshot[:12], "stage": stage,
        "scorecard": {"products": rows, "signals": list(SIGNALS), "cells": cells, "verdicts": verdicts,
                      "previous": previous, "basis": basis},
        "ladder": ladder, "dag": dag, "burnup": burnup(history, names, now),
        "lanes": {"window_start": core.iso(now - WINDOW_LANES), "window_end": core.iso(now),
                  "order": list(LANE_ORDER), "enabled": enabled, "intervals": lane_intervals(facts, now),
                  "idle_waiting": idle_spans(evaluation.get("idle_series", []), now)},
    }


# ---------------------------------------------------------------------------- one call for the tick

def run(facts: Dict[str, Any], config: Optional[Dict[str, Any]], now: datetime, *, state: Optional[Dict[str, Any]],
        snapshot: str, journal: Any = None, history: Iterable[Any] = ()) -> Dict[str, Any]:
    """Signals + findings lifecycle + chart data for one committed T1 snapshot.

    Returns {"evaluation", "state" (new findings.json), "changes", "verdicts", "previous_verdicts", "charts"}.
    """
    state = _check_state(state)
    evaluation = evaluate(facts, config, now, journal=journal, state=state)
    new, changes = update_findings(state, evaluation, now)
    stage = (config or {}).get("stage") or "DRY"
    charts = chart_data(facts, evaluation, now=now, snapshot=snapshot, stage=stage, history=list(history),
                        previous_verdicts=state.get("verdicts") or {})
    return {"evaluation": evaluation, "state": new, "changes": changes, "verdicts": new["verdicts"],
            "previous_verdicts": new["previous_verdicts"], "charts": charts}
