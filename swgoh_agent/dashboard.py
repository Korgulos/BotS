"""Loopback-only management API and dashboard, without extra dependencies."""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import contextmanager
import hashlib
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
from string import Template
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

from .capture import WindowCapture
from .config import ROOT, Settings
from .runtime import AgentLease
from .store import Store
from .vision import TemplateMatcher, detect_state
from .action_types import ACTION_KINDS, INPUT_KINDS, parameters, within_frame, postconditions
from .textvision import TextMatcher

COLLECTIONS = {'states': 'state_definitions', 'actions': 'actions',
               'observations': 'observation_fields', 'prompts': 'prompts'}
IDENTIFIER = re.compile(r'^[A-Za-z0-9_.-]{1,80}$')
STATIC = Path(__file__).with_name('web')


def number(value, label, minimum, maximum, nullable=False):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{label}: expected a number')
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f'{label}: expected {minimum}–{maximum}')
    return value


def flag(value, label):
    if type(value) is not bool and not (type(value) is int and value in (0, 1)):
        raise ValueError(f'{label}: expected true/false')
    return int(value)


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError('Name must use 1–80 letters, numbers, underscores, dots or hyphens')
    return value


class Dashboard:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.db_path = self.root / 'agent.sqlite3'
        self.config_path = self.root / 'config.json'
        self.lease_path = self.root / 'logs' / 'agent.lock'
        self.lock = threading.RLock()
        self.process = None
        self.output_handle = None
        self.mode = None
        self.once = False
        self.output_path = self.root / 'logs' / 'dashboard-agent.log'
        self.image_bytes = None
        self.image_revision = None
        self.preview = None

    @contextmanager
    def db(self, write=False):
        db = sqlite3.connect(self.db_path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def tables(self, db):
        return {key: [dict(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY ' +
                  ('prompt_key' if key == 'prompts' else 'sort_order, name'))]
                for key, table in COLLECTIONS.items()}

    def revision(self, tables):
        return hashlib.sha256(json.dumps(tables, sort_keys=True).encode()).hexdigest()

    def config(self):
        return json.loads(self.config_path.read_text())

    def template_path(self, ref):
        if not isinstance(ref, str) or not ref:
            raise ValueError('Template reference is required')
        path = Path(self.config().get('templates', {}).get(ref, ref))
        path = (self.root / path).resolve()
        if not path.is_relative_to((self.root / 'templates').resolve()):
            raise ValueError('Templates must be inside templates/')
        return path

    def matcher(self):
        return TemplateMatcher(lambda ref: self.template_path(ref) if ref else None,
                               self.config().get('template_preprocessing', {}))

    def templates(self):
        refs = dict(self.config().get('templates', {}))
        for path in sorted((self.root / 'templates').glob('*.png')):
            refs.setdefault(str(path.relative_to(self.root)), str(path.relative_to(self.root)))
        result = []
        for ref in refs:
            try:
                path = self.template_path(ref)
                usable = path.is_file() and cv2.imread(str(path)) is not None
            except ValueError:
                usable = False
            result.append({'ref': ref, 'available': usable})
        return result

    def status(self):
        with self.lock:
            running = self.process is not None and self.process.poll() is None
            return {'running': running, 'pid': self.process.pid if running else None,
                    'exit_code': self.process.poll() if self.process else None,
                    'mode': self.mode, 'once': self.once,
                    'external_running': not running and AgentLease.busy(self.lease_path)}

    def snapshot(self):
        with self.lock, self.db() as db:
            tables = self.tables(db)
            row = db.execute('SELECT payload_json, observed_at FROM latest_state WHERE id=1').fetchone()
            config_bytes = self.config_path.read_bytes()
            return {'tables': tables, 'revision': self.revision(tables),
                    'config_revision': hashlib.sha256(config_bytes).hexdigest(),
                    'templates': self.templates(), 'config': json.loads(config_bytes),
                    'status': self.status(),
                    'latest': {'payload': json.loads(row['payload_json']),
                               'observed_at': row['observed_at']} if row else None,
                    'preview': self.preview}

    @contextmanager
    def editable(self):
        with self.lock:
            if self.status()['running']:
                raise RuntimeError('Stop the agent before editing or testing rules')
            with AgentLease(self.lease_path):
                yield

    def validate_record(self, collection, record, db):
        if not isinstance(record, dict):
            raise ValueError('Record must be an object')
        key = 'prompt_key' if collection == 'prompts' else 'name'
        name = identifier(record.get(key))
        if collection == 'states':
            if name == 'unknown':
                raise ValueError('unknown is reserved for unrecognized screens')
            ref = record.get('template', '')
            self.template_path(ref)
            enabled = flag(record.get('enabled', False), 'enabled')
            if enabled and not self.matcher().available(ref):
                raise ValueError('An enabled state needs a readable template')
            return {'name': name, 'template': ref, 'enabled': enabled,
                    'threshold': number(record.get('threshold'), 'threshold', 0, 1, True),
                    'sort_order': self.order(record)}
        if collection == 'observations':
            value_type = record.get('value_type', 'integer')
            if value_type not in ('integer', 'number', 'string', 'boolean', 'string or number'):
                raise ValueError('Unsupported observation type')
            return {'name': name, 'description': str(record.get('description', '')),
                    'value_type': value_type, 'sort_order': self.order(record)}
        if collection == 'prompts':
            text = record.get('template')
            if not isinstance(text, str) or not text.strip():
                raise ValueError('Prompt cannot be empty')
            try:
                Template(text).substitute(state='main_menu', template_scores_json='{}',
                                          requested_fields_json='[]')
            except (ValueError, KeyError) as exc:
                raise ValueError(f'Invalid prompt placeholder: {exc}') from exc
            return {'prompt_key': name, 'template': text}
        kind = record.get('kind', 'click')
        if kind not in ACTION_KINDS:
            raise ValueError('Unsupported action kind')
        params = parameters(kind, record.get('parameters', {}))
        settings = Settings.load(self.config_path)
        within_frame(kind, params, *settings.resolution)
        enabled = flag(record.get('enabled', False), 'enabled')
        review = flag(record.get('requires_manual_review', True), 'requires_manual_review')
        states = {r['name']: r['enabled'] for r in db.execute('SELECT name,enabled FROM state_definitions')}
        when = record.get('when_state', [])
        if not isinstance(when, list) or not all(isinstance(x, str) and x in states for x in when):
            raise ValueError('Every initial state must exist')
        if enabled and not when:
            raise ValueError('An enabled action needs an initial state')
        posts = postconditions(record)
        if any(state not in states for state in posts):
            raise ValueError('Every postcondition state must exist')
        ref = record.get('template') or None
        if kind == 'click' and ref:
            self.template_path(ref)
        if kind in INPUT_KINDS and enabled and not review:
            if not posts or any(not states[state] for state in posts):
                raise ValueError('An input action needs at least one enabled postcondition state; all selected states must be enabled')
            if kind == 'click' and (not ref or not self.matcher().available(ref)):
                raise ValueError('A live click needs a readable target template')
        conditions = record.get('conditions', [])
        if not isinstance(conditions, list):
            raise ValueError('Conditions must be a list')
        for condition in conditions:
            if (not isinstance(condition, dict) or not isinstance(condition.get('field'), str)
                    or not condition['field'].strip() or condition.get('operator') not in ('==', '!=', '>', '>=', '<', '<=')
                    or 'value' not in condition):
                raise ValueError('Each condition needs field, operator and value')
        return {'name': name, 'kind': kind, 'description': str(record.get('description', '')),
                'enabled': enabled, 'requires_manual_review': review,
                'template': ref if kind == 'click' else None,
                'when_state_json': json.dumps(list(dict.fromkeys(when))),
                'postcondition_state': posts[0] if kind in INPUT_KINDS and posts else None,
                'postcondition_states_json': json.dumps(posts if kind in INPUT_KINDS else []),
                'parameters_json': json.dumps(params, allow_nan=False),
                'conditions_json': json.dumps(conditions, allow_nan=False),
                'cooldown_seconds': number(record.get('cooldown_seconds', 5), 'cooldown', 0, 3600),
                'min_confidence': number(record.get('min_confidence'), 'confidence', 0, 1, True),
                'sort_order': self.order(record)}

    @staticmethod
    def order(record):
        value = number(record.get('sort_order', 0), 'sort_order', -100000, 100000)
        if int(value) != value:
            raise ValueError('sort_order must be an integer')
        return int(value)

    def mutate(self, payload, delete=False):
        collection = payload.get('collection')
        if collection not in COLLECTIONS:
            raise ValueError('Unknown collection')
        with self.editable(), self.db(write=True) as db:
            if payload.get('revision') != self.revision(self.tables(db)):
                raise RuntimeError('Rules changed. Refresh before saving.')
            table = COLLECTIONS[collection]
            key = 'prompt_key' if collection == 'prompts' else 'name'
            if delete:
                name = identifier(payload.get('name'))
                if collection == 'states':
                    references = [r['name'] for r in db.execute('SELECT * FROM actions')
                                  if name in json.loads(r['when_state_json']) or name in postconditions(dict(r))]
                    if references:
                        raise ValueError('State is used by actions: ' + ', '.join(references))
                if collection == 'prompts' and name == 'visual_observation':
                    raise ValueError('The visual_observation prompt is required by the agent')
                db.execute(f'DELETE FROM {table} WHERE {key}=?', (name,))
            else:
                record = self.validate_record(collection, payload.get('record'), db)
                name = record[key]
                original = payload.get('original_name')
                if original and original != name:
                    raise ValueError('Names cannot be changed in place. Create a new record instead.')
                exists = db.execute(f'SELECT 1 FROM {table} WHERE {key}=?', (name,)).fetchone()
                if exists and not original:
                    raise ValueError('That name already exists')
                if original and not exists:
                    raise RuntimeError('Record no longer exists. Refresh the editor.')
                if collection == 'states' and not record['enabled']:
                    for action in db.execute('SELECT * FROM actions WHERE enabled=1 AND requires_manual_review=0'):
                        if action['kind'] in INPUT_KINDS and name in postconditions(dict(action)):
                            raise ValueError(f"Disable action {action['name']} before disabling its postcondition")
                columns = list(record)
                updates = ','.join(f'{col}=excluded.{col}' for col in columns if col != key)
                db.execute(f'INSERT INTO {table} ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)}) '
                           f'ON CONFLICT({key}) DO UPDATE SET {updates}', tuple(record.values()))
        return self.snapshot()

    def save_config(self, payload):
        config = payload.get('config')
        if not isinstance(config, dict):
            raise ValueError('Configuration must be an object')
        with self.editable():
            current = self.config_path.read_bytes()
            if payload.get('config_revision') != hashlib.sha256(current).hexdigest():
                raise RuntimeError('Configuration changed. Refresh before saving.')
            encoded = json.dumps(config, indent=2, allow_nan=False) + '\n'
            temp = self.config_path.with_suffix('.json.tmp')
            try:
                temp.write_text(encoded)
                settings = Settings.load(temp)
                for section, fields in {
                    'agent': ('dry_run', 'require_ollama_for_actions'),
                    'input': ('enabled', 'auto_focus'),
                    'ollama': ('enabled', 'send_image'),
                }.items():
                    for field in fields:
                        if field in config.get(section, {}):
                            flag(config[section][field], field)
                for value, label, low, high in (
                    (settings.agent.loop_interval_seconds, 'loop interval', .1, 3600),
                    (settings.agent.max_frame_age_seconds, 'frame age', .1, 3600),
                    (settings.agent.postcondition_timeout_seconds, 'postcondition timeout', .1, 3600),
                    (settings.agent.template_threshold, 'state threshold', 0, 1),
                    (settings.agent.min_confidence_for_action, 'action threshold', 0, 1),
                    (settings.ollama.timeout_seconds, 'model timeout', 1, 3600),
                    (settings.agent.max_actions_per_session, 'action budget', 1, 10000),
                    (settings.agent.max_consecutive_errors, 'error limit', 1, 100),
                    (settings.input.min_seconds_between_clicks, 'click interval', 0, 3600),
                ):
                    number(value, label, low, high)
                if len(settings.resolution) != 2 or any(type(x) is not int or x <= 0 for x in settings.resolution):
                    raise ValueError('Resolution needs two positive integers')
                temp.replace(self.config_path)
            finally:
                temp.unlink(missing_ok=True)
        return self.snapshot()

    def logs(self):
        result = []
        path = self.root / 'logs' / 'agent.jsonl'
        if path.exists():
            with path.open(errors='replace') as file:
                lines = deque(file, maxlen=100)
            for line in lines:
                try:
                    result.append(json.loads(line))
                except ValueError:
                    pass
        output = ''
        if self.output_path.exists():
            with self.output_path.open(errors='replace') as file:
                output = ''.join(deque(file, maxlen=100))
        return {'events': result, 'output': output}

    def start(self, payload):
        if payload.get('mode') not in ('dry-run', 'live'):
            raise ValueError('Select dry-run or live mode explicitly')
        once = payload.get('once', False)
        flag(once, 'once')
        with self.lock:
            if self.status()['running'] or AgentLease.busy(self.lease_path):
                raise RuntimeError('An agent is already running')
            if self.output_handle:
                self.output_handle.close()
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            self.output_handle = self.output_path.open('w')
            args = [sys.executable, '-u', str(self.root / 'agent.py'), '--' + payload['mode']]
            if once:
                args.append('--once')
            self.process = subprocess.Popen(args, cwd=self.root, stdout=self.output_handle,
                                            stderr=subprocess.STDOUT, start_new_session=True)
            self.mode, self.once = payload['mode'], bool(once)
            return self.status()

    def stop(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                try:
                    # The worker owns a new session. Interrupt input subprocesses
                    # too, so a text batch cannot keep typing after Stop.
                    os.killpg(self.process.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
            return self.status()

    def capture(self, fresh=True):
        with self.editable():
            settings = Settings.load(self.config_path)
            if fresh:
                settings = replace(settings, capture=replace(
                    settings.capture, output=self.root / 'logs' / 'ui_capture.png'))
                frame = WindowCapture.from_settings(settings).grab()
                image = frame.image
                origin = frame.origin
                source = 'capture'
            else:
                data = settings.capture.output.read_bytes()
                image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
                origin = None
                source = 'last_screen'
            if image is None:
                raise ValueError('No readable screenshot. Capture a fresh frame.')
            ok, encoded = cv2.imencode('.png', image)
            if not ok:
                raise ValueError('Cannot encode screenshot')
            self.image_bytes = encoded.tobytes()
            self.image_revision = hashlib.sha256(self.image_bytes).hexdigest()
            with self.db() as db:
                states = [dict(row) for row in db.execute('SELECT * FROM state_definitions WHERE enabled=1 ORDER BY sort_order,name')]
            from .store import StateDef
            detection = detect_state(image, [StateDef(s['name'], s['template'], s['threshold']) for s in states],
                                     self.matcher(), settings.agent.template_threshold)
            self.preview = {'revision': self.image_revision, 'width': image.shape[1],
                            'height': image.shape[0], 'source': source, 'origin': origin,
                            'state': detection.state, 'scores': detection.scores}
            return self.preview

    def crop(self, payload):
        with self.editable():
            if not self.image_bytes or payload.get('image_revision') != self.image_revision:
                raise RuntimeError('Screenshot changed. Select the crop again.')
            name = identifier(payload.get('name'))
            if name in ('.', '..'):
                raise ValueError('Invalid template name')
            coords = [payload.get(k) for k in ('x', 'y', 'width', 'height')]
            if not all(type(x) is int for x in coords):
                raise ValueError('Crop coordinates must be integers')
            x, y, w, h = coords
            image = cv2.imdecode(np.frombuffer(self.image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
            if min(x, y) < 0 or min(w, h) <= 0 or x+w > image.shape[1] or y+h > image.shape[0]:
                raise ValueError('Crop is outside the screenshot')
            path = self.root / 'templates' / (name + '.png')
            path.parent.mkdir(parents=True, exist_ok=True)
            # New files only: never silently overwrite a template used by rules.
            data = cv2.imencode('.png', image[y:y+h, x:x+w])[1].tobytes()
            with path.open('xb') as file:
                file.write(data)
            return {'template': str(path.relative_to(self.root))}

    def test_action(self, payload):
        with self.editable():
            if not self.image_bytes or payload.get('image_revision') != self.image_revision:
                raise RuntimeError('Capture a screenshot before testing')
            with self.db() as db:
                row = db.execute('SELECT * FROM actions WHERE name=?', (payload.get('name'),)).fetchone()
                if not row:
                    raise ValueError('Save the action before testing it')
                action = dict(row)
            image = cv2.imdecode(np.frombuffer(self.image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
            with self.db() as db:
                from .store import StateDef
                states = [StateDef(r['name'], r['template'], r['threshold']) for r in db.execute(
                    'SELECT * FROM state_definitions WHERE enabled=1 ORDER BY sort_order,name')]
            state = detect_state(image, states, self.matcher(),
                                 Settings.load(self.config_path).agent.template_threshold).state
            conditions = json.loads(action['conditions_json'])
            minimum = action['min_confidence']
            if minimum is None:
                minimum = Settings.load(self.config_path).agent.min_confidence_for_action
            params = parameters(action['kind'], json.loads(action['parameters_json']))
            reasons = []
            try:
                within_frame(action['kind'], params, image.shape[1], image.shape[0])
            except ValueError as exc:
                reasons.append(str(exc))
            point, end_point, score = None, None, None
            if not reasons:
                if action['kind'] == 'click':
                    score, point = self.matcher().match(image, action['template'])
                elif action['kind'] == 'click_text':
                    match = TextMatcher().match(image, params)
                    score, point = match.score, match.point
                    minimum = params['min_text_confidence']
                    if match.reason:
                        reasons.append(match.reason)
                elif action['kind'] == 'drag':
                    point = (params['start_x'], params['start_y'])
                    end_point = (params['end_x'], params['end_y'])
            if not action['enabled']: reasons.append('Action is disabled')
            if action['requires_manual_review']: reasons.append('Manual review flag is set')
            if state not in json.loads(action['when_state_json']): reasons.append('Initial state does not match')
            if conditions: reasons.append('AI conditions are not evaluated by the offline test')
            if action['kind'] in INPUT_KINDS:
                if action['kind'] in ('click', 'click_text') and (point is None or score < minimum):
                    reasons.append('Target score is below threshold')
                if not postconditions(action): reasons.append('Postcondition is missing')
                if point and not (20 <= point[0] < image.shape[1]-20 and 20 <= point[1] < image.shape[0]-20):
                    reasons.append('Target is too close to the edge')
            return {'name': action['name'], 'state': state, 'score': score, 'required': minimum,
                    'point': point, 'end_point': end_point, 'kind': action['kind'],
                    'parameters': params, 'postcondition_states': postconditions(action), 'reasons': reasons,
                    'eligible_in_preview': not reasons, 'input_sent': False,
                    'note': 'Preview only. Live execution rechecks age, focus, pointer and postcondition.'}


class Handler(BaseHTTPRequestHandler):
    server_version = 'SWGOHLocalUI/1'

    def log_message(self, *args):
        pass

    def send(self, status, data, content_type='application/json'):
        if content_type == 'application/json':
            data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def dispatch(self, write=False):
        try:
            port = self.server.server_port
            allowed = {f'127.0.0.1:{port}', f'localhost:{port}'}
            if self.headers.get('Host') not in allowed:
                return self.send(403, {'error': 'Invalid Host'})
            url = urlparse(self.path)
            path = url.path
            if not write and path in ('/', '/app.js', '/style.css'):
                filename, mime = {'/': ('index.html', 'text/html; charset=utf-8'),
                                  '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                                  '/style.css': ('style.css', 'text/css; charset=utf-8')}[path]
                data = (STATIC / filename).read_bytes()
                if path == '/':
                    data = data.replace(b'__TOKEN__', self.server.token.encode())
                return self.send(200, data, mime)
            token = self.headers.get('X-UI-Token') or parse_qs(url.query).get('token', [''])[0]
            if not secrets.compare_digest(token, self.server.token):
                return self.send(403, {'error': 'Missing UI token. Reload the page.'})
            origin = self.headers.get('Origin')
            if origin and origin not in {f'http://{host}' for host in allowed}:
                return self.send(403, {'error': 'Cross-origin requests are not allowed'})
            app = self.server.app
            if write:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 1024 * 1024:
                    raise ValueError('Expected a JSON body smaller than 1 MB')
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict): raise ValueError('Expected a JSON object')
                routes = {'/api/save': lambda: app.mutate(payload),
                          '/api/delete': lambda: app.mutate(payload, True),
                          '/api/config': lambda: app.save_config(payload),
                          '/api/start': lambda: app.start(payload), '/api/stop': app.stop,
                          '/api/capture': lambda: app.capture(payload.get('fresh', True)),
                          '/api/crop': lambda: app.crop(payload),
                          '/api/test': lambda: app.test_action(payload)}
                if path not in routes: return self.send(404, {'error': 'Not found'})
                return self.send(200, routes[path]())
            if path == '/api/snapshot':
                data = app.snapshot()
                return self.send(200, data)
            if path == '/api/status':
                with app.db() as db:
                    row = db.execute('SELECT * FROM latest_state WHERE id=1').fetchone()
                return self.send(200, {'status': app.status(), 'latest': dict(row) if row else None, **app.logs()})
            if path == '/api/live-image':
                source = Settings.load(app.config_path).capture.output.resolve()
                if not source.is_relative_to(app.root.resolve()):
                    raise ValueError('Screenshot must be inside the workspace')
                return self.send(200, source.read_bytes(), 'image/png')
            if path == '/api/image':
                if app.image_bytes is None: raise ValueError('Capture a screenshot first')
                return self.send(200, app.image_bytes, 'image/png')
            if path == '/api/template':
                ref = parse_qs(url.query).get('ref', [''])[0]
                return self.send(200, app.template_path(ref).read_bytes(), 'image/png')
            return self.send(404, {'error': 'Not found'})
        except (ValueError, KeyError, TypeError, sqlite3.IntegrityError) as exc:
            self.send(400, {'error': str(exc)})
        except RuntimeError as exc:
            self.send(409, {'error': str(exc)})
        except (OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
            self.send(500, {'error': str(exc)})

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch(True)


def main():
    parser = argparse.ArgumentParser(description='Local SWGOH rule editor')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    Store(Settings.load().raw).initialize()
    app = Dashboard()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.app = app
    server.token = secrets.token_urlsafe(32)
    print(f'SWGOH UI: http://127.0.0.1:{args.port}', flush=True)
    print('Agent is stopped. Select observation or live mode in the dashboard.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        if app.process:
            try:
                app.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                app.process.terminate()
                app.process.wait(timeout=5)
        if app.output_handle:
            app.output_handle.close()
        server.server_close()


if __name__ == '__main__':
    main()
