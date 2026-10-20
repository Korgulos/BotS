import sqlite3
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np

from swgoh_agent.actions import ActionExecutor, Clicker
from swgoh_agent.capture import Frame, Window, WindowCapture
from swgoh_agent.config import Settings, ROOT
from swgoh_agent.store import ActionRule, Store, Rules, StateDef
from swgoh_agent.runner import Agent
from swgoh_agent.vision import Match, TemplateMatcher, detect_state


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = replace(Settings.load().agent, dry_run=False)
        self.image = np.zeros((200, 300, 3), dtype=np.uint8)
        self.frame = Frame(self.image, (120, 80), time.monotonic(),
                           Window('0x123', 120, 80, 300, 200))
        self.action = ActionRule('go', ('menu',), 'button', '', True, False,
                                 'next', 0, .94, ())
        self.matcher = Mock()
        self.matcher.match.return_value = Match(.99, (100, 70))
        self.clicker = Mock()
        self.clicker.click.side_effect = lambda x, y, before_click: before_click()
        self.guard = Mock()
        self.executor = ActionExecutor(self.cfg, self.matcher, self.clicker, Mock())

    def run_action(self, frame=None, fresh=None, state='menu'):
        return self.executor.run(frame or self.frame, state, {}, [self.action],
                                 refresh=lambda: (fresh or self.frame, state),
                                 guard=self.guard)

    def test_window_coordinates_and_guard(self):
        self.assertEqual(self.run_action(), 'clicked:go')
        self.clicker.click.assert_called_once()
        self.assertEqual(self.clicker.click.call_args.args, (220, 150))
        self.guard.assert_called_once_with(self.frame)

    def test_stale_observation(self):
        old = self.frame._replace(captured_at=time.monotonic() - 100)
        self.assertIn('stale_observation', self.run_action(frame=old))
        self.clicker.click.assert_not_called()

    def test_target_disappears(self):
        self.matcher.match.side_effect = [(.99, (100, 70)), (0, None)]
        self.assertIn('target_changed', self.run_action())
        self.clicker.click.assert_not_called()

    def test_changed_screen(self):
        result = self.executor.run(self.frame, 'menu', {}, [self.action],
                                  refresh=lambda: (self.frame, 'other'), guard=self.guard)
        self.assertIn('screen_changed', result)
        self.clicker.click.assert_not_called()

    def test_fresh_origin_used_after_window_move(self):
        fresh = self.frame._replace(origin=(250, 90),
                                   window=Window('0x123', 250, 90, 300, 200))
        self.run_action(fresh=fresh)
        self.assertEqual(self.clicker.click.call_args.args, (350, 160))
        self.guard.assert_called_once_with(fresh)

    def test_pending_blocks_duplicate_then_confirms(self):
        self.run_action()
        self.assertIn('awaiting_postcondition', self.run_action())
        self.assertEqual(self.executor.check_pending('next'), 'confirmed:go')
        self.assertEqual(self.clicker.click.call_count, 1)
        self.assertIsNone(self.executor.pending)

    def test_timeout_halts_without_retry(self):
        self.run_action()
        action, ready, deadline = self.executor.pending
        with patch('swgoh_agent.actions.time.monotonic', return_value=deadline + 1):
            self.assertIn('postcondition_failed', self.run_action())
        self.assertIn('halted', self.run_action())
        self.assertEqual(self.clicker.click.call_count, 1)

    def test_partial_input_failure_halts(self):
        self.clicker.click.side_effect = RuntimeError('input failed')
        with self.assertRaises(RuntimeError):
            self.run_action()
        self.assertIn('halted', self.run_action())
        self.assertEqual(self.clicker.click.call_count, 1)

    def test_frame_expires_during_input_delay(self):
        def delayed(x, y, before_click):
            with patch('swgoh_agent.actions.time.monotonic',
                       return_value=self.frame.captured_at + 100):
                before_click()
        self.clicker.click.side_effect = delayed
        with self.assertRaisesRegex(RuntimeError, 'expired'):
            self.run_action()
        self.guard.assert_not_called()

    def test_no_postcondition_means_no_click(self):
        self.action = replace(self.action, postcondition_state=None)
        self.assertEqual(self.run_action(), 'no eligible action')
        self.clicker.click.assert_not_called()

    def test_dry_run_never_clicks(self):
        self.executor.cfg = replace(self.cfg, dry_run=True)
        self.assertIn('dry_run', self.run_action())
        self.clicker.click.assert_not_called()


class CaptureGuardTests(unittest.TestCase):
    def setUp(self):
        self.capture = WindowCapture('game', (300, 200), Path('/tmp/test.png'), 5)
        self.window = Window('0x123', 120, 80, 300, 200)
        self.frame = Frame(np.zeros((200, 300, 3)), (120, 80), 0, self.window)
        self.capture.find_window = Mock(return_value=self.window)
        self.capture._run = Mock(return_value=Mock(stdout='_NET_ACTIVE_WINDOW(WINDOW): window id # 0x123\n'))

    def test_active_window(self):
        self.capture.assert_click_target(self.frame)

    def test_inactive_window(self):
        self.capture._run.return_value.stdout = 'window id # 0x456'
        with self.assertRaisesRegex(RuntimeError, 'not active'):
            self.capture.assert_click_target(self.frame)

    def test_window_moved(self):
        self.capture.find_window.return_value = self.window._replace(x=200)
        with self.assertRaisesRegex(RuntimeError, 'moved'):
            self.capture.assert_click_target(self.frame)

    def test_clicker_guards_before_move_and_press(self):
        guard = Mock()
        with patch('swgoh_agent.actions.subprocess.run') as run, patch('swgoh_agent.actions.time.sleep'):
            Clicker(replace(Settings.load().input, enabled=True)).click(100, 200, guard)
        self.assertEqual(guard.call_count, 2)
        self.assertEqual(run.call_count, 2)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / 'test.sqlite3'
        self.patch = patch('swgoh_agent.store.DATABASE', self.database)
        self.patch.start()
        self.legacy = patch('swgoh_agent.store.LEGACY_STATE_FILE', Path(self.temp.name) / 'absent.json')
        self.legacy.start()

    def tearDown(self):
        self.patch.stop()
        self.legacy.stop()
        self.temp.cleanup()

    def test_fresh_seed_and_idempotency(self):
        store = Store()
        store.initialize()
        first = store.load_rules()
        self.assertIn('visual_observation', first.prompts)
        self.assertEqual(len(first.actions), 2)
        store.initialize()
        self.assertEqual(store.load_rules(), first)

    def test_failed_import_rolls_back_and_can_retry(self):
        with self.assertRaises(ValueError):
            Store({'states': []}).initialize()
        with sqlite3.connect(self.database) as db:
            for table in ('state_definitions', 'actions', 'prompts', 'app_metadata'):
                self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)
        Store().initialize()
        self.assertIn('visual_observation', Store().load_rules().prompts)

    def test_failed_seed_rolls_back(self):
        seed = Path(self.temp.name) / 'broken.sql'
        seed.write_text((ROOT / 'agent_seed.sql').read_text() + '\nINSERT INTO nonexistent VALUES (1);\n')
        with patch('swgoh_agent.store.SEED_FILE', seed):
            with self.assertRaises(sqlite3.OperationalError):
                Store().initialize()
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM actions').fetchone()[0], 0)
        Store().initialize()
        self.assertEqual(len(Store().load_rules().actions), 2)

    def test_action_template_mappings_exist(self):
        Store().initialize()
        settings = Settings.load()
        for action in Store().load_rules().actions:
            self.assertTrue(settings.resolve_template(action.template).is_file(), action.name)


class ReplayTests(unittest.TestCase):
    def test_saved_screen_detection_and_click_target(self):
        rng = np.random.default_rng(42)
        template = rng.integers(0, 256, (20, 30, 3), dtype=np.uint8)
        screen = np.zeros((200, 300, 3), dtype=np.uint8)
        screen[60:80, 90:120] = template
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'button.png'
            cv2.imwrite(str(path), template)
            screenshot = Path(directory) / 'screen.png'
            cv2.imwrite(str(screenshot), screen)
            matcher = TemplateMatcher(lambda ref: path)
            image = cv2.imread(str(screenshot))
            detected = detect_state(image, [StateDef('menu', 'button', .94)], matcher, .91)
            self.assertEqual(detected.state, 'menu')
            self.assertEqual(matcher.match(image, 'button').point, (105, 70))
            missing = TemplateMatcher(lambda ref: Path(directory) / 'absent.png')
            self.assertFalse(missing.available('button'))
            self.assertEqual(detect_state(image, [StateDef('menu', 'missing', .94)],
                                          missing, .91).state, 'unknown')


class RunnerTests(unittest.TestCase):
    def make_agent(self):
        # Construct without opening logs, database, or the desktop.
        agent = Agent.__new__(Agent)
        agent.settings = Settings.load()
        agent.log = Mock()
        agent.matcher = Mock()
        agent.store = Mock()
        agent.capture = Mock(spec=WindowCapture)
        agent.ollama = Mock()
        agent.executor = ActionExecutor(replace(agent.settings.agent, dry_run=False),
                                        agent.matcher, Mock(), agent.log)
        image = np.zeros((200, 300, 3), dtype=np.uint8)
        frame = Frame(image, (120, 80), time.monotonic(), Window('0x123', 120, 80, 300, 200))
        agent.capture.grab.return_value = frame
        agent.matcher.match.return_value = Match(.99, (100, 70))
        action = ActionRule('go', ('menu',), 'button', '', True, False, 'next', 0, .94, ())
        agent.store.load_rules.return_value = Rules((StateDef('menu', 'button', .94),),
                                                   (action,), (), {})
        agent.ollama.observe.return_value = {}
        agent.executor.clicker.click.side_effect = lambda x, y, before_click: before_click()
        return agent

    def test_step_refreshes_after_model_and_pending_skips_model(self):
        agent = self.make_agent()
        agent.step()
        self.assertEqual(agent.capture.grab.call_count, 2)
        agent.capture.assert_click_target.assert_called_once()
        self.assertEqual(agent.executor.clicker.click.call_args.args, (220, 150))
        agent.step()
        self.assertEqual(agent.ollama.observe.call_count, 1)
        self.assertEqual(agent.executor.clicker.click.call_count, 1)

    def test_slow_model_never_sends_click(self):
        agent = self.make_agent()
        frame = agent.capture.grab.return_value
        def slow_observe(*args):
            agent.executor.cfg = replace(agent.executor.cfg, max_frame_age_seconds=.001)
            return {}
        agent.ollama.observe.side_effect = slow_observe
        agent.capture.grab.return_value = frame._replace(captured_at=time.monotonic() - 1)
        agent.step()
        agent.executor.clicker.click.assert_not_called()

    def test_startup_reports_missing_and_blocks_live_mode(self):
        agent = self.make_agent()
        rules = agent.store.load_rules.return_value
        rules = replace(rules, actions=())
        agent.matcher.available.return_value = False
        agent.validate_templates(rules)
        agent.log.assert_called_with('missing_template', definition=unittest.mock.ANY)
        agent.settings = replace(agent.settings, agent=replace(agent.settings.agent, dry_run=False))
        with self.assertRaisesRegex(ValueError, 'missing templates'):
            agent.validate_templates(rules)


if __name__ == '__main__':
    unittest.main()
