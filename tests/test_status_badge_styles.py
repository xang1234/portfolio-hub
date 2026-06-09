"""The status-badge CSS must declare a rule for every connection state.

The badge template uses three modifier classes — `--connected`, `--reconnecting`,
`--disconnected` — and the stylesheet must back each of them, otherwise the
RECONNECTING state renders unstyled (background falls back to default text
styling, which looks visually broken next to the green/red badges).
"""

import re
from pathlib import Path


CSS_PATH = Path(__file__).parent.parent / "app" / "static" / "app.css"


def test_status_badge_has_connected_modifier_class():
    css = CSS_PATH.read_text()
    assert ".status-badge--connected" in css


def test_status_badge_has_disconnected_modifier_class():
    css = CSS_PATH.read_text()
    assert ".status-badge--disconnected" in css


def test_status_badge_has_reconnecting_modifier_class():
    css = CSS_PATH.read_text()
    assert ".status-badge--reconnecting" in css


def test_status_badge_has_compact_action_classes():
    css = CSS_PATH.read_text()
    assert re.search(r"(?m)^\.status-actions\s*\{", css)
    assert re.search(r"(?m)^\.status-action\s*\{", css)
    assert re.search(r"(?m)^\.status-action--danger\s*\{", css)
