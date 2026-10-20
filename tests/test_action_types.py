import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

import cv2
import numpy as np

from swgoh_agent.action_types import parameters, within_frame
from swgoh_agent.actions import ActionExecutor, Clicker
from swgoh_agent.capture import Frame, Window, ClickNotReady
from swgoh_agent.config import Settings, ROOT
from swgoh_agent.store import ActionRule, migrate_actions, migrate_postconditions
from swgoh_agent.textvision import TextMatcher, TextMatch


def tsv(rows):
    header = 'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n'
    return header + ''.join(f'5\t1\t1\t1\t{line}\t1\t{x}\t20\t100\t40\t{conf}\t{text}\n'
                            for line, x, conf, text in rows)


class ParameterTests(unittest.TestCase):
    def test_key_normalization_and_invalid_arguments(self):
        self.assertEqual(parameters('key', {'keys': 'ctrl+enter'})['keys'], 'ctrl+Return')
        for key in ('--window', 'super+x', 'a b', 'Return;rm', 'ctrl+', 'F25'):
            with self.assertRaises(ValueError): parameters('key', {'keys': key})

    def test_text_is_literal_and_single_line(self):
        value = 'Ćirilica Ж — $(literal) --window'
        self.assertEqual(parameters('type_text', {'text': value})['text'], value)
        for value in ('', 'a\nb', 'a\x00b'):
            with self.assertRaises(ValueError): parameters('type_text', {'text': value})

    def test_drag_boundaries_all_directions_and_diagonal(self):
        for end in ((40, 100), (260, 100), (150, 40), (150, 160), (240, 150)):
            params = parameters('drag', dict(start_x=150, start_y=100, end_x=end[0], end_y=end[1]))
            within_frame('drag', params, 300, 200)
        with self.assertRaises(ValueError):
            within_frame('drag', parameters('drag', dict(start_x=150, start_y=100, end_x=300, end_y=100)), 300, 200)
        with self.assertRaises(ValueError): parameters('drag', dict(start_x=1, start_y=2, end_x=1, end_y=2))


class OCRTests(unittest.TestCase):
    def test_case_insensitive_exact_phrase_and_real_box(self):
        match = TextMatcher.from_tsv(tsv([(1, 20, 98, 'Galactic'), (1, 140, 96, 'War')]),
                                    parameters('click_text', {'text': 'GALACTIC war', 'region': [30, 40, 200, 100]}), (30, 40))
        self.assertEqual(match.point, (95, 60))
        self.assertEqual(match.score, .96)

    def test_duplicate_and_substring_are_rejected(self):
        params = parameters('click_text', {'text': 'Store'})
        self.assertIsNone(TextMatcher.from_tsv(tsv([(1, 20, 99, 'Restore')]), params).point)
        match = TextMatcher.from_tsv(tsv([(1, 20, 99, 'Store'), (2, 40, 99, 'Store')]), params)
        self.assertIsNone(match.point)
        self.assertIn('ambiguous', match.reason)

    def test_low_confidence_is_rejected(self):
        match = TextMatcher.from_tsv(tsv([(1, 20, 45, 'Store')]), parameters('click_text', {'text': 'Store'}))
        self.assertIsNone(match.point)
        self.assertIn('confidence', match.reason)

    def test_white_label_fallback_preserves_roi_coordinates(self):
        matcher = TextMatcher()
        params = parameters('click_text', {'text': 'CLAIM', 'region': [50, 60, 200, 100]})
        empty = tsv([]).encode()
        found = tsv([(1, 20, 92, 'CLAIM')]).encode()
        with patch('swgoh_agent.textvision.subprocess.run', side_effect=[Mock(stdout=empty), Mock(stdout=found)]) as run:
            match = matcher.match(np.zeros((200,300,3), np.uint8), params)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(match.point, (85, 80))
        self.assertEqual(match.score, .92)

    def test_fallback_never_overrides_ambiguity_or_low_confidence(self):
        params = parameters('click_text', {'text': 'CLAIM'})
        for rows in ([(1, 20, 99, 'CLAIM'), (2, 40, 99, 'CLAIM')], [(1, 20, 45, 'CLAIM')]):
            with patch('swgoh_agent.textvision.subprocess.run', return_value=Mock(stdout=tsv(rows).encode())) as run:
                match = TextMatcher().match(np.zeros((200,300,3), np.uint8), params)
            self.assertEqual(run.call_count, 1)
            self.assertIsNone(match.point)

    def test_both_passes_missing_never_return_target(self):
        with patch('swgoh_agent.textvision.subprocess.run', return_value=Mock(stdout=tsv([]).encode())) as run:
            match = TextMatcher().match(np.zeros((200,300,3), np.uint8), parameters('click_text', {'text': 'CLAIM'}))
        self.assertEqual(run.call_count, 2)
        self.assertIsNone(match.point)
        self.assertEqual(match.reason, 'Text not found')

    def test_real_tesseract_on_generated_text(self):
        image = np.full((140, 650, 3), 255, dtype=np.uint8)
        cv2.putText(image, 'SHIPMENTS', (40, 90), cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 0, 0), 3)
        match = TextMatcher().match(image, parameters('click_text', {'text': 'Shipments', 'min_text_confidence': .5}))
        self.assertIsNotNone(match.point)
        self.assertTrue(40 < match.point[0] < 450)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.image = np.zeros((200, 300, 3), np.uint8)
        self.frame = Frame(self.image, (120, 80), time.monotonic(), Window('0x123', 120, 80, 300, 200))
        self.rule = ActionRule('trial', ('menu',), None, '', True, False, 'next', 0, None, ())
        self.driver = Mock()
        for name in ('key', 'type_text', 'drag'):
            getattr(self.driver, name).side_effect = lambda *args, before_click: before_click()
        self.driver.click.side_effect = lambda *args, before_click: before_click()
        self.ocr = Mock()
        self.ocr.match.return_value = TextMatch(.98, (100, 70))
        self.executor = ActionExecutor(replace(Settings.load().agent, dry_run=False), Mock(), self.driver, Mock(), self.ocr)

    def run_rule(self, kind, params):
        return self.executor.run(self.frame, 'menu', {}, [replace(self.rule, kind=kind, parameters=params)],
                                 refresh=lambda: (self.frame, 'menu'), guard=Mock())

    def test_dispatch_key_text_and_drag_with_window_translation(self):
        for kind, params, method, prefix in (
            ('key', {'keys': 'Escape'}, 'key', 'key_sent:'),
            ('type_text', {'text': 'Ćirilica Ж'}, 'type_text', 'text_entered:'),
            ('drag', dict(start_x=100, start_y=70, end_x=200, end_y=140), 'drag', 'dragged:'),
        ):
            self.executor.pending = None
            result = self.run_rule(kind, params)
            self.assertTrue(result.startswith(prefix), result)
            getattr(self.driver, method).assert_called_once()
        self.assertEqual(self.driver.drag.call_args.args[:2], ((220, 150), (320, 220)))

    def test_text_click_rechecks_ocr_on_fresh_image(self):
        self.assertEqual(self.run_rule('click_text', {'text': 'Shipments'}), 'clicked:trial')
        self.assertEqual(self.ocr.match.call_count, 2)
        self.assertEqual(self.driver.click.call_args.args, (220, 150))

    def test_ambiguous_ocr_never_clicks(self):
        self.ocr.match.return_value = TextMatch(0, None, 'Text is ambiguous')
        self.assertIn('ambiguous', self.run_rule('click_text', {'text': 'Store'}))
        self.driver.click.assert_not_called()

    def test_invalid_database_params_never_dispatch(self):
        self.assertIn('no eligible action', self.run_rule('key', {'keys': '--window'}))
        self.driver.key.assert_not_called()

    def test_all_new_kinds_respect_dry_run(self):
        self.executor.cfg = replace(self.executor.cfg, dry_run=True)
        for kind in ('key', 'type_text', 'drag', 'click_text'):
            self.assertIn('dry_run', self.run_rule(kind, {}))
        self.assertFalse(self.driver.mock_calls)


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.driver = Clicker(replace(Settings.load().input, enabled=True))

    def test_drag_releases_button_on_failure(self):
        self.driver._move = Mock(side_effect=[None, RuntimeError('move failed')])
        with patch('swgoh_agent.actions.subprocess.run') as run:
            with self.assertRaisesRegex(RuntimeError, 'move failed'):
                self.driver.drag((100, 100), (200, 100), 200, Mock())
        self.assertEqual(run.call_args_list[0].args[0], ['ydotool', 'click', '0x40'])
        self.assertEqual(run.call_args_list[-1].args[0], ['ydotool', 'click', '0x80'])

    def test_drag_focus_loss_after_press_is_not_safe_to_retry(self):
        self.driver._move = Mock()
        guard = Mock(side_effect=[None, None, ClickNotReady('focus lost')])
        with patch('swgoh_agent.actions.subprocess.run') as run:
            with self.assertRaisesRegex(RuntimeError, 'after button-down') as error:
                self.driver.drag((100, 100), (200, 100), 200, guard)
        self.assertNotIsInstance(error.exception, ClickNotReady)
        self.assertEqual(run.call_args_list[-1].args[0][-1], '0x80')

    def test_successful_drag_moves_in_steps_and_releases(self):
        self.driver._move = Mock()
        with patch('swgoh_agent.actions.subprocess.run') as run, patch('swgoh_agent.actions.time.sleep'):
            self.driver.drag((100, 100), (200, 150), 200, Mock())
        self.assertEqual(self.driver._move.call_args.args, (200, 150))
        self.assertEqual(run.call_args_list[-1].args[0][-1], '0x80')

    def test_key_release_even_when_keydown_fails(self):
        with patch('swgoh_agent.actions.subprocess.run', side_effect=[RuntimeError('failed'), Mock()]) as run:
            with self.assertRaises(RuntimeError): self.driver.key('ctrl+a', 80, Mock())
        self.assertEqual(run.call_args_list[-1].args[0], ['xdotool', 'keyup', 'ctrl+a'])

    def test_text_uses_stdin_and_partial_input_halts(self):
        text = '$(literal) Ж'
        guard = Mock(side_effect=[None, ClickNotReady('focus lost')])
        with patch('swgoh_agent.actions.subprocess.run') as run:
            with self.assertRaisesRegex(RuntimeError, 'partial input') as error:
                self.driver.type_text(text, 30, guard)
        self.assertNotIsInstance(error.exception, ClickNotReady)
        self.assertEqual(run.call_args.kwargs['input'], text[:8])
        self.assertNotIn(text[:8], run.call_args.args[0])


class MigrationTests(unittest.TestCase):
    def test_postcondition_migration_preserves_legacy_and_existing_lists(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        schema = '\n'.join(line for line in (ROOT/'agent_schema.sql').read_text().splitlines()
                           if 'postcondition_states_json' not in line)
        db.executescript(schema)
        db.execute("INSERT INTO actions(name,postcondition_state) VALUES ('old','inbox')")
        db.execute("INSERT INTO actions(name) VALUES ('empty')")
        migrate_postconditions(db)
        self.assertEqual(json.loads(db.execute("SELECT postcondition_states_json FROM actions WHERE name='old'").fetchone()[0]), ['inbox'])
        self.assertEqual(db.execute("SELECT postcondition_states_json FROM actions WHERE name='empty'").fetchone()[0], '[]')
        db.execute("UPDATE actions SET postcondition_states_json='[\"inbox\",\"quests\"]' WHERE name='old'")
        migrate_postconditions(db)
        self.assertEqual(json.loads(db.execute("SELECT postcondition_states_json FROM actions WHERE name='old'").fetchone()[0]), ['inbox','quests'])

    def test_old_check_constraint_migrates_preserving_rules_and_index(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        schema = (ROOT / 'agent_schema.sql').read_text().replace(
            "'click', 'click_text', 'key', 'type_text', 'drag', 'observe'", "'click', 'observe'")
        schema = '\n'.join(line for line in schema.splitlines() if 'parameters_json' not in line)
        db.executescript(schema)
        db.execute("INSERT INTO actions(name,kind) VALUES ('original','observe')")
        db.execute('CREATE INDEX custom_action_order ON actions(sort_order)')
        migrate_actions(db)
        self.assertEqual(db.execute("SELECT kind,parameters_json FROM actions WHERE name='original'").fetchone(), ('observe', '{}'))
        for kind in ('click_text', 'key', 'type_text', 'drag'):
            db.execute('INSERT INTO actions(name,kind) VALUES (?,?)', (kind,kind))
        migrate_actions(db)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM actions').fetchone()[0], 5)
        self.assertIsNotNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='custom_action_order'").fetchone())
