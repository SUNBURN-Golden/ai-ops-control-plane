#!/usr/bin/env python3
"""Independent Mac/Linux controller: local execution, shared GitHub ownership.

No old-host URL, SSH, root service, HMAC gateway or shared SQLite mount. One
explicit invocation does one action; provider/browser capabilities are qualified
separately. The legacy HTTP relay remains an optional transport, not a prerequisite.
"""
import argparse
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import subprocess
import sys
import uuid


def sibling(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ownership = sibling("control_plane_ownership")
gateway = sibling("control_plane_flow_gateway")
relay = sibling("control_plane_codex_relay")
flow = gateway.flow
require = flow.require
FILES = ("control_plane_local_host.py", "control_plane_ownership.py", "control_plane_flow.py",
         "control_plane_flow_gateway.py", "control_plane_codex_relay.py")
LANES = {"DEVIN", "GROK_BUILD", "CURSOR", "GLM"}
TARGETS = {"BeautifulMind-JT/ai-ops-control-plane", "BeautifulMind-JT/kix-protocol",
           "BeautifulMind-JT/kix-commerce-apps",
           "BeautifulMind-JT/ZARI", "BeautifulMind-JT/film-unit-mv-studio", "BeautifulMind-JT/maeum-gyeol"}


def private(path, *, directory=False):
    path = Path(path).absolute()
    info = path.lstat()
    require(not path.is_symlink() and info.st_uid == os.geteuid() and not info.st_mode & 0o077 and
            (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1),
            "host-owned private path required")
    for parent in path.parents:
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid in {0, os.geteuid()} and not info.st_mode & 0o022,
                "replaceable host path ancestor")
    return path


def exclusive(path, value):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(value if isinstance(value, bytes) else value.encode())
        stream.flush()
        os.fsync(stream.fileno())


def initialize(root, host_id):
    """Create a new local partition, not a copy of any other host's ledger."""
    root = Path(root).absolute()
    require(root.parent.exists(), "create a private installation parent first")
    # Validate parent before any writes. Never replace an existing/partial install.
    private(root.parent, directory=True)
    identity = ownership.host_identity(host_id, str(uuid.uuid4()))
    root.mkdir(mode=0o700)
    for folder in ("bin", "control", "relay", "workspaces"):
        (root / folder).mkdir(mode=0o700)
    pins = {}
    for name in FILES:
        content = Path(__file__).with_name(name).read_bytes()
        exclusive(root / "bin" / name, content)
        pins[name] = hashlib.sha256(content).hexdigest()
    store = flow.Store(root / "control" / "flow.sqlite", initialize=True)
    store.initialize_consumers()
    with store.transaction() as db:
        db.executescript("""
        CREATE TABLE host_identity (instance TEXT PRIMARY KEY);
        CREATE TABLE host_tasks (task TEXT PRIMARY KEY, initial_sha TEXT NOT NULL);
        CREATE TABLE host_actions (id TEXT PRIMARY KEY, assignment TEXT NOT NULL,
          operation TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
          receipt TEXT, active INTEGER NOT NULL DEFAULT 1);
        CREATE UNIQUE INDEX one_active_local_action ON host_actions(active) WHERE active=1;
        """)
        db.execute("INSERT INTO host_identity VALUES (?)", (identity["instance_id"],))
    policy = dict(schema_version=1, authority_mode="INDEPENDENT_HOST", **identity,
                  root=str(root), enabled=False, source_review="PENDING", source_review_pointer=None,
                  source_sha256=pins, user_actors=[], projection_actor=None,
                  assignment_binding=None, diagnostic=None, relay=None, adapters={}, desktop={},
                  execution_transport="APP_SCREEN" if sys.platform == "darwin" else "CLI_ADAPTER")
    exclusive(root / "policy.json", flow.canonical(policy))
    return dict(state="LOCAL_PARTITION_CREATED_DISABLED", host_id=host_id,
                instance_id=identity["instance_id"], policy=str(root / "policy.json"),
                old_host_contacted=False)


def load_policy(path):
    policy = flow.decode(private(path).read_text())
    require(policy.get("schema_version") == 1 and policy.get("authority_mode") == "INDEPENDENT_HOST",
            "independent host policy required")
    ownership.host_identity(policy["host_id"], policy["instance_id"])
    root = private(policy["root"], directory=True)
    for name in FILES:
        file = private(root / "bin" / name)
        require(file.resolve() == Path(__file__).with_name(name).resolve() and
                hashlib.sha256(file.read_bytes()).hexdigest() == policy["source_sha256"][name],
                "execute the installed, pinned local source, not a task checkout")
    private(root / "control", directory=True)
    private(root / "control" / "flow.sqlite")
    return policy


class Host:
    def __init__(self, policy, api):
        require(policy.get("enabled") is True and policy.get("source_review") == "PASS" and
                flow.github_pointer(policy.get("source_review_pointer")), "local source/execution not approved")
        self.policy, self.api = policy, api
        self.root = Path(policy["root"])
        self.store = flow.Store(self.root / "control" / "flow.sqlite")
        with self.store.transaction() as db:
            identities = db.execute("SELECT instance FROM host_identity").fetchall()
            require(len(identities) == 1 and identities[0][0] == policy["instance_id"],
                    "local continuity lost; never adopt an old host instance or recreate its ledger")
        self.registry = ownership.Registry(api)

    def record(self, binding, marker):
        repo, issue = binding["repository"], binding["issue"]
        ownership.task_key(repo, issue)
        require(binding["actor"] in self.policy["user_actors"], "untrusted authorization actor")
        value = self.api.call("GET", f"repos/{repo}/issues/comments/{int(binding['comment_id'])}")
        body = value.get("body", "")
        require(value.get("user", {}).get("login") == binding["actor"] and
                value.get("issue_url") == f"https://api.github.com/repos/{repo}/issues/{issue}" and
                value.get("html_url") == f"https://github.com/{repo}/issues/{issue}#issuecomment-{binding['comment_id']}" and
                hashlib.sha256(body.encode()).hexdigest() == binding["sha256"], "authorization edited/wrong task")
        record = gateway.unwrap(body, marker)
        require(record.get("active") is True, "authorization inactive")
        return record, value["html_url"]

    def assignment(self, *, terminal=False):
        binding = self.policy["assignment_binding"]
        require(binding["repository"] in TARGETS, "target repository outside approved independent-host scope")
        value, pointer = self.record(binding, "<!-- ASTRA_HOST_ASSIGNMENT_V1 -->")
        require(value.get("repository") == binding["repository"] and value.get("issue") == binding["issue"] and
                value.get("host_id") == self.policy["host_id"] and value.get("instance_id") == self.policy["instance_id"] and
                value.get("admission") == "FRESH_TASK" and value.get("pointer") == pointer and
                isinstance(value.get("task_id"), str) and value.get("task_id") and
                isinstance(value.get("operations"), list) and value["operations"] and
                set(value["operations"]) <= {"WORK_DIAGNOSTIC", "BUILDER", "REVIEW"}, "host enrollment mismatch")
        task = self.api.call("GET", f"repos/{binding['repository']}/issues/{binding['issue']}")
        require((terminal or task.get("state") == "open") and not task.get("pull_request") and
                task.get("id") == value.get("github_issue_id") and
                task.get("created_at") == value.get("github_issue_created_at"), "canonical task identity changed")
        return value

    def shared(self, assignment, *, acquire=False):
        key = ownership.task_key(assignment["repository"], assignment["issue"])
        with self.store.transaction() as db:
            known = db.execute("SELECT initial_sha FROM host_tasks WHERE task=?", (key,)).fetchone()
        if known:
            head, state = self.registry.continuity(assignment, known[0])
        else:
            head, state = self.registry.acquire(assignment) if acquire else self.registry.owned(assignment)
        with self.store.transaction() as db:
            db.execute("INSERT INTO host_tasks VALUES (?,?) ON CONFLICT(task) DO UPDATE SET initial_sha=excluded.initial_sha",
                       (key, head))
        return head, state

    def begin(self, action_id, operation, payload):
        assignment = self.assignment()
        require(operation in assignment["operations"], "operation not assigned")
        with self.store.transaction() as db:
            prior = db.execute("SELECT * FROM host_actions WHERE id=?", (action_id,)).fetchone()
            if prior:
                require(prior["assignment"] == flow.canonical(assignment) and
                        prior["payload"] == flow.canonical(payload) and prior["operation"] == operation,
                        "local request input changed")
                return dict(start_allowed=False, state=prior["state"], action_id=action_id)
            require(db.execute("SELECT 1 FROM host_actions WHERE active=1").fetchone() is None,
                    "max_active_sessions=1; unresolved local action")
            db.execute("INSERT INTO host_actions VALUES (?,?,?,?, 'CLAIMING',NULL,1)",
                       (action_id, flow.canonical(assignment), operation, flow.canonical(payload)))
        try:
            self.shared(assignment, acquire=True)
            grant = self.registry.begin(assignment, action_id, operation, payload)
            if not grant["start_allowed"]:
                self.state(action_id, "OWNED_ELSEWHERE_OR_ALREADY_ATTEMPTED", grant)
                return grant
            self.shared(assignment)
            self.state(action_id, "SUBMITTING", grant)
            return grant
        except BaseException:
            self.state(action_id, "UNKNOWN", None)
            raise

    def state(self, action_id, state, receipt):
        with self.store.transaction() as db:
            db.execute("UPDATE host_actions SET state=?,receipt=? WHERE id=? AND active=1",
                       (state, flow.canonical(receipt), action_id))

    def diagnostic_app(self):
        action = self.policy["diagnostic"]["request"]
        flow.validate_diagnostic(action)
        assignment = self.assignment()
        require(assignment["repository"] == action["subject"]["repository"] and
                assignment["issue"] == flow.diagnostic_issue(action) and
                assignment["task_id"] == action["subject"]["task_id"], "diagnostic belongs to another host task")
        policy = dict(self.policy, enabled=False)
        ports = gateway.GithubPorts(self.api, policy)
        # In-process calls only. No listener, root install or external shared secret.
        app = gateway.Ingress(self.store, ports, policy, b"", b"", diagnostic_secret=secrets.token_bytes(32))
        return app, action

    def diagnostic_ports(self, app, action):
        def claim(_policy, request_id, session, _secret):
            require(request_id == action["request_id"] and session == action["work_session"], "local claim changed")
            self.shared(self.assignment())
            app.ports.diagnostic_current(action)
            delivered, receipt = self.store.delivered_action(request_id)
            require(delivered == action, "wrong local action")
            app.ports.projected_pointer(action, receipt, dict(repository=action["subject"]["repository"],
                                                             issue=flow.diagnostic_issue(action)))
            result = self.store.claim_astra(action, action["identity"], session)
            if result["start_allowed"]:
                result["action"] = action
            return result

        def submit(_policy, operation, value, _secret):
            require(operation == "result", "unsupported local transport operation")
            self.shared(self.assignment())
            app.ports.diagnostic_current(action)
            result_action = flow.diagnostic_result(action, value)
            self.store.diagnostic_claim(action)
            def current(_):
                self.shared(self.assignment())
                self.store.diagnostic_claim(action)
                return app.ports.diagnostic_current(action)
            result = self.store.send_once(result_action, lambda a: app.ports.diagnostic_publish(
                a, "<!-- ASTRA_DIAGNOSTIC_RESULT_V1 -->"), current)
            return dict(flow.diagnostic_outcome(result), kind="DIAGNOSTIC_RESULT",
                        diagnostic_request_id=action["request_id"], grants=[], live_acceptance="UNCHANGED")
        return claim, submit

    def work(self, *, collect=False, send_fn=relay.run_codex):
        if self.policy.get("execution_transport") == "APP_SCREEN":
            require(not collect, "use collect-ui with an observation from the existing conversation")
            return self.prepare_ui()
        app, action = self.diagnostic_app()
        request_id = action["request_id"]
        policy = dict(self.policy["relay"], mode="DIAGNOSTIC", authority_mode="IN_PROCESS_HOST",
                      diagnostic_request=action, work_sessions={request_id: action["work_session"]})
        # Only this installed controller can select in-process transport. Standalone
        # relay CLI rejects it; no caller-supplied endpoint or fake HTTP credential.
        require(policy.get("browser_qualified") is True and flow.github_pointer(policy.get("browser_evidence")),
                "dedicated browser capabilities must be qualified")
        relay.validate_policy(policy)
        require(Path(policy["state_directory"]).resolve() == (self.root / "relay").resolve(),
                "wrong host relay partition")
        claim, submit = self.diagnostic_ports(app, action)
        if not collect:
            grant = self.begin(request_id, "WORK_DIAGNOSTIC", action)
            if not grant["start_allowed"]:
                return grant
        else:
            self.shared(self.assignment())
            with self.store.transaction() as db:
                prior = db.execute("SELECT * FROM host_actions WHERE id=? AND active=1", (request_id,)).fetchone()
                require(prior is not None and prior["state"] == "WAITING" and
                        prior["payload"] == flow.canonical(action), "not the same WAITING local request")
                db.execute("UPDATE host_actions SET state='OBSERVING' WHERE id=?", (request_id,))
        try:
            if not collect:
                prepared = app.prepare_diagnostic(local_delivery=True)
                require(prepared["state"] == "CONFIRMED", "diagnostic GitHub projection unresolved")
            # Ephemeral internal capability never leaves this process or enters child env.
            internal = secrets.token_bytes(32)
            if collect:
                result = relay.collect_existing(policy, request_id, internal, observe_fn=send_fn, submit_fn=submit)
            else:
                result = relay.deliver(policy, request_id, internal, claim_fn=claim, send_fn=send_fn, submit_fn=submit)
            self.state(request_id, result["state"], result)
            return result
        except BaseException:
            self.state(request_id, "UNKNOWN", None)
            raise

    def run_adapter(self, binding):
        """Run one qualified local adapter, not the legacy VM's host helper."""
        if self.policy.get("execution_transport") == "APP_SCREEN":
            return self.prepare_ui(binding)
        authorized, _ = self.record(binding, "<!-- ASTRA_HOST_ACTION_V1 -->")
        assignment = self.assignment()
        packet = authorized["packet"]
        lane, operation = packet["builder_id"], packet["operation"]
        require(binding["repository"] == assignment["repository"] and binding["issue"] == assignment["issue"] and
                lane in LANES and operation in {"BUILDER", "REVIEW"} and
                packet["repository"] == assignment["repository"] and packet["issue"] == assignment["issue"] and
                packet["task_id"] == assignment["task_id"] and packet["host_id"] == assignment["host_id"] and
                packet["instance_id"] == assignment["instance_id"], "adapter task/host mismatch")
        require(operation != "REVIEW" or packet.get("read_only") is True, "review must be read-only")
        config = self.policy["adapters"][lane]
        require(config.get("qualified") is True and flow.github_pointer(config.get("evidence_pointer")) and
                config.get("host_id") == self.policy["host_id"] and
                config.get("instance_id") == self.policy["instance_id"] and
                operation in config.get("operations", []) and config.get("execution_mode") in
                {"REMOTE_SESSION", "PERSISTENT_SUPERVISOR"}, "local adapter not qualified for operation")
        executable = private(config["executable"])
        require(hashlib.sha256(executable.read_bytes()).hexdigest() == config["sha256"], "adapter changed")
        require(isinstance(packet.get("revision"), str) and packet["revision"] and
                re.fullmatch(r"[a-f0-9]{40}", packet.get("head", "")), "revision/HEAD required")
        if operation == "REVIEW":
            require(type(packet.get("pr")) is int and packet["pr"] > 0, "review PR required")
            pr = self.api.call("GET", f"repos/{packet['repository']}/pulls/{packet['pr']}")
            require(pr.get("state") == "open" and pr.get("head", {}).get("sha") == packet["head"], "stale review HEAD")
        else:
            main = self.api.call("GET", f"repos/{packet['repository']}/git/ref/heads/main")
            require(main.get("object", {}).get("sha") == packet["head"], "stale implementation base")
        action_id = ownership.digest(packet)
        grant = self.begin(action_id, operation, packet)
        if not grant["start_allowed"]:
            return grant
        try:
            workspace = self.root / "workspaces" / action_id
            workspace.mkdir(mode=0o700)  # no shared/reused task checkout
            payload = dict(packet, request_id=action_id, worktree_root=str(workspace))
            run = subprocess.run([str(executable), operation.lower()], input=flow.canonical(payload), text=True,
                                 capture_output=True, timeout=30, cwd=workspace,
                                 env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"})
            require(run.returncode == 0 and len(run.stdout) <= 65536, "adapter result unknown")
            result = flow.decode(run.stdout)
            require(result.get("request_id") == action_id and result.get("outcome") == "CONFIRMED" and
                    isinstance(result.get("session_id"), str) and result["session_id"], "durable session not confirmed")
            self.state(action_id, "CONFIRMED", result)
            return result
        except BaseException:
            self.state(action_id, "UNKNOWN", None)
            raise

    def ui_current(self, packet, operation):
        """Recheck canonical source immediately before admitting a screen effect."""
        self.shared(self.assignment())
        if operation == "WORK_DIAGNOSTIC":
            app, action = self.diagnostic_app()
            require(action == packet, "diagnostic changed")
            app.ports.diagnostic_current(action)
            self.store.diagnostic_claim(action)
        elif operation == "REVIEW":
            pr = self.api.call("GET", f"repos/{packet['repository']}/pulls/{packet['pr']}")
            require(pr.get("state") == "open" and pr.get("head", {}).get("sha") == packet["head"],
                    "stale review HEAD")
        else:
            main = self.api.call("GET", f"repos/{packet['repository']}/git/ref/heads/main")
            require(main.get("object", {}).get("sha") == packet["head"], "stale implementation base")

    def prepare_ui(self, binding=None):
        """Prepare a desktop-tool envelope; never invoke a CLI or send to an app."""
        assignment = self.assignment()
        if binding is None:
            app, packet = self.diagnostic_app()
            operation, lane = "WORK_DIAGNOSTIC", "WORK"
            action_id = packet["request_id"]
            config = self.policy.get("desktop", {}).get(lane, {})
            target = config.get("target", {})
            require(target.get("session") == packet["work_session"] and
                    target.get("model") == "GPT-6 Astra" and target.get("effort") == "medium",
                    "dedicated Work target required")
        else:
            authorized, _ = self.record(binding, "<!-- ASTRA_HOST_ACTION_V1 -->")
            packet = authorized["packet"]
            operation, lane = packet["operation"], packet["builder_id"]
            require(binding["repository"] == assignment["repository"] and binding["issue"] == assignment["issue"] and
                    lane in LANES and operation in {"BUILDER", "REVIEW"} and all(
                        packet.get(k) == assignment[k] for k in
                        ("repository", "issue", "task_id", "host_id", "instance_id")), "desktop assignment mismatch")
            require(operation != "REVIEW" or (packet.get("read_only") is True and
                    type(packet.get("pr")) is int and packet["pr"] > 0), "read-only review PR required")
            require(isinstance(packet.get("revision"), str) and packet["revision"] and
                    re.fullmatch(r"[a-f0-9]{40}", packet.get("head", "")), "revision/HEAD required")
            config = self.policy.get("desktop", {}).get(lane, {})
            target = packet.get("ui_target", {})
            require(target == config.get("target"), "authorized app target mismatch")
            action_id = ownership.digest(packet)
        require(config.get("execution_mode") == "APP_SCREEN" and
                config.get("host_id") == self.policy["host_id"] and
                config.get("instance_id") == self.policy["instance_id"] and
                operation in config.get("operations", []) and
                flow.github_pointer(config.get("isolation_evidence")), "desktop isolation/identity not qualified")
        pair = config.get("coordinator")
        require(pair in [{"model": "gpt-5.6-sol", "effort": "max"},
                         {"model": "gpt-5.6-sol", "effort": "xhigh"}] and
                pair in config.get("supported_coordinators", []), "unverified coordinator model/effort")
        require(pair["effort"] == "max" or {"model": "gpt-5.6-sol", "effort": "max"} not in
                config["supported_coordinators"], "use supported primary before selecting the alternative")
        require(isinstance(target, dict) and set(target) == {"app", "account", "session", "model", "effort"} and
                all(isinstance(v, str) and 0 < len(v.strip()) <= 500 for v in target.values()),
                "exact app/account/session/model/effort required")
        # This is an observation protocol, not provider attestation or a sandbox.
        # A read-only reviewer needs independently enforced workspace/tool rights.
        require(operation != "REVIEW" or config.get("read_only_enforced") is True,
                "review isolation must be enforced, not just requested in a prompt")
        grant = self.begin(action_id, operation, packet)
        if not grant["start_allowed"]:
            return dict(grant, send_allowed=False)
        try:
            if operation == "WORK_DIAGNOSTIC":
                require(app.prepare_diagnostic(local_delivery=True)["state"] == "CONFIRMED",
                        "diagnostic projection unresolved")
                claim, _ = self.diagnostic_ports(app, packet)
                require(claim(None, action_id, packet["work_session"], None)["start_allowed"],
                        "diagnostic already claimed")
            self.ui_current(packet, operation)
            workspace = self.root / "workspaces" / action_id
            workspace.mkdir(mode=0o700)
            context = dict(transport="APP_SCREEN", target=dict(target, workspace=str(workspace)),
                           binding=binding, configuration=config)
            self.state(action_id, "UI_READY", context)
            return dict(state="UI_READY", action_id=action_id, send_allowed=False,
                        target=context["target"], packet=packet)
        except BaseException:
            self.state(action_id, "UNKNOWN", None)
            raise

    def ui_row(self, action_id):
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM host_actions WHERE id=? AND active=1", (action_id,)).fetchone()
        require(row is not None, "active desktop action missing")
        context = flow.decode(row["receipt"])
        require(isinstance(context, dict) and context.get("transport") == "APP_SCREEN",
                "not a desktop action; reconcile existing state")
        require(row["assignment"] == flow.canonical(self.assignment()), "assignment changed")
        packet = flow.decode(row["payload"])
        lane = "WORK" if row["operation"] == "WORK_DIAGNOSTIC" else packet["builder_id"]
        require(context["configuration"] == self.policy.get("desktop", {}).get(lane), "desktop policy changed")
        if context["binding"] is not None:
            authorized, _ = self.record(context["binding"], "<!-- ASTRA_HOST_ACTION_V1 -->")
            require(authorized["packet"] == packet, "action authorization changed")
        return row, context, packet

    def send_ui(self, observation):
        """Consume the one send permit BEFORE the desktop coordinator types/clicks.

        A lost return/crash consumes the permit too. Never reconstruct a permit
        from SQLite, shell output history, a timeout or a provider error.
        """
        action_id = observation["action_id"]
        row, context, packet = self.ui_row(action_id)
        require(observation.get("target") == context["target"] and observation.get("input_ready") is True,
                "fresh visible target/input verification required")
        self.ui_current(packet, row["operation"])
        with self.store.transaction() as db:
            updated = db.execute("UPDATE host_actions SET state='UNKNOWN' WHERE id=? AND active=1 AND state='UI_READY'",
                                 (action_id,)).rowcount
            require(updated == 1, "permit already consumed; inspect existing session only")
        return dict(state="SUBMITTING", action_id=action_id, send_allowed=True, target=context["target"],
                    packet=packet, instruction="Use the verified app screen once; never replay this response")

    def collect_ui(self, observation):
        """Persist an authenticated coordinator observation, never an approval."""
        action_id = observation["action_id"]
        row, context, packet = self.ui_row(action_id)
        require(observation.get("target") == context["target"], "wrong observed app/session/workspace")
        outcome = observation.get("outcome")
        require(outcome in {"UNKNOWN", "WAITING", "ANSWER", "SESSION_OBSERVED"}, "invalid screen outcome")
        if row["operation"] == "WORK_DIAGNOSTIC":
            require(outcome != "SESSION_OBSERVED", "Work requires a diagnostic answer")
            if outcome == "ANSWER":
                flow.diagnostic_result(packet, observation.get("diagnostic_result"))
        else:
            require(outcome != "ANSWER", "builder observation cannot issue a diagnostic/audit verdict")
        self.ui_current(packet, row["operation"])
        fingerprint = flow.digest(observation)
        with self.store.transaction() as db:
            current = db.execute("SELECT state,receipt FROM host_actions WHERE id=? AND active=1", (action_id,)).fetchone()
            previous = flow.decode(current["receipt"])
            if current["state"] in {"DIAGNOSTIC_RECORDED", "SESSION_OBSERVED"}:
                require(previous.get("observation_digest") == fingerprint, "result already recorded")
                return dict(state=current["state"], action_id=action_id, duplicate=True, grants=[])
            require(current["state"] in {"UNKNOWN", "WAITING"}, "not an observable desktop action")
            # Durable intent precedes collection/publication. Crash blocks replay.
            db.execute("UPDATE host_actions SET state='RESULT_SUBMITTING' WHERE id=?", (action_id,))
        try:
            state = outcome
            if outcome == "ANSWER":
                app, action = self.diagnostic_app()
                _, submit = self.diagnostic_ports(app, action)
                result = submit(None, "result", observation["diagnostic_result"], None)
                require(result["state"] == "CONFIRMED", "publication unresolved")
                state = "DIAGNOSTIC_RECORDED"
            context = dict(context, observation_digest=fingerprint, observation=observation,
                           provenance="COORDINATOR_SCREEN_OBSERVATION_NOT_PROVIDER_ATTESTATION")
            self.state(action_id, state, context)
            return dict(state=state, action_id=action_id, grants=[], live_acceptance="UNCHANGED")
        except BaseException:
            # Preserve context; collector's own outbox fences any publication retry.
            self.state(action_id, "UNKNOWN", context)
            raise

    def finish(self, binding):
        evidence, pointer = self.record(binding, "<!-- ASTRA_HOST_TERMINAL_V1 -->")
        assignment = self.assignment(terminal=True)
        require(binding["repository"] == assignment["repository"] and binding["issue"] == assignment["issue"] and
                evidence.get("host_id") == assignment["host_id"] and
                evidence.get("instance_id") == assignment["instance_id"], "terminal evidence wrong host/task")
        action_id = evidence["action_id"]
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM host_actions WHERE id=?", (action_id,)).fetchone()
            require(row is not None and row["assignment"] == flow.canonical(assignment), "local action missing")
        prestart = evidence.get("failed_prestart") is True
        terminal = dict(sender_fenced=evidence.get("sender_fenced"), session_ended=evidence.get("session_ended"),
                        publication_resolved=evidence.get("publication_resolved"), pointer=pointer)
        require(all(terminal[k] is True for k in ("sender_fenced", "session_ended", "publication_resolved")),
                "exact terminal and publisher fence evidence required")
        if row["operation"] == "WORK_DIAGNOSTIC":
            action = flow.decode(row["payload"])
            with self.store.transaction() as db:
                claim = db.execute("SELECT 1 FROM astra_claims WHERE request=?", (action_id,)).fetchone()
                ids = (action_id, flow.digest(["projection", action_id]), flow.digest(["diagnostic-result", action_id]))
                unresolved = db.execute("SELECT 1 FROM outbox WHERE id IN (?,?,?) AND state != 'CONFIRMED'", ids).fetchone()
            require(not unresolved, "publisher uncertainty requires separate reconciliation")
            if claim:
                self.store.reconcile_astra(action_id, action["identity"], action["work_session"], pointer,
                                           consumer_fenced=True)
            else:
                require(prestart and evidence.get("downstream_never_started") is True,
                        "unclaimed action needs explicit never-started/fenced proof")
        if prestart:
            require(evidence.get("downstream_never_started") is True, "failed-prestart proof required")
            self.shared(assignment, acquire=True)
            self.registry.failed_prestart(assignment, action_id, row["operation"], flow.decode(row["payload"]), terminal)
        else:
            self.shared(assignment)
            self.registry.finish(assignment, action_id, terminal)
        self.shared(assignment)
        with self.store.transaction() as db:
            db.execute("UPDATE host_actions SET state='FINISHED',active=0,receipt=? WHERE id=?",
                       (flow.canonical(terminal), action_id))
        return dict(state="FINISHED", action_id=action_id, owner_retained=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "check", "work-diagnostic", "collect-diagnostic", "run-adapter", "finish",
                                            "prepare-ui", "send-ui", "collect-ui"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--host-id")
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--binding", type=Path)
    parser.add_argument("--observation", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "init":
            result = initialize(args.root, args.host_id)
        else:
            policy = load_policy(args.policy)
            if args.command == "check":
                result = dict(state="SOURCE_CONFIG_CHECKED", enabled=policy["enabled"], live_acceptance="NOT_TESTED")
            else:
                api = gateway.Api(os.environ.pop("ASTRA_HOST_GITHUB_TOKEN", ""), os.environ.pop("ASTRA_HOST_SLACK_TOKEN", ""))
                host = Host(policy, api)
                if args.command == "prepare-ui":
                    result = host.prepare_ui(flow.decode(private(args.binding).read_text()) if args.binding else None)
                elif args.command in {"send-ui", "collect-ui"}:
                    observation = flow.decode(private(args.observation).read_text())
                    result = (host.send_ui if args.command == "send-ui" else host.collect_ui)(observation)
                elif args.command in {"work-diagnostic", "collect-diagnostic"}:
                    result = host.work(collect=args.command == "collect-diagnostic")
                else:
                    binding = flow.decode(private(args.binding).read_text())
                    result = (host.run_adapter if args.command == "run-adapter" else host.finish)(binding)
        print(flow.canonical(result))
        return 2 if not result.get("send_allowed") and result.get("state") in {
            "UNKNOWN", "SUBMITTING", "CLAIMING", "OBSERVING", "RESULT_SUBMITTING"} else 0
    except Exception:
        print(flow.canonical(dict(state="BLOCKED", instruction="inspect protected local state; never replay UNKNOWN")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
