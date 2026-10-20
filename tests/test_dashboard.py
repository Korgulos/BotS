import hashlib
from http import HTTPStatus
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from swgoh_agent.config import ROOT
from swgoh_agent.dashboard import Dashboard, Handler
from swgoh_agent.runtime import AgentLease


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'templates').mkdir()
        rng = np.random.default_rng(12)
        self.image = np.zeros((200, 300, 3), dtype=np.uint8)
        templates = {}
        for name, x, y in [('main_menu', 30, 30), ('shipments_screen', 160, 130),
                           ('shipments_button', 100, 70), ('resources', 20, 120)]:
            template = rng.integers(0, 256, (15, 25, 3), dtype=np.uint8)
            path = self.root / 'templates' / (name + '.png')
            cv2.imwrite(str(path), template)
            templates[name] = str(path)
            if name != 'shipments_screen':
                self.image[y:y+15, x:x+25] = template
        self.screen_path = self.root / 'last_screen.png'
        cv2.imwrite(str(self.screen_path), self.image)
        self.config = json.loads((ROOT / 'config.json').read_text())
        self.config['templates'] = templates
        self.config['capture']['output'] = str(self.screen_path)
        self.config['resolution'] = [300, 200]
        self.config['template_preprocessing'] = {}
        (self.root / 'config.json').write_text(json.dumps(self.config))
        with sqlite3.connect(self.root / 'agent.sqlite3') as db:
            db.executescript((ROOT / 'agent_schema.sql').read_text())
            db.executescript((ROOT / 'agent_seed.sql').read_text())
        self.app = Dashboard(self.root)

    def save(self, collection, record, original=None):
        return self.app.mutate({'collection': collection, 'record': record,
                                'original_name': original,
                                'revision': self.app.snapshot()['revision']})

    def action(self):
        return {'name': 'trial', 'kind': 'click', 'enabled': True,
                'requires_manual_review': False, 'when_state': ['main_menu'],
                'postcondition_state': 'shipments_screen', 'template': 'shipments_button',
                'cooldown_seconds': 8, 'min_confidence': .94, 'conditions': []}

    def test_snapshot_and_action_roundtrip(self):
        result = self.save('actions', self.action())
        row = next(r for r in result['tables']['actions'] if r['name'] == 'trial')
        self.assertEqual(json.loads(row['when_state_json']), ['main_menu'])
        self.assertEqual(row['kind'], 'click')
        action = self.action()
        action['cooldown_seconds'] = 3
        self.save('actions', action, 'trial')
        with self.app.db() as db:
            self.assertEqual(db.execute("SELECT cooldown_seconds FROM actions WHERE name='trial'").fetchone()[0], 3)

    def test_new_action_kinds_roundtrip_and_preview(self):
        preview = self.app.capture(False)
        for kind, params in (
            ('key', {'keys': 'Escape', 'hold_ms': 100}),
            ('type_text', {'text': 'Želim test', 'delay_ms': 20}),
            ('drag', {'start_x': 100, 'start_y': 70, 'end_x': 200, 'end_y': 140, 'duration_ms': 500}),
            ('click_text', {'text': 'Store', 'min_text_confidence': .9, 'region': [0, 0, 200, 100]}),
        ):
            action = self.action()
            action.update(name='trial_'+kind, kind=kind, template=None, parameters=params)
            snapshot = self.save('actions', action)
            saved = next(r for r in snapshot['tables']['actions'] if r['name']==action['name'])
            self.assertEqual(json.loads(saved['parameters_json'])['keys' if kind=='key' else 'start_x' if kind=='drag' else 'text'],
                             params['keys' if kind=='key' else 'start_x' if kind=='drag' else 'text'])
            self.assertEqual(saved['postcondition_state'], 'shipments_screen')
            if kind != 'click_text':
                result = self.app.test_action({'name': action['name'], 'image_revision': preview['revision']})
                self.assertFalse(result['input_sent'])
                self.assertTrue(result['eligible_in_preview'])
                if kind=='drag': self.assertEqual(result['end_point'], (200, 140))

    def test_new_input_actions_require_postcondition_and_valid_parameters(self):
        for kind, params in (('key', {'keys': 'Escape'}), ('type_text', {'text': 'hello'}),
                             ('drag', {'start_x': 100, 'start_y': 70, 'end_x': 200, 'end_y': 140}),
                             ('click_text', {'text': 'Store'})):
            action = self.action()
            action.update(kind=kind, parameters=params, postcondition_state=None)
            with self.assertRaises(ValueError): self.save('actions', action)
        action = self.action()
        action.update(kind='key', parameters={'keys': '--window'})
        with self.assertRaises(ValueError): self.save('actions', action)

    def test_multiple_postconditions_roundtrip_preview_and_reference_protection(self):
        action = self.action()
        action['postcondition_states'] = ['shipments_screen', 'main_menu', 'main_menu']
        result = self.save('actions', action)
        saved = next(r for r in result['tables']['actions'] if r['name']=='trial')
        self.assertEqual(json.loads(saved['postcondition_states_json']), ['shipments_screen', 'main_menu'])
        preview = self.app.capture(False)
        checked = self.app.test_action({'name': 'trial', 'image_revision': preview['revision']})
        self.assertEqual(checked['postcondition_states'], ['shipments_screen', 'main_menu'])
        # main_menu is not the primary postcondition, but must still be protected.
        state = next(r for r in result['tables']['states'] if r['name']=='main_menu')
        state['enabled'] = False
        with self.assertRaises(ValueError): self.save('states', state, 'main_menu')
        # Remove every initial-state reference to main_menu to isolate final-state protection.
        with self.app.db(write=True) as db:
            db.execute("UPDATE actions SET when_state_json='[]'")
        with self.assertRaisesRegex(ValueError, 'used by actions'):
            self.app.mutate({'collection':'states', 'name':'main_menu',
                             'revision':self.app.snapshot()['revision']}, delete=True)

    def test_multi_postconditions_reject_unknown_empty_and_wrong_type(self):
        for states in (['missing'], [], 'main_menu', [None]):
            action = self.action()
            action['postcondition_states'] = states
            with self.assertRaises(ValueError): self.save('actions', action)

    def test_stale_revision_cannot_overwrite(self):
        revision = self.app.snapshot()['revision']
        self.save('observations', {'name': 'balance', 'value_type': 'integer'})
        with self.assertRaisesRegex(RuntimeError, 'Rules changed'):
            self.app.mutate({'collection': 'actions', 'record': self.action(), 'revision': revision})

    def test_reference_validation_and_deletion(self):
        action = self.action()
        action['postcondition_state'] = 'missing'
        with self.assertRaises(ValueError): self.save('actions', action)
        with self.assertRaisesRegex(ValueError, 'used by actions'):
            self.app.mutate({'collection': 'states', 'name': 'main_menu',
                             'revision': self.app.snapshot()['revision']}, delete=True)

    def test_invalid_numeric_values_are_not_written(self):
        before = self.app.snapshot()['revision']
        for value in (float('nan'), -1, 1.5, True):
            action = self.action()
            action['min_confidence'] = value
            with self.assertRaises(ValueError): self.save('actions', action)
        self.assertEqual(before, self.app.snapshot()['revision'])

    def test_conditions_validation_and_observe_never_has_target(self):
        action = self.action()
        action['conditions'] = [{'field': 'values.credits', 'operator': 'execute', 'value': 1}]
        with self.assertRaises(ValueError): self.save('actions', action)
        action.update(kind='observe', conditions=[])
        result = self.save('actions', action)
        row = next(r for r in result['tables']['actions'] if r['name'] == 'trial')
        self.assertIsNone(row['template'])
        self.assertIsNone(row['postcondition_state'])

    def test_prompt_and_observation_edit(self):
        self.save('prompts', {'prompt_key': 'extra', 'template': 'State=${state}; $$ literal'})
        with self.assertRaises(ValueError):
            self.save('prompts', {'prompt_key': 'bad', 'template': '${unknown_variable}'})
        self.save('observations', {'name': 'counter', 'description': 'Visible counter', 'value_type': 'integer'})
        with self.assertRaises(ValueError):
            self.app.mutate({'collection': 'prompts', 'name': 'visual_observation',
                             'revision': self.app.snapshot()['revision']}, delete=True)

    def test_workspace_lease_blocks_edits_and_second_process(self):
        with AgentLease(self.app.lease_path):
            self.assertTrue(self.app.status()['external_running'])
            with self.assertRaises(RuntimeError): self.save('actions', self.action())
            with self.assertRaises(RuntimeError): self.app.start({'mode': 'live', 'once': True})
        self.assertFalse(self.app.status()['external_running'])

    def test_template_paths_cannot_escape_workspace(self):
        for ref in ('../config.json', str(self.root / 'config.json'), 'templates/../../etc/passwd'):
            with self.assertRaises(ValueError): self.app.template_path(ref)
        (self.root / 'templates' / 'escape.png').symlink_to(self.root / 'config.json')
        with self.assertRaises(ValueError): self.app.template_path('templates/escape.png')

    def test_capture_crop_revision_bounds_and_no_overwrite(self):
        preview = self.app.capture(fresh=False)
        self.assertEqual(preview['state'], 'main_menu')
        payload = {'name': 'new_target', 'x': 100, 'y': 70, 'width': 25, 'height': 15,
                   'image_revision': preview['revision']}
        result = self.app.crop(payload)
        self.assertEqual(result['template'], 'templates/new_target.png')
        with self.assertRaises(FileExistsError): self.app.crop(payload)
        with self.assertRaises(RuntimeError): self.app.crop({**payload, 'image_revision': 'stale'})
        with self.assertRaises(ValueError): self.app.crop({**payload, 'name': 'outside', 'width': 1000})
        self.assertEqual(cv2.imread(str(self.root / result['template'])).shape, (15, 25, 3))

    def test_preview_never_dispatches_and_uses_current_rules(self):
        self.save('actions', self.action())
        preview = self.app.capture(False)
        with patch('swgoh_agent.dashboard.subprocess.Popen') as popen:
            result = self.app.test_action({'name': 'trial', 'image_revision': preview['revision']})
            popen.assert_not_called()
        self.assertFalse(result['input_sent'])
        self.assertTrue(result['eligible_in_preview'])
        self.assertEqual(result['point'], (112, 77))
        state = next(r for r in self.app.snapshot()['tables']['states'] if r['name'] == 'main_menu')
        state['enabled'] = False
        self.save('states', state, 'main_menu')
        result = self.app.test_action({'name': 'trial', 'image_revision': preview['revision']})
        self.assertEqual(result['state'], 'unknown')
        self.assertFalse(result['eligible_in_preview'])

    def test_delete_unreferenced_state(self):
        self.save('states', {'name': 'draft', 'template': 'main_menu', 'enabled': False})
        self.app.mutate({'collection': 'states', 'name': 'draft',
                         'revision': self.app.snapshot()['revision']}, delete=True)
        self.assertNotIn('draft', [r['name'] for r in self.app.snapshot()['tables']['states']])

    def test_config_revision_and_validation(self):
        snapshot = self.app.snapshot()
        config = json.loads(json.dumps(self.config))
        config['agent']['loop_interval_seconds'] = 4
        self.app.save_config({'config': config, 'config_revision': snapshot['config_revision']})
        with self.assertRaises(RuntimeError):
            self.app.save_config({'config': config, 'config_revision': snapshot['config_revision']})
        config['agent']['max_frame_age_seconds'] = -1
        before = self.app.config_path.read_bytes()
        with self.assertRaises(ValueError):
            self.app.save_config({'config': config, 'config_revision': hashlib.sha256(before).hexdigest()})
        self.assertEqual(self.app.config_path.read_bytes(), before)

    def test_worker_explicit_mode_once_and_stop(self):
        with self.assertRaises(ValueError): self.app.start({})
        process = Mock(pid=123)
        process.poll.return_value = None
        with patch('swgoh_agent.dashboard.subprocess.Popen', return_value=process) as popen:
            status = self.app.start({'mode': 'dry-run', 'once': True})
            self.assertTrue(status['running'])
            self.assertIn('--dry-run', popen.call_args.args[0])
            self.assertIn('--once', popen.call_args.args[0])
            with self.assertRaises(RuntimeError): self.app.start({'mode': 'live'})
            with patch('swgoh_agent.dashboard.os.killpg') as interrupt:
                self.app.stop()
                interrupt.assert_called_once_with(123, __import__('signal').SIGINT)
        self.app.output_handle.close()

    def handler(self, path, headers=None, body=None):
        handler = Handler.__new__(Handler)
        handler.server = SimpleNamespace(server_port=8765, token='secret', app=self.app)
        handler.path = path
        raw = json.dumps(body).encode() if body is not None else b''
        handler.headers = {'Host': '127.0.0.1:8765', 'Content-Length': str(len(raw)), **(headers or {})}
        handler.rfile = BytesIO(raw)
        handler.send = Mock()
        handler.dispatch(write=body is not None)
        return handler.send.call_args.args

    def test_http_rejects_rebinding_csrf_and_missing_token(self):
        self.assertEqual(self.handler('/api/snapshot')[0], 403)
        self.assertEqual(self.handler('/api/snapshot', {'Host': 'attacker.example', 'X-UI-Token': 'secret'})[0], 403)
        self.assertEqual(self.handler('/api/stop', {'X-UI-Token': 'secret', 'Origin': 'https://attacker.example'}, {})[0], 403)
        self.assertEqual(self.handler('/api/snapshot', {'X-UI-Token': 'secret'})[0], 200)

    def test_static_page_and_media_cannot_read_arbitrary_files(self):
        page = self.handler('/')
        self.assertEqual(page[0], 200)
        self.assertIn(b'content="secret"', page[1])
        self.assertNotIn(b'__TOKEN__', page[1])
        self.assertEqual(self.handler('/api/template?token=secret&ref=../config.json')[0], 400)
        self.assertEqual(self.handler('/../../config.json?token=secret')[0], 404)


if __name__ == '__main__':
    unittest.main()
