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

import agents
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
        self.engine = engine or Engine(self.store)
        self.owner_token = token(self.store.directory / 'desktop-token')
        self.relay_token = token(self.store.directory / 'relay-token')
        self.session = secrets.token_urlsafe(32)
        self.doctor = agents.availability()

    def state(self):
        return {'version': VERSION, 'settings': self.store.settings(), 'jobs': self.store.jobs(),
                'connections': self.doctor, 'data_directory': str(self.store.directory),
                'cli_path': str(Path(__file__).resolve()), 'python_path': sys.executable}

    def start(self, value):
        if not self.doctor['git']['installed'] or not self.doctor['gh']['installed'] or not self.doctor['github_authenticated']:
            raise AppError('GITHUB_CONNECTION_REQUIRED', '연결 설정에서 GitHub 로그인을 완료한 뒤 다시 시작해 주세요.')
        needed = {v['provider'] for v in self.store.settings()['roles'].values()}
        if any(not self.doctor[x]['installed'] for x in needed):
            raise AppError('MODEL_CONNECTION_REQUIRED', '모델 설정에서 선택한 실행 도구를 먼저 설치해 주세요.')
        job = self.store.create(value); self.engine.wake.set(); return job


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
                if self.path == '/api/jobs': return self.send(200, app.store.jobs())
                if self.path.startswith('/api/jobs/'):
                    parts = self.path.split('/')
                    if len(parts) == 4: return self.send(200, app.store.get(parts[3]))
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
                if self.path == '/api/jobs': return self.send(201, app.start(value))
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
                        result = app.store.action(parts[3], action, value)
                        app.engine.wake.set(); return self.send(200, result)
                self.send(404, {'error': 'NOT_FOUND'})
            except (AppError, UnicodeError, ValueError) as exc:
                code = exc.code if isinstance(exc, AppError) else 'INVALID_REQUEST'
                self.send(401 if code == 'UNAUTHENTICATED' else 409, {'error': code, 'message': str(exc) if isinstance(exc, AppError) else '요청 형식을 확인해 주세요.'})
    return Handler


def serve(directory, port):
    directory = private_directory(directory)
    lock = service_lock(directory)
    app = Application(directory)
    origin = 'http://127.0.0.1:' + str(port)
    server = ThreadingHTTPServer(('127.0.0.1', port), handler(app, origin))
    server.daemon_threads = True
    atomic_json(directory / 'endpoint.json', {'origin': origin, 'pid': os.getpid(), 'version': VERSION})
    thread = threading.Thread(target=app.engine.run, daemon=True); thread.start()
    def stop(*_):
        app.engine.stopping.set(); app.engine.wake.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    try: server.serve_forever(poll_interval=0.5)
    finally:
        stop(); thread.join(timeout=5); server.server_close(); os.close(lock)


def client(directory, path, value=None, owner=False):
    directory = Path(directory)
    endpoint = read_json(directory / 'endpoint.json')['origin']
    # A forged state file cannot turn the bot bridge into an arbitrary network client.
    import re
    if not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{4,5}', endpoint): raise AppError('INVALID_LOCAL_ENDPOINT')
    secret = token(directory / ('desktop-token' if owner else 'relay-token'))
    req = urllib.request.Request(endpoint + path, data=encoded(value).encode() if value is not None else None,
                                 headers={'Authorization': 'Bearer ' + secret, 'Content-Type': 'application/json'})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args): raise AppError('REDIRECT_REFUSED')
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as response:
            return parse_json(response.read(4194305).decode())
    except urllib.error.HTTPError as exc:
        data = parse_json(exc.read(65537).decode(), 65536)
        raise AppError(data.get('error', 'HTTP_ERROR'), data.get('message')) from exc


MCP_TOOLS = [
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
    start = sub.add_parser('start'); start.add_argument('--repo', required=True); start.add_argument('--goal', default=''); start.add_argument('--request-id', default=None)
    for name in ('status', 'pause'):
        one = sub.add_parser(name); one.add_argument('job_id')
    args = parser.parse_args(argv)
    try:
        if args.command == 'serve':
            if not 1024 <= args.port <= 65535: raise AppError('INVALID_PORT')
            serve(args.data_dir, args.port); return 0
        if args.command == 'mcp': mcp(args.data_dir); return 0
        if args.command == 'doctor': result = agents.availability()
        elif args.command == 'open':
            client(args.data_dir, '/api/state', owner=True)
            url = read_json(args.data_dir / 'endpoint.json')['origin'] + '/#' + token(args.data_dir / 'desktop-token')
            webbrowser.open(url); return 0
        elif args.command == 'start':
            result = client(args.data_dir, '/api/jobs', {'repository': args.repo, 'goal': args.goal, 'request_id': args.request_id or uuid.uuid4().hex})
        elif args.command == 'list': result = client(args.data_dir, '/api/jobs')
        else:
            import re
            if not re.fullmatch(r'[0-9a-f]{16}', args.job_id): raise AppError('INVALID_JOB_ID')
            result = client(args.data_dir, '/api/jobs/' + args.job_id + ('/pause' if args.command == 'pause' else ''), {} if args.command == 'pause' else None)
        print(encoded(result)); return 0
    except (AppError, OSError, ValueError) as exc:
        print(encoded({'error': exc.code if isinstance(exc, AppError) else type(exc).__name__, 'message': str(exc) if isinstance(exc, AppError) else 'AIOPS 앱의 실행 상태를 확인해 주세요.'}), file=sys.stderr)
        return 2


if __name__ == '__main__': raise SystemExit(main())
