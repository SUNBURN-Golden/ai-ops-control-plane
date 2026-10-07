#!/usr/bin/env python3
"""AIOPS Mac: local application service, one-shot bot CLI, and MCP stdio bridge."""
from __future__ import annotations

import argparse
import http.cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import sys
import threading
import urllib.error
import urllib.request
import uuid
import webbrowser

if __name__ == '__main__':
    sys.dont_write_bytecode = True

import agents
import handoff
import host_observation
import mac_authority
import native_transfer
from provider_catalog import public_catalog
from common import AppError, VERSION, atomic_json, encoded, parse_json, private_directory, read_json
from core import Engine, Store, service_lock

ASSETS = Path(__file__).with_name('ui')
ASSET_TYPES = {'/': ('index.html', 'text/html; charset=utf-8'),
               '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
               '/style.css': ('style.css', 'text/css; charset=utf-8'),
               '/icon.svg': ('icon.svg', 'image/svg+xml')}


def default_directory():
    return Path.home() / ('Library/Application Support/AIOPS' if sys.platform == 'darwin' else '.local/share/aiops-mac')


def token(path):
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(secrets.token_urlsafe(32)); stream.flush(); os.fsync(stream.fileno())
    info = path.lstat()
    if path.is_symlink() or not path.is_file() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise AppError('PRIVATE_TOKEN_REQUIRED')
    value = path.read_text().strip()
    if len(value) < 32: raise AppError('INVALID_TOKEN_FILE')
    return value


class Application:
    def __init__(self, directory, engine=None):
        self.store = Store(directory)
        try:
            self.engine = engine or Engine(self.store)
            self.canonical = getattr(self.engine, 'canonical', None) or native_transfer.Controller(self.store)
            self.owner_token = token(self.store.directory / 'desktop-token')
            self.relay_token = token(self.store.directory / 'relay-token')
            self.session = secrets.token_urlsafe(32)
            # Publish the local service without waiting for unused CLI versions
            # or network authentication. Admission checks credentials afresh.
            self.doctor = agents.availability(versions=False, authenticate=False)
        except BaseException:
            self.store.close()
            raise

    def state(self):
        return {'version': VERSION, 'settings': self.store.settings(), 'jobs': [self.engine.describe(j, compact=True) for j in self.store.jobs()],
                'providers': public_catalog(),
                'connections': self.doctor, 'data_directory': str(self.store.directory),
                'cli_path': str(Path(__file__).resolve()), 'python_path': sys.executable}

    def canonical_start(self, value):
        record = self.canonical.start(value)
        self.engine.wake.set()
        return self.canonical.public(record)

    def host_action(self, operation, value):
        source=self.canonical.source
        if not isinstance(source,mac_authority.LocalSource): raise AppError('MAC_HOST_ADAPTER_REQUIRED')
        if operation=='initialize': return source.initialize(value)
        if operation=='generation': return source.generation(value)
        if operation=='product-builder': return self.store.product_builder(value)
        if operation=='bundle-prepare':
            import mac_bundle
            return mac_bundle.prepare(source,value)
        if operation=='receipt-scope': return source.annotate_receipt(value)
        if operation=='advance-base': return source.advance_base(value)
        if operation=='stop':
            if set(value)!={'request_id'}: raise AppError('MAC_HOST_REQUEST_INVALID')
            return self.canonical.public(self.canonical.stop(value['request_id']))
        if operation=='reconcile-accepted':
            if set(value)!={'repository','task_id'}: raise AppError('MAC_HOST_REQUEST_INVALID')
            return self.engine.pipeline.reconcile_accepted(value['repository'],value['task_id'])
        if operation=='retry-delivery':
            if set(value)!={'repository','task_id'}: raise AppError('MAC_HOST_REQUEST_INVALID')
            result=self.engine.pipeline.retry_delivery(value['repository'],value['task_id'])
            self.engine.wake.set(); return result
        if operation in ('register','tasks'):
            if set(value)!={'repository'}: raise AppError('MAC_HOST_REQUEST_INVALID')
            if operation=='register':
                # The owner cannot inject a fake history/program JSON over HTTP.
                # The existing authenticated read-only GitHub reader imports it.
                return source.register(handoff.inspect_repository(value['repository']))
            return source.tasks(value['repository'])
        if operation=='start':
            if set(value)!={'repository','task_id','request_id'}: raise AppError('MAC_HOST_REQUEST_INVALID')
            return self.canonical_start({'binding':source.select(value['repository'],value['task_id']),
                                         'request_id':value['request_id']})
        raise AppError('MAC_HOST_OPERATION_UNSUPPORTED')

    def start(self, value):
        existing = self.store.existing_request(value)
        if existing: return existing
        # Discovery is a preflight observation, not a permanent start-time cache.
        # It does not run a model or claim provider authentication is qualified.
        needed = {v['provider'] for v in self.store.settings()['roles'].values()}
        self.doctor.update(agents.availability(providers=needed, versions=False))
        if not self.doctor['git']['installed'] or not self.doctor['gh']['installed'] or not self.doctor['github_authenticated']:
            raise AppError('GITHUB_CONNECTION_REQUIRED', '연결 설정에서 GitHub 로그인을 완료한 뒤 다시 시작해 주세요.')
        if any(not self.doctor[x]['installed'] for x in needed):
            raise AppError('MODEL_CONNECTION_REQUIRED', '모델 설정에서 선택한 실행 도구를 먼저 설치해 주세요.')
        job = self.store.create(value); self.engine.wake.set(); return job

    def action(self, key, action, value):
        if action=='merge': return self.engine.user_merge(key,value)
        with self.store.lock:
            if action == 'accept': self.engine.validate_acceptance(self.store.get(key), value)
            if action=='accept' and self.store.get(key).get('native_lineage'):
                self.store.db.execute('BEGIN IMMEDIATE')
                try:
                    result = self.store.action(key, action, value)
                    self.engine.pipeline.record_inspection(result)
                    self.store.db.execute('COMMIT')
                except Exception:
                    self.store.db.execute('ROLLBACK'); raise
            else: result = self.store.action(key, action, value)
        self.engine.wake.set(); return result

    def inspect_handoff(self, value):
        value = handoff.request(value, inspect=True)
        snapshot = handoff.inspect_repository(value['repository'])
        with self.store.lock:
            refs, blockers = handoff.local_references(self.store, value['repository'])
        snapshot.update(source_jobs=refs, blockers=[*snapshot['blockers'], *blockers])
        return handoff.summary(snapshot)

    def prepare_handoff(self, value):
        value = handoff.request(value)
        prior = self.store.existing_handoff(value)
        if prior: return handoff.summary(prior)
        with self.store.lock:
            handoff.local_references(self.store, value['repository'], value['source_job_id'])
        snapshot = handoff.inspect_repository(value['repository'])
        return handoff.summary(self.store.create_handoff(value, snapshot))


def handler(app, origin):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'AIOPS/' + VERSION
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args): pass  # no URL, token, prompt or body logging

        def send(self, status, value, mime='application/json; charset=utf-8', cookie=None):
            raw = value if isinstance(value, bytes) else encoded(value).encode()
            self.send_response(status)
            for key, val in [('Content-Type', mime), ('Content-Length', str(len(raw))), ('Cache-Control', 'no-store'),
                             ('X-Content-Type-Options', 'nosniff'), ('Referrer-Policy', 'no-referrer'),
                             ('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"),
                             ('Connection', 'close')]:
                self.send_header(key, val)
            if cookie: self.send_header('Set-Cookie', cookie)
            self.end_headers(); self.wfile.write(raw); self.close_connection = True

        def principal(self):
            if self.headers.get('Host') != origin.removeprefix('http://'):
                raise AppError('BAD_HOST')
            if self.headers.get('Origin') not in (None, origin): raise AppError('BAD_ORIGIN')
            header = self.headers.get('Authorization', '')
            supplied = header.removeprefix('Bearer ') if header.startswith('Bearer ') else ''
            if supplied and secrets.compare_digest(supplied, app.owner_token): return 'owner'
            if supplied and secrets.compare_digest(supplied, app.relay_token): return 'relay'
            cookies = http.cookies.SimpleCookie()
            try: cookies.load(self.headers.get('Cookie', ''))
            except http.cookies.CookieError: pass
            item = cookies.get('aiops_session')
            if item and secrets.compare_digest(item.value, app.session):
                if self.command == 'GET' or (self.headers.get('Origin') == origin and self.headers.get('X-AIOPS-Client') == 'desktop'):
                    return 'owner'
            raise AppError('UNAUTHENTICATED', 'AIOPS 앱을 다시 열어 연결해 주세요.')

        def do_OPTIONS(self): self.send(403, {'error': 'CROSS_ORIGIN_DISABLED'})

        def do_GET(self):
            try:
                if self.headers.get('Host') != origin.removeprefix('http://'): raise AppError('BAD_HOST')
                if self.path in ASSET_TYPES:
                    path, mime = ASSET_TYPES[self.path]
                    return self.send(200, (ASSETS / path).read_bytes(), mime)
                self.principal()
                if self.path == '/api/state': return self.send(200, app.state())
                if self.path == '/api/canonical/capability': return self.send(200, app.canonical.capability())
                if self.path == '/api/host/status':
                    if self.principal()!='owner': raise AppError('OWNER_REQUIRED')
                    if not isinstance(app.canonical.source,mac_authority.LocalSource): raise AppError('MAC_HOST_ADAPTER_REQUIRED')
                    return self.send(200,app.canonical.source.status())
                if self.path.startswith('/api/canonical/') and len(self.path.split('/')) == 4:
                    return self.send(200, app.canonical.public(app.canonical.get(self.path.split('/')[3])))
                if self.path == '/api/handoffs': return self.send(200, [handoff.summary(x) for x in app.store.handoffs()])
                if self.path.startswith('/api/handoffs/'):
                    parts = self.path.split('/')
                    if len(parts) == 5 and parts[4] == 'host-plan':
                        return self.send(200, host_observation.query_plan(app.store.get_handoff(parts[3])))
                if self.path.startswith('/api/handoffs/') and len(self.path.split('/')) == 4:
                    return self.send(200, handoff.summary(app.store.get_handoff(self.path.split('/')[3])))
                if self.path == '/api/jobs': return self.send(200, [app.engine.describe(j, compact=True) for j in app.store.jobs()])
                if self.path.startswith('/api/jobs/'):
                    parts = self.path.split('/')
                    if len(parts) == 4: return self.send(200, app.engine.describe(app.store.get(parts[3]), compact=True))
                    if len(parts) == 5 and parts[4] == 'events': return self.send(200, app.store.events(parts[3]))
                self.send(404, {'error': 'NOT_FOUND'})
            except AppError as exc: self.send(401 if exc.code == 'UNAUTHENTICATED' else 400, {'error': exc.code, 'message': str(exc)})

        def do_POST(self):
            try:
                principal = self.principal()
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json': raise AppError('JSON_REQUIRED')
                raw_size = self.headers.get('Content-Length', '')
                if not raw_size.isdigit() or not 0 < int(raw_size) <= 65536 or self.headers.get('Transfer-Encoding'):
                    raise AppError('INVALID_BODY_LENGTH')
                value = parse_json(self.rfile.read(int(raw_size)).decode('utf-8'), 65536)
                if not isinstance(value, dict): raise AppError('OBJECT_REQUIRED')
                if self.path == '/api/session':
                    if principal != 'owner': raise AppError('OWNER_REQUIRED')
                    return self.send(200, {'ok': True}, cookie='aiops_session=' + app.session + '; HttpOnly; SameSite=Strict; Path=/')
                if self.path.startswith('/api/host/'):
                    if principal!='owner': raise AppError('OWNER_REQUIRED')
                    return self.send(200,app.host_action(self.path.removeprefix('/api/host/'),value))
                if self.path == '/api/canonical/start':
                    if principal != 'owner': raise AppError('OWNER_REQUIRED')
                    return self.send(201, app.canonical_start(value))
                if self.path == '/api/canonical/reconcile':
                    if principal != 'owner': raise AppError('OWNER_REQUIRED')
                    if set(value) != {'request_id'}: raise AppError('NATIVE_START_INVALID')
                    return self.send(200, app.canonical.public(app.canonical.reconcile(value['request_id'])))
                if self.path == '/api/jobs': return self.send(201, app.engine.describe(app.start(value), compact=True))
                if self.path == '/api/handoffs/inspect': return self.send(200, app.inspect_handoff(value))
                if self.path == '/api/handoffs': return self.send(201, app.prepare_handoff(value))
                if self.path == '/api/settings':
                    if principal != 'owner': raise AppError('OWNER_REQUIRED')
                    return self.send(200, app.store.set_settings(value))
                if self.path == '/api/doctor':
                    if principal != 'owner': raise AppError('OWNER_REQUIRED')
                    app.doctor = agents.availability(); return self.send(200, app.doctor)
                if self.path.startswith('/api/jobs/'):
                    parts = self.path.split('/')
                    if len(parts) == 5:
                        action = parts[4]
                        if principal != 'owner' and action != 'pause': raise AppError('OWNER_REQUIRED')
                        return self.send(200, app.engine.describe(app.action(parts[3], action, value), compact=True))
                self.send(404, {'error': 'NOT_FOUND'})
            except (AppError, UnicodeError, ValueError) as exc:
                code = exc.code if isinstance(exc, AppError) else 'INVALID_REQUEST'
                self.send(401 if code == 'UNAUTHENTICATED' else 409, {'error': code, 'message': str(exc) if isinstance(exc, AppError) else '요청 형식을 확인해 주세요.'})
    return Handler


def serve(directory, port):
    directory = private_directory(directory)
    lock = service_lock(directory)
    app = server = thread = None
    previous_signals = {}
    try:
        app = Application(directory)
        origin = 'http://127.0.0.1:' + str(port)
        server = ThreadingHTTPServer(('127.0.0.1', port), handler(app, origin))
        server.daemon_threads = True
        atomic_json(directory / 'endpoint.json', {'origin': origin, 'pid': os.getpid(), 'version': VERSION})
        thread = threading.Thread(target=app.engine.run, daemon=True); thread.start()
        def stop(*_):
            app.engine.stopping.set(); app.engine.wake.set()
            threading.Thread(target=server.shutdown, daemon=True).start()
        for number in (signal.SIGTERM, signal.SIGINT):
            previous_signals[number] = signal.signal(number, stop)
        server.serve_forever(poll_interval=0.5)
    finally:
        for number, previous in previous_signals.items(): signal.signal(number, previous)
        if server is not None: server.server_close()
        if app is not None:
            app.engine.stopping.set(); app.engine.wake.set()
        def release():
            try:
                if thread is not None and thread.ident is not None: thread.join()
                if app is not None: app.store.close()
            finally: os.close(lock)
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        if thread is not None and thread.is_alive():
            # Never admit a new service while an old in-process step still runs.
            threading.Thread(target=release, daemon=False).start()
        else: release()


def local_origin(directory):
    value = read_json(Path(directory) / 'endpoint.json')
    endpoint = value.get('origin') if isinstance(value, dict) else None
    # A forged state file cannot turn the bot bridge into an arbitrary network client.
    import re
    if not isinstance(endpoint, str) or not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{4,5}', endpoint) \
            or not 1024 <= int(endpoint.rsplit(':', 1)[1]) <= 65535:
        raise AppError('INVALID_LOCAL_ENDPOINT')
    return endpoint


def client(directory, path, value=None, owner=False):
    directory = Path(directory)
    endpoint = local_origin(directory)
    secret = token(directory / ('desktop-token' if owner else 'relay-token'))
    req = urllib.request.Request(endpoint + path, data=encoded(value).encode() if value is not None else None,
                                 headers={'Authorization': 'Bearer ' + secret, 'Content-Type': 'application/json'})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args): raise AppError('REDIRECT_REFUSED')
    try:
        # Handoff observation can need four bounded GitHub reads, including pagination.
        timeout = 600 if value is not None and path in ('/api/handoffs', '/api/handoffs/inspect', '/api/canonical/start','/api/host/register','/api/host/generation','/api/host/bundle-prepare','/api/host/start') else 20
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(req, timeout=timeout) as response:
            return parse_json(response.read(16777217).decode(), 16777216)
    except urllib.error.HTTPError as exc:
        data = parse_json(exc.read(65537).decode(), 65536)
        raise AppError(data.get('error', 'HTTP_ERROR'), data.get('message')) from exc


MCP_TOOLS = [
    {'name': 'aiops_handoff_host_plan', 'description': '인계 준비 기록에 묶인 보호 호스트 읽기 전용 조회 목록을 만듭니다. 호스트에 접속하거나 실행을 승인하지 않습니다.',
     'inputSchema': {'type': 'object', 'properties': {'handoff_id': {'type': 'string'}}, 'required': ['handoff_id'], 'additionalProperties': False}},
    {'name': 'aiops_handoff_inspect', 'description': '기존 프로그램과 작업 등록을 읽어 인계 장애물을 확인합니다. 실행 권한을 만들지 않습니다.',
     'inputSchema': {'type': 'object', 'properties': {'repository': {'type': 'string'}}, 'required': ['repository'], 'additionalProperties': False}},
    {'name': 'aiops_handoff_prepare', 'description': '원래 프로그램과 로컬 작업 기록의 참조를 인계 준비 기록으로 보관합니다. 실행하거나 소유권을 변경하지 않습니다. 같은 request_id로 재조회할 수 있습니다.',
     'inputSchema': {'type': 'object', 'properties': {'repository': {'type': 'string'}, 'request_id': {'type': 'string'}, 'source_job_id': {'type': 'string'}}, 'required': ['repository', 'request_id'], 'additionalProperties': False}},
    {'name': 'aiops_handoff_list', 'description': '보관된 인계 준비 기록을 조회합니다.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'aiops_handoff_status', 'description': '특정 인계 준비 시점의 기록을 조회합니다. 현재 실행 권한을 증명하지 않습니다.',
     'inputSchema': {'type': 'object', 'properties': {'handoff_id': {'type': 'string'}}, 'required': ['handoff_id'], 'additionalProperties': False}},
    {'name': 'aiops_start', 'description': '사용자가 지정한 GitHub 레포와 목표로 계획·개발·감사·감리 작업을 시작합니다. 재시도 시 같은 request_id를 사용하세요. 결과는 최종 사용자 검수를 기다리며 자동 병합하지 않습니다.',
     'inputSchema': {'type': 'object', 'properties': {'repository': {'type': 'string'}, 'goal': {'type': 'string'}, 'request_id': {'type': 'string'}}, 'required': ['repository', 'request_id'], 'additionalProperties': False}},
    {'name': 'aiops_list', 'description': '로컬 작업 상태를 한 번 조회합니다.', 'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'aiops_status', 'description': '특정 작업의 결과·질문·PR 링크를 조회합니다.', 'inputSchema': {'type': 'object', 'properties': {'job_id': {'type': 'string'}}, 'required': ['job_id'], 'additionalProperties': False}},
    {'name': 'aiops_pause', 'description': '현재 단계가 끝난 뒤 작업을 멈춥니다.', 'inputSchema': {'type': 'object', 'properties': {'job_id': {'type': 'string'}}, 'required': ['job_id'], 'additionalProperties': False}},
]


def mcp(directory, source=sys.stdin, output=sys.stdout):
    for line in source:
        request = {}
        try:
            request = parse_json(line, 65536)
            if not isinstance(request, dict):
                request = {}; raise AppError('INVALID_RPC_REQUEST')
            method = request.get('method')
            if 'id' not in request: continue
            if not isinstance(request.get('params', {}), dict): raise AppError('INVALID_RPC_PARAMS')
            if method == 'initialize':
                version = (request.get('params') or {}).get('protocolVersion')
                result = {'protocolVersion': version if version in ('2024-11-05', '2025-03-26', '2025-06-18') else '2024-11-05',
                          'capabilities': {'tools': {}}, 'serverInfo': {'name': 'aiops-mac', 'version': VERSION}}
            elif method == 'ping': result = {}
            elif method == 'tools/list': result = {'tools': MCP_TOOLS}
            elif method == 'tools/call':
                params = request.get('params') or {}; name = params.get('name'); args = params.get('arguments') or {}
                if not isinstance(args, dict): raise AppError('INVALID_TOOL_ARGUMENTS')
                if name == 'aiops_start': data = client(directory, '/api/jobs', args)
                elif name in ('aiops_handoff_inspect', 'aiops_handoff_prepare'):
                    inspecting = name == 'aiops_handoff_inspect'
                    value = handoff.request(args, inspect=inspecting)
                    data = client(directory, '/api/handoffs' + ('/inspect' if inspecting else ''), value)
                elif name == 'aiops_handoff_list':
                    if args: raise AppError('INVALID_HANDOFF_REQUEST')
                    data = client(directory, '/api/handoffs')
                elif name in ('aiops_handoff_status', 'aiops_handoff_host_plan'):
                    if set(args) != {'handoff_id'}: raise AppError('INVALID_HANDOFF_REQUEST')
                    data = client(directory, '/api/handoffs/' + handoff.handoff_id(args['handoff_id']) + ('/host-plan' if name == 'aiops_handoff_host_plan' else ''))
                elif name == 'aiops_list': data = client(directory, '/api/jobs')
                elif name in ('aiops_status', 'aiops_pause'):
                    import re
                    key = args.get('job_id', '')
                    if not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{16}', key): raise AppError('INVALID_JOB_ID')
                    data = client(directory, '/api/jobs/' + key + ('/pause' if name == 'aiops_pause' else ''), {} if name == 'aiops_pause' else None)
                else: raise AppError('UNKNOWN_TOOL')
                result = {'content': [{'type': 'text', 'text': encoded(data)}]}
            else: raise AppError('METHOD_NOT_FOUND')
            reply = {'jsonrpc': '2.0', 'id': request['id'], 'result': result}
        except (AppError, OSError, ValueError) as exc:
            reply = {'jsonrpc': '2.0', 'id': request.get('id'), 'error': {'code': -32000, 'message': str(exc) if isinstance(exc, AppError) else type(exc).__name__}}
        output.write(encoded(reply) + '\n'); output.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description='AIOPS Mac — 맡기고, 결과를 검수하세요.')
    parser.add_argument('--data-dir', type=Path, default=default_directory())
    sub = parser.add_subparsers(dest='command', required=True)
    server = sub.add_parser('serve'); server.add_argument('--port', type=int, default=8765)
    sub.add_parser('open'); sub.add_parser('list'); sub.add_parser('mcp'); sub.add_parser('doctor')
    sub.add_parser('audit', help='중지된 서비스의 기존 KIX 작업에 독립 Mac GLM 감사를 한 번 실행/수집; builder 실행 없음').add_argument('job_id')
    start = sub.add_parser('start'); start.add_argument('--repo', required=True); start.add_argument('--goal', default=''); start.add_argument('--request-id', default=None)
    for name in ('status', 'pause'):
        one = sub.add_parser(name); one.add_argument('job_id')
    canonical = sub.add_parser('canonical', help='canonical Mac 이관 후보; 인증 source 미지원 시 실행 차단')
    native_actions = canonical.add_subparsers(dest='canonical_command', required=True)
    native_actions.add_parser('capability')
    for name in ('status', 'reconcile'):
        native_actions.add_parser(name).add_argument('request_id')
    native_start = native_actions.add_parser('start')
    native_start.add_argument('--binding', type=Path, required=True)
    native_start.add_argument('--request-id', required=True)
    host = sub.add_parser('host', help='이 Mac의 원장과 단일 실행 예약; 기존 외부 실행을 종료로 간주하지 않음')
    host_actions=host.add_subparsers(dest='host_command',required=True)
    host_actions.add_parser('status')
    host_init=host_actions.add_parser('initialize'); host_init.add_argument('--mode',choices=['MAC'],required=True)
    host_init.add_argument('--decision',required=True)
    host_gen=host_actions.add_parser('generation',help='사용자 승인으로 원래 단일 root 범위를 새 Mac task/revision으로 격리; 기존 실행 종료를 선언하지 않음')
    host_gen.add_argument('--repo',required=True); host_gen.add_argument('--node',required=True)
    host_gen.add_argument('--generation-id',required=True); host_gen.add_argument('--decision',required=True)
    host_gen.add_argument('--plan-commit',required=True); host_gen.add_argument('--plan-blob',required=True)
    for name in ('bundle-prepare','product-builder'):
        host_actions.add_parser(name).add_argument('--input',type=Path,required=True)
    host_gen.add_argument('--bundle',type=Path)
    for name in ('register','tasks'):
        host_actions.add_parser(name).add_argument('--repo',required=True)
    host_start=host_actions.add_parser('start'); host_start.add_argument('--repo',required=True)
    host_start.add_argument('--task',required=True); host_start.add_argument('--request-id',required=True)
    host_actions.add_parser('stop').add_argument('request_id')
    host_accept=host_actions.add_parser('reconcile-accepted'); host_accept.add_argument('--repo',required=True)
    host_accept.add_argument('--task',required=True)
    host_retry=host_actions.add_parser('retry-delivery'); host_retry.add_argument('--repo',required=True)
    host_retry.add_argument('--task',required=True)
    host_base=host_actions.add_parser('advance-base'); host_base.add_argument('--repo',required=True)
    host_base.add_argument('--decision',required=True)
    host_scope=host_actions.add_parser('receipt-scope'); host_scope.add_argument('--association',type=Path,required=True)
    transfer = sub.add_parser('handoff', help='기존 계획과 기록의 인계 준비; 실행 권한 변경 없음')
    actions = transfer.add_subparsers(dest='handoff_command', required=True)
    probe = actions.add_parser('inspect'); probe.add_argument('--repo', required=True)
    prepare = actions.add_parser('prepare'); prepare.add_argument('--repo', required=True)
    prepare.add_argument('--request-id', required=True); prepare.add_argument('--source-job', default=None)
    actions.add_parser('list')
    status = actions.add_parser('status'); status.add_argument('handoff_id')
    plan = actions.add_parser('host-plan'); plan.add_argument('handoff_id')
    check = actions.add_parser('check-fixture'); check.add_argument('handoff_id'); check.add_argument('--fixture', type=Path, required=True)
    preview = actions.add_parser('preview-start'); preview.add_argument('handoff_id')
    preview.add_argument('--fixture', type=Path, required=True); preview.add_argument('--node', required=True)
    preview.add_argument('--request-id', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'serve':
            if not 1024 <= args.port <= 65535: raise AppError('INVALID_PORT')
            serve(args.data_dir, args.port); return 0
        if args.command == 'mcp': mcp(args.data_dir); return 0
        if args.command == 'audit':
            import mac_glm_runner
            print(encoded(mac_glm_runner.command(args.data_dir,args.job_id))); return 0
        if args.command == 'doctor': result = agents.availability()
        elif args.command == 'open':
            client(args.data_dir, '/api/state', owner=True)
            url = local_origin(args.data_dir) + '/#' + token(args.data_dir / 'desktop-token')
            webbrowser.open(url); return 0
        elif args.command == 'start':
            result = client(args.data_dir, '/api/jobs', {'repository': args.repo, 'goal': args.goal, 'request_id': args.request_id or uuid.uuid4().hex})
        elif args.command == 'list': result = client(args.data_dir, '/api/jobs')
        elif args.command == 'canonical':
            operation = args.canonical_command
            if operation == 'capability': result = client(args.data_dir, '/api/canonical/capability', owner=True)
            elif operation == 'start':
                value = {'request_id': args.request_id, 'binding': native_transfer.binding(read_json(args.binding, 65536))}
                result = client(args.data_dir, '/api/canonical/start', value, owner=True)
            elif operation == 'reconcile':
                result = client(args.data_dir, '/api/canonical/reconcile', {'request_id': args.request_id}, owner=True)
            else: result = client(args.data_dir, '/api/canonical/' + args.request_id, owner=True)
        elif args.command == 'host':
            operation=args.host_command
            if operation=='status': result=client(args.data_dir,'/api/host/status',owner=True)
            else:
                if operation=='initialize': value={'mode':args.mode,'decision':args.decision}
                elif operation=='generation': value={'repository':args.repo,'node':args.node,'generation_id':args.generation_id,
                    'decision':args.decision,'plan_commit':args.plan_commit,'plan_blob':args.plan_blob}
                elif operation in ('bundle-prepare','product-builder'): value=read_json(args.input,262144)
                elif operation=='receipt-scope': value=read_json(args.association,65536)
                elif operation=='start': value={'repository':args.repo,'task_id':args.task,'request_id':args.request_id}
                elif operation=='stop': value={'request_id':args.request_id}
                elif operation in ('reconcile-accepted','retry-delivery'): value={'repository':args.repo,'task_id':args.task}
                elif operation=='advance-base': value={'repository':args.repo,'decision':args.decision}
                else: value={'repository':args.repo}
                if operation=='generation' and args.bundle: value['bundle']=read_json(args.bundle,65536)
                result=client(args.data_dir,'/api/host/'+operation,value,owner=True)
        elif args.command == 'handoff':
            if args.handoff_command == 'inspect':
                result = client(args.data_dir, '/api/handoffs/inspect', handoff.request({'repository': args.repo}, inspect=True))
            elif args.handoff_command == 'prepare':
                result = client(args.data_dir, '/api/handoffs', handoff.request(
                    {'repository': args.repo, 'request_id': args.request_id, 'source_job_id': args.source_job}))
            elif args.handoff_command == 'list': result = client(args.data_dir, '/api/handoffs')
            elif args.handoff_command == 'host-plan':
                result = client(args.data_dir, '/api/handoffs/' + handoff.handoff_id(args.handoff_id) + '/host-plan')
            elif args.handoff_command in ('check-fixture', 'preview-start'):
                record = client(args.data_dir, '/api/handoffs/' + handoff.handoff_id(args.handoff_id))
                with args.fixture.open('rb') as source: raw = source.read(host_observation.LIMIT + 1)
                evidence = parse_json(raw.decode('utf-8'), host_observation.LIMIT)
                result = (host_observation.assess(record, evidence) if args.handoff_command == 'check-fixture' else
                          host_observation.preview_start(record, evidence, args.node, args.request_id))
            else: result = client(args.data_dir, '/api/handoffs/' + handoff.handoff_id(args.handoff_id))
        else:
            import re
            if not re.fullmatch(r'[0-9a-f]{16}', args.job_id): raise AppError('INVALID_JOB_ID')
            result = client(args.data_dir, '/api/jobs/' + args.job_id + ('/pause' if args.command == 'pause' else ''), {} if args.command == 'pause' else None)
        print(encoded(result)); return 0
    except (AppError, OSError, ValueError) as exc:
        print(encoded({'error': exc.code if isinstance(exc, AppError) else type(exc).__name__, 'message': str(exc) if isinstance(exc, AppError) else 'AIOPS 앱의 실행 상태를 확인해 주세요.'}), file=sys.stderr)
        return 2


if __name__ == '__main__': raise SystemExit(main())
