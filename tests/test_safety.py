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
from swgoh_agent.capture import ClickNotReady, Frame, Window, WindowCapture
from swgoh_agent.config import Settings, ROOT
from swgoh_agent.store import ActionRule, Store, Rules, StateDef
from swgoh_agent.runner import Agent
from swgoh_agent.vision import Detection, Match, TemplateMatcher, detect_state


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

    def test_auto_focus_precedes_fresh_capture_and_click(self):
        events = []
        def prepare(frame):
            events.append('focus')
        def refresh():
            events.append('capture')
            return self.frame, 'menu'
        self.clicker.click.side_effect = lambda x, y, before_click: events.append('click')
        result = self.executor.run(self.frame, 'menu', {}, [self.action],
                                   prepare=prepare, refresh=refresh, guard=self.guard)
        self.assertEqual(result, 'clicked:go')
        self.assertEqual(events, ['focus', 'capture', 'click'])

    def test_auto_focus_not_attempted_for_rejected_target_or_dry_run(self):
        prepare = Mock()
        self.matcher.match.return_value = Match(.8, (100, 70))
        self.executor.run(self.frame, 'menu', {}, [self.action], prepare=prepare,
                          refresh=Mock(), guard=self.guard)
        prepare.assert_not_called()
        self.matcher.match.return_value = Match(.99, (100, 70))
        self.executor.cfg = replace(self.cfg, dry_run=True)
        self.executor.run(self.frame, 'menu', {}, [self.action], prepare=prepare,
                          refresh=Mock(), guard=self.guard)
        prepare.assert_not_called()

    def test_failed_auto_focus_defers_without_input(self):
        prepare = Mock(side_effect=ClickNotReady('focus denied'))
        refresh = Mock()
        result = self.executor.run(self.frame, 'menu', {}, [self.action],
                                   prepare=prepare, refresh=refresh, guard=self.guard)
        self.assertIn('click_deferred:focus denied', result)
        refresh.assert_not_called()
        self.clicker.click.assert_not_called()
        self.assertFalse(self.executor.halted)

    def test_low_score_reports_required_threshold_and_sends_no_input(self):
        self.matcher.match.return_value = Match(.928, (100, 70))
        result = self.run_action()
        self.assertIn('template_score=0.928', result)
        self.assertIn('required=0.940', result)
        self.clicker.click.assert_not_called()
        self.executor.log.assert_called_with(
            'action_rejected', name='go',
            reason='template_below_threshold_or_missing', score=.928,
            required=.94, point=(100, 70))

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

    def test_any_selected_postcondition_can_confirm(self):
        for final in ('next', 'inbox'):
            self.executor.pending = None
            self.action = replace(self.action, postcondition_states=('next', 'inbox'))
            self.run_action()
            self.assertEqual(self.executor.check_pending(final), 'confirmed:go')
            self.assertIsNone(self.executor.pending)

    def test_explicit_empty_postconditions_do_not_fall_back_to_legacy(self):
        self.action = replace(self.action, postcondition_states=())
        self.assertEqual(self.run_action(), 'no eligible action')
        self.clicker.click.assert_not_called()

    def test_unrelated_state_cannot_confirm_multi_postcondition(self):
        self.action = replace(self.action, postcondition_states=('next', 'inbox'))
        self.run_action()
        self.assertIn('awaiting_postcondition', self.executor.check_pending('other'))
        self.assertIsNotNone(self.executor.pending)
        self.assertEqual(self.clicker.click.call_count, 1)

    def test_multi_postcondition_failure_logs_all_expected_states(self):
        self.action = replace(self.action, postcondition_states=('next', 'inbox'))
        self.run_action()
        deadline = self.executor.pending[2]
        with patch('swgoh_agent.actions.time.monotonic', return_value=deadline+1):
            self.assertIn('postcondition_failed', self.executor.check_pending('other'))
        self.executor.log.assert_called_with('postcondition_failed', name='go',
                                            expected=['next', 'inbox'], state='other')

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
        self.assertIn("click_deferred:Frame expired", self.run_action())
        self.assertFalse(self.executor.halted)
        self.guard.assert_not_called()

    def test_inactive_window_defers_and_recovers_after_focus(self):
        self.guard.side_effect = ClickNotReady('Game window is not active')
        self.assertIn('click_deferred:', self.run_action())
        self.assertFalse(self.executor.halted)
        self.assertIsNone(self.executor.pending)
        self.assertEqual(self.executor.actions_done, 0)
        self.guard.side_effect = None
        self.assertEqual(self.run_action(), 'clicked:go')
        self.assertEqual(self.executor.actions_done, 1)

    def test_real_clicker_focus_loss_before_press_never_sends_button(self):
        self.executor.clicker = Clicker(replace(Settings.load().input, enabled=True))
        self.guard.side_effect = [None, ClickNotReady('focus lost')]
        with patch('swgoh_agent.actions.subprocess.run', return_value=Mock(stdout='X=220\nY=150\n')) as run, patch('swgoh_agent.actions.time.sleep'):
            self.assertIn('click_deferred:', self.run_action())
            self.assertEqual(run.call_count, 2)
            self.assertFalse(any(call.args[0][1] == 'click' for call in run.call_args_list))
        self.assertFalse(self.executor.halted)
        self.assertEqual(self.executor.actions_done, 0)

    def test_real_clicker_inactive_window_sends_no_input(self):
        self.executor.clicker = Clicker(replace(Settings.load().input, enabled=True))
        self.guard.side_effect = ClickNotReady('inactive')
        with patch('swgoh_agent.actions.subprocess.run') as run:
            self.assertIn('click_deferred:', self.run_action())
            run.assert_not_called()
        self.assertFalse(self.executor.halted)

    def test_wrong_pointer_position_never_sends_button_and_does_not_halt(self):
        self.executor.clicker = Clicker(replace(Settings.load().input, enabled=True))
        with patch('swgoh_agent.actions.subprocess.run',
                   return_value=Mock(stdout='X=1920\nY=1\n')) as run, patch('swgoh_agent.actions.time.sleep'):
            result = self.run_action()
        self.assertIn('Pointer missed target', result)
        self.assertIn('actual=(1920, 1)', result)
        self.assertFalse(any(call.args[0][1] == 'click' for call in run.call_args_list))
        self.assertFalse(self.executor.halted)
        self.assertEqual(self.executor.actions_done, 0)

    def test_unreadable_pointer_position_never_sends_button(self):
        self.executor.clicker = Clicker(replace(Settings.load().input, enabled=True))
        with patch('swgoh_agent.actions.subprocess.run',
                   return_value=Mock(stdout='invalid')) as run, patch('swgoh_agent.actions.time.sleep'):
            self.assertIn('Cannot verify pointer position', self.run_action())
        self.assertFalse(any(call.args[0][1] == 'click' for call in run.call_args_list))
        self.assertEqual(self.executor.actions_done, 0)

    def test_no_postcondition_means_no_click(self):
        self.action = replace(self.action, postcondition_state=None)
        self.assertEqual(self.run_action(), 'no eligible action')
        self.clicker.click.assert_not_called()

    def test_resource_rule_records_without_click(self):
        self.action = replace(self.action, kind="observe", template=None,
                              postcondition_state=None)
        self.executor.observe('menu', {'values': {'credits': 120, 'crystals': None}},
                              [self.action])
        self.executor.log.assert_called_once_with(
            'resource_observation', name='go', state='menu',
            values={'credits': 120, 'crystals': None})
        self.assertEqual(self.run_action(), 'no eligible action')
        self.clicker.click.assert_not_called()
        self.assertEqual(self.executor.actions_done, 0)

    def test_resource_rule_respects_disabled_and_failed_observation(self):
        rule = replace(self.action, kind='observe', enabled=False)
        self.executor.observe('menu', {'values': {'credits': 120}}, [rule])
        self.executor.observe('menu', None, [replace(rule, enabled=True)])
        self.executor.log.assert_not_called()
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

    def test_auto_focus_uses_captured_window_id_and_waits_for_confirmation(self):
        self.capture.assert_click_target = Mock(side_effect=[ClickNotReady('inactive'), None])
        self.capture.focus_window(self.frame)
        self.capture._run.assert_called_once_with(['wmctrl', '-i', '-a', '0x123'], 'wmctrl')
        self.assertEqual(self.capture.assert_click_target.call_count, 2)

    def test_auto_focus_does_nothing_when_already_active(self):
        self.capture.focus_window(self.frame)
        self.assertEqual(self.capture._run.call_count, 1)
        self.assertEqual(self.capture._run.call_args.args[0][0], 'xprop')

    def test_auto_focus_denied_is_bounded(self):
        self.capture.assert_click_target = Mock(side_effect=ClickNotReady('denied'))
        with patch('swgoh_agent.capture.time.sleep'):
            with self.assertRaisesRegex(ClickNotReady, 'denied'):
                self.capture.focus_window(self.frame)
        self.assertEqual(self.capture.assert_click_target.call_count, 11)

    def test_auto_focus_rejects_replaced_window(self):
        self.capture.find_window.return_value = self.window._replace(id='0x456')
        with self.assertRaises(ClickNotReady):
            self.capture.focus_window(self.frame)
        self.capture._run.assert_not_called()

    def test_clicker_guards_before_move_and_press(self):
        guard = Mock()
        with patch('swgoh_agent.actions.subprocess.run', return_value=Mock(stdout='X=100\nY=200\n')) as run, patch('swgoh_agent.actions.time.sleep'):
            Clicker(replace(Settings.load().input, enabled=True)).click(100, 200, guard)
        self.assertEqual(guard.call_count, 2)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args_list[0].args[0], ['xdotool', 'mousemove', '--sync', '100', '200'])
        self.assertEqual(run.call_args_list[-1].args[0], ['ydotool', 'click', '0xC0'])


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
        self.assertEqual([state.name for state in first.states],
                         ['main_menu', 'shipments_screen'])
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

    def test_existing_schema_migrates_without_losing_rules(self):
        schema = (ROOT / 'agent_schema.sql').read_text()
        schema = '\n'.join(line for line in schema.splitlines()
                           if not line.strip().startswith('kind TEXT'))
        with sqlite3.connect(self.database) as db:
            db.executescript(schema)
            db.execute("INSERT INTO actions(name) VALUES ('custom')")
            db.execute("INSERT INTO app_metadata VALUES ('seeded', '1')")
        Store().initialize()
        Store().initialize()
        rules = Store().load_rules()
        self.assertEqual([a.name for a in rules.actions], ['custom'])
        self.assertEqual(rules.actions[0].kind, 'click')

    def test_action_template_mappings_exist(self):
        Store().initialize()
        settings = Settings.load()
        for action in Store().load_rules().actions:
            if action.kind == "observe":
                self.assertIsNone(action.template)
                continue
            self.assertTrue(settings.resolve_template(action.template).is_file(), action.name)


class ReplayTests(unittest.TestCase):
    def test_actual_shipments_label_variant_passes_with_configured_smoothing(self):
        settings = Settings.load()
        fixture = cv2.imread(str(ROOT / 'tests/fixtures/shipments_label_variant.png'))
        self.assertIsNotNone(fixture)
        raw = TemplateMatcher(settings.resolve_template)
        matcher = TemplateMatcher(settings.resolve_template, settings.template_preprocessing)
        self.assertLess(raw.match(fixture, 'shipments_button').score, .94)
        match = matcher.match(fixture, 'shipments_button')
        self.assertGreaterEqual(match.score, .94)
        self.assertEqual(match.point, (91, 31))
        self.assertLess(matcher.match(np.zeros_like(fixture), 'shipments_button').score, .94)
        # Preprocessing is scoped to the action reference, leaving states intact.
        template_path = settings.resolve_template('shipments_button')
        scoped = TemplateMatcher(lambda ref: template_path, {'shipments_button': 'gaussian3'})
        self.assertAlmostEqual(scoped.match(fixture, 'other').score,
                               raw.match(fixture, 'shipments_button').score)

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
        agent.settings = replace(agent.settings, agent=replace(agent.settings.agent, dry_run=True))
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

    def test_navigation_refreshes_and_clicks_before_model(self):
        agent = self.make_agent()
        agent.step()
        self.assertEqual(agent.capture.grab.call_count, 2)
        agent.capture.assert_click_target.assert_called_once()
        self.assertEqual(agent.executor.clicker.click.call_args.args, (220, 150))
        agent.step()
        self.assertEqual(agent.ollama.observe.call_count, 0)
        self.assertEqual(agent.executor.clicker.click.call_count, 1)

    def test_runner_wires_auto_focus_setting(self):
        agent = self.make_agent()
        agent.settings = replace(agent.settings, input=replace(agent.settings.input,
                                                                enabled=True, auto_focus=True))
        agent.step()
        agent.capture.focus_window.assert_called_once()
        other = self.make_agent()
        other.settings = replace(other.settings, input=replace(other.settings.input, auto_focus=False))
        other.step()
        other.capture.focus_window.assert_not_called()

    def test_slow_model_never_sends_ai_required_click(self):
        agent = self.make_agent()
        agent.executor.cfg = replace(agent.executor.cfg, require_ollama_for_actions=True)
        frame = agent.capture.grab.return_value
        def slow_observe(*args):
            agent.executor.cfg = replace(agent.executor.cfg, max_frame_age_seconds=.001)
            return {}
        agent.ollama.observe.side_effect = slow_observe
        agent.capture.grab.return_value = frame._replace(captured_at=time.monotonic() - 1)
        agent.step()
        agent.executor.clicker.click.assert_not_called()

    def test_focus_wait_skips_slow_model_and_recovers_next_step(self):
        agent = self.make_agent()
        agent.capture.assert_click_target.side_effect = ClickNotReady('inactive')
        agent.step()
        agent.ollama.observe.assert_not_called()
        self.assertFalse(agent.executor.halted)
        self.assertEqual(agent.executor.actions_done, 0)
        agent.capture.assert_click_target.side_effect = None
        agent.step()
        self.assertEqual(agent.executor.actions_done, 1)
        agent.ollama.observe.assert_not_called()

    def test_conditioned_rule_still_waits_for_model(self):
        agent = self.make_agent()
        rules = agent.store.load_rules.return_value
        action = replace(rules.actions[0], conditions=(
            {'field': 'credits', 'operator': '>=', 'value': 100},))
        agent.store.load_rules.return_value = replace(rules, actions=(action,))
        agent.ollama.observe.return_value = {'values': {'credits': 120}}
        agent.step()
        agent.ollama.observe.assert_called_once()
        agent.executor.clicker.click.assert_called_once()

    def test_failed_condition_never_clicks(self):
        agent = self.make_agent()
        rules = agent.store.load_rules.return_value
        action = replace(rules.actions[0], conditions=(
            {'field': 'credits', 'operator': '>=', 'value': 100},))
        agent.store.load_rules.return_value = replace(rules, actions=(action,))
        agent.ollama.observe.return_value = {'values': {'credits': 50}}
        agent.step()
        agent.executor.clicker.click.assert_not_called()

    def test_once_waits_for_postcondition_without_second_step(self):
        agent = self.make_agent()
        agent.validate_templates = Mock()
        action = agent.store.load_rules.return_value.actions[0]
        def single_click():
            agent.executor.pending = (action, 0, time.monotonic() + 30)
        agent.step = Mock(side_effect=single_click)
        with patch('swgoh_agent.runner.time.sleep'), patch('swgoh_agent.runner.detect_state',
                return_value=Detection('next', {'next': 1.0}, 1.0)):
            agent._run(once=True)
        agent.step.assert_called_once()
        self.assertIsNone(agent.executor.pending)
        agent.store.save_latest_state.assert_called_once()
        self.assertEqual(agent.store.save_latest_state.call_args.args[0]['action_result'], 'confirmed:go')

    def test_cli_respects_shared_workspace_lease(self):
        from swgoh_agent.runtime import AgentLease
        agent = self.make_agent()
        agent._run = Mock()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with AgentLease(root / 'logs' / 'agent.lock'), patch('swgoh_agent.runner.ROOT', root):
                with self.assertRaisesRegex(RuntimeError, 'already running'):
                    agent.run(once=True)
        agent._run.assert_not_called()

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
