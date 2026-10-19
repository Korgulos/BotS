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
