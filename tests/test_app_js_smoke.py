"""Slice 8 review-fix: app.js evaluation smoke test.

Catches TDZ / hoisting bugs in the JS IIFE that grep-based tests miss.
Runs app.js in Node with a minimal browser-API stub and asserts the
top-level IIFE completes without throwing — which is the contract every
page-load relies on.

Skips gracefully if `node` isn't installed (CI environments without it
just lose this coverage; not all dev machines need it).
"""

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


JS_FILE = Path("app/static/app.js")


def _run_iife() -> tuple[int, str, str]:
    """Execute app.js in Node with a stubbed browser environment.

    Returns (returncode, stdout, stderr). Uses `eval` against a leading
    stub block so we don't need a real DOM library — every API the IIFE
    touches is replaced by a no-op that returns the right shape.
    """
    stub = textwrap.dedent("""
        const fs = require('fs');
        global.document = {
            readyState: 'complete',
            querySelector: () => null,
            querySelectorAll: () => [],
            addEventListener: () => {},
            removeEventListener: () => {},
            body: { addEventListener: () => {} },
        };
        global.window = {
            matchMedia: () => ({ matches: false }),
            location: { reload: () => {} },
            scrollY: 0,
        };
        global.localStorage = { getItem: () => null, setItem: () => {} };
        global.setInterval = () => 0;
        global.setTimeout = () => 0;
        global.clearTimeout = () => {};
        global.CustomEvent = class { constructor(name, init) { this.type = name; this.detail = (init || {}).detail; } };
        const src = fs.readFileSync(process.argv[1], 'utf8');
        try {
            eval(src);
            console.log('OK');
        } catch (e) {
            console.error('IIFE_ERROR:', e.message);
            process.exit(1);
        }
    """)
    return subprocess.run(
        ["node", "-e", stub, "--", str(JS_FILE)],
        capture_output=True, text=True, timeout=10,
    )


def test_app_js_iife_does_not_throw_on_load():
    """The whole IIFE must complete on script load.

    A crash short-circuits every listener registered after the throw,
    which silently breaks the countdown, drawer init, sort wiring, and
    HTMX afterSwap rehydration. grep-based tests miss this because the
    JS file *contains* the relevant strings — it just can't run them.
    """
    if shutil.which("node") is None:
        pytest.skip("node not installed; skipping JS smoke test")
    result = _run_iife()
    if result.returncode != 0:
        pytest.fail(
            f"app.js IIFE threw on load:\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
    assert "OK" in result.stdout
