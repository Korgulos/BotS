"""Shared action parameters for the editor, preview and executor."""
from __future__ import annotations

import math
import json
import re

INPUT_KINDS = ('click', 'click_text', 'key', 'type_text', 'drag')
ACTION_KINDS = (*INPUT_KINDS, 'observe')
SENT_PREFIXES = ('clicked:', 'key_sent:', 'text_entered:', 'dragged:')


def integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{name} must be an integer from {low} to {high}')
    return value


def parameters(kind, raw):
    if kind not in ACTION_KINDS:
        raise ValueError('Unknown action kind')
    if not isinstance(raw, dict):
        raise ValueError('Action parameters must be an object')
    if kind in ('click', 'observe'):
        return {}
    if kind == 'key':
        keys = raw.get('keys', '')
        if not isinstance(keys, str) or len(keys) > 100:
            raise ValueError('Specify a key such as Return, Escape, g or ctrl+a')
        aliases = {'enter': 'Return', 'esc': 'Escape', 'space': 'space',
                   'backspace': 'BackSpace', 'delete': 'Delete', 'tab': 'Tab',
                   'up': 'Up', 'down': 'Down', 'left': 'Left', 'right': 'Right',
                   'home': 'Home', 'end': 'End', 'pageup': 'Prior', 'pagedown': 'Next'}
        parts = keys.split('+')
        if len(parts) > 4 or any(x.lower() not in ('ctrl', 'alt', 'shift') for x in parts[:-1]):
            raise ValueError('Modifiers may be ctrl, alt or shift')
        key = aliases.get(parts[-1].lower(), parts[-1])
        if not re.fullmatch(r'[a-zA-Z0-9]|F(?:[1-9]|1[0-9]|2[0-4])|Return|Escape|BackSpace|Delete|Tab|space|Up|Down|Left|Right|Home|End|Prior|Next', key):
            raise ValueError('Unsupported key name')
        return {'keys': '+'.join([x.lower() for x in parts[:-1]] + [key]),
                'hold_ms': integer(raw.get('hold_ms', 80), 'hold_ms', 10, 2000)}
    if kind == 'type_text':
        text = raw.get('text')
        if not isinstance(text, str) or not text or len(text) > 2000 or any(ord(c) < 32 for c in text):
            raise ValueError('Text must contain 1–2000 printable characters on one line')
        return {'text': text, 'delay_ms': integer(raw.get('delay_ms', 30), 'delay_ms', 0, 500)}
    if kind == 'drag':
        result = {name: integer(raw.get(name), name, 0, 32767)
                  for name in ('start_x', 'start_y', 'end_x', 'end_y')}
        if (result['start_x'], result['start_y']) == (result['end_x'], result['end_y']):
            raise ValueError('Drag start and end must differ')
        result['duration_ms'] = integer(raw.get('duration_ms', 800), 'duration_ms', 100, 5000)
        return result
    text = raw.get('text')
    if not isinstance(text, str) or not text.strip() or len(text) > 200:
        raise ValueError('Specify 1–200 characters of target text')
    score = raw.get('min_text_confidence', .85)
    if type(score) not in (float, int) or not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError('OCR confidence must be between 0 and 1')
    region = raw.get('region')
    if region is not None:
        if not isinstance(region, list) or len(region) != 4:
            raise ValueError('OCR region must be [x, y, width, height]')
        for i, value in enumerate(region):
            integer(value, 'OCR region', 0 if i < 2 else 1, 32767)
    return {'text': text.strip(), 'min_text_confidence': score, 'region': region}


def within_frame(kind, params, width, height, margin=20):
    if kind == 'drag':
        for prefix in ('start', 'end'):
            if not (margin <= params[prefix+'_x'] < width-margin
                    and margin <= params[prefix+'_y'] < height-margin):
                raise ValueError('Drag endpoints must be inside the screenshot, away from the edges')
    if kind == 'click_text' and params['region']:
        x, y, w, h = params['region']
        if x+w > width or y+h > height:
            raise ValueError('OCR region is outside the screenshot')


def postconditions(record):
    """Read new lists or legacy single-state records without changing meaning."""
    if 'postcondition_states' in record:
        states = record['postcondition_states']
    elif record.get('postcondition_states_json') is not None:
        states = json.loads(record['postcondition_states_json'])
    else:
        state = record.get('postcondition_state')
        states = [state] if state else []
    if not isinstance(states, (list, tuple)) or not all(isinstance(s, str) and s.strip() for s in states):
        raise ValueError('Postconditions must be a list of state names')
    return list(dict.fromkeys(states))
