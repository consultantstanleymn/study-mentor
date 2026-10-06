Study Mentor crab: a pixel crab that walks onto the screen at study time, says a line out loud and offers to start the app.
Run it: `./venv/bin/python widget/crab_widget.py` (autostart: copy `widget/study-mentor-crab.desktop` to `~/.config/autostart/` and replace `@INSTALL_DIR@` with the folder you cloned into).
Schedule: `~/.config/study-mentor/schedule.json` (edit by hand or in the Voice settings popover); fired reminders are remembered in `~/.local/state/study-mentor/fired.json`.
Test: `./venv/bin/python widget/crab_widget.py --test aws` (or `lsat`, `quant`); `--once` does a single check and exits.
KWin rule "crab" for window class `study-mentor-crab` (keep above, no taskbar, pager or switcher, no border) is stored in `~/.config/kwinrulesrc`.
