---
name: ha-entity-reviewer
description: Reviews changes to HA entity platform files for accidental entity ID or name regressions that would break existing installations
---

Check the diff for changes to any of: sensor.py, number.py, select.py, switch.py, time.py, const.py.

Flag any change to `unique_id`, entity key names, translation keys, or anything in `const.py` that entity IDs derive from — these are breaking changes for existing Home Assistant installations and must be called out explicitly.

Report findings concisely: list each flagged change with the file, the old value, the new value, and why it's breaking.
