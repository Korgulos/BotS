# SWGOH Agent for CachyOS (Steam + Proton)

Conservative starter project for 1920x1080 KDE Wayland. It does **not** ship game UI templates because those must be captured from your actual game screen. It starts in dry-run and will not click anything by default.

## 1. System packages

```bash
sudo pacman -Syu
sudo pacman -S --needed python python-pip opencv tesseract spectacle ydotool
```

`ydotool` uses `/dev/uinput` and needs the `ydotoold` daemon. Do not run the whole Python agent as root. First inspect the service available on your CachyOS installation:

```bash
systemctl status ydotool
systemctl status ydotoold
```

If neither unit exists, check package files and the package's current service name:

```bash
pacman -Ql ydotool | grep -E 'systemd|service|ydotoold'
ydotool --help
ydotool mousemove --help
ydotool click --help
```

Start the service using the unit name actually installed by your package. Do not run arbitrary commands as root from the agent.

## 2. Python environment

```fish
cd ~/Projects/swgoh-agent-cachyos
python -m venv .venv
source .venv/bin/activate.fish
pip install -r requirements.txt
```

## 3. Configure Steam launch

The project deliberately does not guess a Steam AppID.

- Open Steam Library and locate SWGOH.
- Open its Properties and determine the installed game's AppID/launch details.
- Put the verified AppID in `config.json` under `steam.appid`, or put a known launch command under `steam.launch_command`.
- Launch with:

```fish
python launch_game.py
```

If the game is already running, skip this step.

## 4. Test screenshot capture

Keep the game visible on the desktop at 1920x1080, then run:

```fish
python capture_test.py
```

This uses KDE Spectacle's background CLI mode. Confirm `last_screen.png` is the expected screen and size before building templates. If the screenshot captures the wrong monitor, arrange the game/monitor accordingly or adjust the capture backend.

## 5. Create templates

Templates are small crops from your own screenshots. Use a crop that is distinctive and stable, not a changing number or animated effect.

Example (replace coordinates with the actual position on your screenshot):

```fish
python crop_template.py main_menu 700 100 300 100
python crop_template.py battle_button 1400 900 220 90
python crop_template.py battle_screen 300 100 350 80
python crop_template.py victory 700 150 400 120
python crop_template.py defeat 700 150 400 120
```

Those coordinates are examples only; inspect `last_screen.png` and use the actual bounds. Templates are not included because guessing coordinates is unsafe. Capture templates for the exact UI and resolution you use.

## 6. Run observation-only mode

Install/start Ollama separately and pull the configured model:

```bash
ollama pull granite4:micro
sudo systemctl enable --now ollama
```

Then:

```fish
python agent.py
```

Initially `dry_run=true` and `input.enabled=false`. The agent observes, matches templates, logs state, and does not click.

## 7. Controlled input test

Before enabling any game actions, test ydotool on a harmless desktop area with a visible text editor. Use the CLI documented by your installed version. `ydotool` uses a virtual input device and may need a daemon and `/dev/uinput` permissions.

Only after the screenshot and template matching work:
1. Set `input.enabled=true`.
2. Keep `agent.dry_run=true` while validating detections.
3. Add a template and an explicit action to the SQLite database (`agent.sqlite3`).
4. Review the screenshot and logs after each test.
5. Set `dry_run=false` only when the target template, state condition and postcondition are validated.

Do not enable the example `start_pve_battle` action until you have captured the correct `battle_button` template and verified it points to the intended button. The starter action is disabled and requires manual review by design.

## 8. SQLite configuration

The agent creates `agent.sqlite3` on its first run using `agent_schema.sql` and the initial records in `agent_seed.sql`. State definitions, action rules, observation fields, and the Ollama prompt are stored in this database; Python retains the detection and action-execution logic. The database also stores the latest observed state as JSON in `latest_state`. Legacy state, action, and observation definitions are imported from an older `config.json` once when the database is first created, and an existing `logs/last_state.json` snapshot is imported once.

Inspect the database with:

```fish
sqlite3 agent.sqlite3
```

Useful queries:

```sql
SELECT name, template, threshold, enabled, sort_order
FROM state_definitions ORDER BY sort_order, name;
SELECT name, when_state_json, template, enabled, requires_manual_review FROM actions;
SELECT prompt_key, template FROM prompts;
SELECT payload_json, observed_at FROM latest_state;
```

For state recognition priority, set `state_definitions.sort_order` to an integer. Lower numbers are checked first; the first state whose template meets its threshold is selected. Use distinct numbers when you need a specific order.

To enable an action, edit its `enabled` and `requires_manual_review` columns only after verifying its template, state, input settings, and safety. Input remains disabled and dry-run remains enabled by default.

## 9. Logs and safety

- `logs/agent.jsonl`: observations, actions, errors.
- `agent.sqlite3` (`latest_state` table): most recent classification and observation.
- Unknown screen, missing template, low match score, or disabled input means no click.
- A generic screenshot classifier is not enough to safely automate purchases, premium currency, crystals, account settings, or irreversible actions. Keep those actions manual.
- The agent does not bypass game protections or guarantee that automation complies with game/platform rules.

## Action verification and regression tests

The current agent captures the named X11/XWayland game window using `wmctrl`,
`xwd` and ImageMagick (`magick`). Live input also requires `xprop` to verify
`_NET_ACTIVE_WINDOW`. The legacy `capture.backend` setting is not used by this
implementation. Use `python capture_test_x11.py` to test the same capture path
as the agent. Keep the game visible. With `input.auto_focus=true`, the agent activates it
only when a click rule has passed its initial checks.

Before each live click, the agent captures another frame, checks the detected
state and button again, adds the current window origin to the button coordinates,
and checks window identity, geometry and focus immediately before input. For actions requiring AI, both
frames must be younger than `agent.max_frame_age_seconds` (default 30 seconds),
including time spent waiting for the model and input rate limit. A slower model
response causes the action to be skipped. Adjust this limit deliberately for
stationary screens; increasing it permits older model observations.

Every executable click rule must define `postcondition_state`. After dispatch,
the agent sends no more clicks until cooldown finishes and the expected state
is observed. `agent.postcondition_timeout_seconds` (default 30 seconds) begins
at the end of cooldown. If confirmation fails or an input command raises an error,
actions halt until the agent is restarted after manual inspection. A failed
pre-click guard (inactive/moved window, expired frame, or unavailable focus
check) instead reports `click_deferred` and retries validation with a fresh
frame on the next iteration. It sends no mouse button command. Template-only
navigation also skips Qwen while waiting for focus, so switching back to the
game resumes validation without a restart. There is no
automatic click retry. A same-state postcondition confirms only screen presence,
not that an in-place resource update succeeded.

Startup reports missing or unreadable templates. Live mode refuses to start
until these are supplied or the unused rules are disabled/removed. The included
configuration maps the existing shipments and resources action images; the active state definitions are limited to `main_menu` and `shipments_screen`.
Dry-run and input defaults remain unchanged.

Run the offline tests (no game, model server, or real input required):

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Tests use temporary SQLite databases, mocked input/window APIs, and a saved
synthetic screenshot replay. They cover coordinate conversion, stale frames,
changed targets/screens, pending action confirmation and timeout, input failures,
first-run seed rollback/retry, template mappings and runner integration.

## Shipments trial actions

Action rules now have a `kind` column: `click` or `observe`. Existing databases
are migrated on startup, with custom rules retaining `click` behavior. New
installations still seed rules disabled for review.

In the configured local trial, `go_to_shipments` clicks only from `main_menu`
and requires `shipments_screen` as its postcondition. `read_resources` is an
`observe` rule for both screens: it writes `resource_observation` events with
Qwen's values and never sends input or consumes the click budget. The requested
fields are credits, crystals, normal energy, cantina energy, fleet energy and
mod energy; unreadable balances must be null. These are model readings, not
verified account balances.

Template-only navigation runs before Qwen inference when no eligible click rule
has AI conditions and `require_ollama_for_actions` is false. Successful navigation
skips model reading for that old screen; resource observation resumes after the
postcondition is confirmed. AI-dependent rules still wait for the model and
reject stale observations. Increasing the frame age limit is unnecessary for
`go_to_shipments`.

`template_preprocessing` optionally applies preprocessing per template reference.
The `shipments_button` rule uses `gaussian3` (a 3x3 Gaussian blur on both images)
to tolerate small text rasterization differences while retaining its 0.94 match
threshold. Other references retain the original color matching. A saved label
variant in `tests/fixtures` covers this case. Rejected matches now log
`action_rejected` with the measured score and required threshold, and include
those values in the action status.

For the current X11/XWayland capture backend, `input.mousemove_backend` defaults
to `xdotool` and requires that executable. The installed `ydotool --absolute`
movement was observed to miss the target on this multi-monitor Wayland session.
The agent now moves with `xdotool mousemove --sync`, reads the pointer using
`xdotool getmouselocation --shell`, and sends the ydotool button command only
when the pointer is within 3 desktop pixels of the target and the window/frame
guards still pass. A pointer mismatch or unreadable position defers the click;
it does not consume the action budget. The game must remain active for XWayland
pointer positioning. `input.mousemove_backend=ydotool` remains available, with
the same mandatory pointer verification.

Automatic focus is enabled in the local configuration with `input.auto_focus=true`.
After validating a click rule and its initial target, the agent activates the
captured window by its exact ID using `wmctrl -i -a`, waits for focus, then
captures and validates a fresh frame before moving and clicking. Dry-run,
observation-only rules, rejected targets and pending postconditions do not
activate the game. A failed activation defers the action. Set `auto_focus=false`
to require manual focus again.

## Local dashboard

Launch from the project's virtual environment:

```bash
.venv/bin/python ui.py
```

Open **http://127.0.0.1:8765**. To choose another local port, use
`python ui.py --port 8766`. No additional Python or frontend dependencies are
needed. The server binds only to loopback. Close terminal agents started with
older versions before using the dashboard; new CLI and UI agents share a
workspace lock at `logs/agent.lock`.

The dashboard provides:

- **Stanja:** create, edit and delete states; choose a template, threshold,
  priority and enabled flag.
- **Akcije:** edit click/observe rules, initial states, postconditions, cooldown,
  confidence threshold and all-of conditions. Conditions use the same fields and
  operators as the agent, for example `values.crystals >= 100`.
- **Tok kretanja:** read-only visualization derived from initial states → action
  → postcondition. Edit connections through action forms; this is not a separate
  flow execution engine or a drag-and-drop editor.
- **Očitavanja / Qwen promptovi:** manage model fields and prompt text.
- **Podešavanja:** validated JSON editor for the existing configuration file.
- **Ekran igre:** capture the window, or load the previous screenshot. Drag to
  select a crop, name it and save a new template. Existing image files are never
  overwritten. Templates stay inside `templates/`.
- **Testiraj na snimku:** test a saved action against the selected screenshot,
  show the target and failed checks; no input and no model call are sent. AI
  conditions are explicitly marked unevaluated. Live focus, frame age and cursor
  checks still run separately during execution.
- **Agent controls:** choose observation (default, no input) or live mode, start,
  stop, or execute one step. A single live step still waits for its postcondition
  and sends no second action. UI mode overrides apply only to that process;
  `config.json` remains unchanged. Stopping interrupts only the process owned by
  this dashboard; terminal agents must be stopped in their own terminal.
- **Pregled:** latest state, action status, recent events, process output and
  current screenshot while the agent runs.

Edits and manual capture/preview tests are locked while an agent is running.
Changes use transactions and revision checks. Referenced states cannot be
removed, and the required observation prompt cannot be deleted. The UI never
starts an agent on page load. Stop the server with Ctrl+C in its terminal;
this also requests shutdown of its managed agent.

The standalone CLI additionally supports:

```bash
.venv/bin/python agent.py --once --dry-run
.venv/bin/python agent.py --once --live
```

## Additional input action types

The editor and executor support six action kinds:

| UI type | Stored kind | Parameters |
| --- | --- | --- |
| Klik na šablon | `click` | Existing template reference and match threshold |
| Klik na tekst (OCR) | `click_text` | Exact text, OCR confidence and optional `[x,y,width,height]` search region |
| Pritisni taster | `key` | Key or combination such as `Escape`, `Return`, `g`, `ctrl+a`; hold duration |
| Unesi tekst | `type_text` | Literal single-line text and delay between characters |
| Prevuci mišem | `drag` | Start/end coordinates within the game screenshot and duration |
| Očitavanje | `observe` | Existing model observation behavior, without input |

Action-specific parameters are stored in `actions.parameters_json`. Startup
migrates the old SQLite action-kind constraint transactionally, retaining
existing rules and their settings. New rules remain disabled pending review.
All input kinds use the existing state/condition checks, fresh captures,
automatic focus setting, action budget, cooldown and postcondition verification.
The source and destination coordinates of a drag are translated using the
current window origin; the editor can calculate left/right/up/down endpoints,
and explicit endpoints also allow diagonal drags. Preview shows the path and
never holds or presses a button.

Text clicks require the `tesseract` executable and English trained data (already
present in this environment). OCR matches whole words on one line without case
sensitivity. Ambiguous or low-confidence matches do not click: select a smaller
search region using the screenshot selection button. OCR is rerun on the fresh
pre-input capture; no model-generated coordinates are used.

Key/text input uses xdotool in the active X11/XWayland game. A text-entry action
writes into the already focused input field; target that field separately with
a click action. Newlines/control characters are rejected; use a key action for
Return or Tab. Text is passed literally on stdin in small batches, with focus
checks between batches. Keyboard modifiers are limited to ctrl/alt/shift.
Key releases and mouse-button releases are attempted in `finally` blocks even
if dispatch fails. If a drag or text entry is interrupted after it began, the
agent halts rather than repeating a potentially partial action. Same-state
postconditions verify only the detected screen, not text-field content or the
amount of scrolling.

### Multiple postconditions

Input actions may select several **Očekivana završna stanja** in the editor.
After cooldown, recognizing any one of those states confirms the action (OR,
not AND). Unknown or unselected states keep the action pending until timeout.
The flow view lists all alternatives. State deletion and disabling checks cover
every selected postcondition, not only the first.

The canonical list is stored as JSON in `actions.postcondition_states_json`.
Startup migrates legacy single-state rules without changing their meaning;
`postcondition_state` remains a compatibility field containing the first name.
The API accepts `postcondition_states: ["quests_state", "inbox_state"]` and
still accepts legacy records containing only `postcondition_state`. Active
input rules require a nonempty list of existing, enabled states. Observation
rules have no postconditions. Multiple acceptable screens do not associate a
specific destination with each initial screen; create separate actions if that
stricter behavior is needed.
