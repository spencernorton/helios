"""Every driver Helios spawns defines the signals the window wires for all.

v0.69.0 added "capabilities-updated" and "context-usage" to the *common* half
of `connect_handlers()`, but only ClaudeCliDriver defines them. GObject raises
`TypeError: unknown signal name` on connect, so the exception landed inside
`_on_composer_send` -> `_ensure_driver` -> `DriverManager.start_new`: every GPT
and OpenRouter send died at spawn with nothing at all shown in the UI.

Nothing caught it because the common list was a bare literal inside a closure.
It is now `COMMON_DRIVER_SIGNALS`, and this test is what makes adding a
provider-specific signal to it fail loudly here instead of silently in
production.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")

from gi.repository import GObject  # noqa: E402

from helios.backend.process.cli_driver import ClaudeCliDriver  # noqa: E402
from helios.backend.process.codex_app_driver import (  # noqa: E402
    CodexAppServerDriver,
)
from helios.backend.process.codex_driver import CodexCliDriver  # noqa: E402
from helios.backend.process.driver_manager import (  # noqa: E402
    COMMON_DRIVER_SIGNALS,
)
from helios.backend.process.gemini_driver import GeminiCliDriver
from helios.backend.process.muse_driver import MuseServeDriver  # noqa: E402
from helios.backend.process.openrouter_driver import (  # noqa: E402
    OpenRouterDriver,
)

DRIVERS = (
    ClaudeCliDriver,
    CodexCliDriver,
    CodexAppServerDriver,
    OpenRouterDriver,
    GeminiCliDriver,
    MuseServeDriver,
)


@pytest.mark.parametrize("driver_cls", DRIVERS, ids=lambda c: c.__name__)
def test_every_driver_defines_the_common_signal_contract(driver_cls) -> None:
    missing = [
        name
        for name in COMMON_DRIVER_SIGNALS
        if not GObject.signal_lookup(name, driver_cls)
    ]
    assert not missing, (
        f"{driver_cls.__name__} does not define {missing}. Either the signal "
        "is provider-specific — wire it in an isinstance branch of "
        "connect_handlers() — or every driver must emit it."
    )


@pytest.mark.parametrize("provider", ["claude", "gpt", "gemini", "openrouter", "muse", "kimi"])
def test_real_window_wiring_uses_only_supported_provider_signals(tmp_path, provider):
    # Exercise the actual closure without constructing a desktop window or
    # spawning a provider. A provider-specific signal in a shared branch used
    # to abort Kimi session creation after the Muse changes were rebased.
    from helios.backend.process.kimi_driver import KimiAcpDriver

    path = Path(__file__).parents[1] / "src/helios/main_window.py"
    tree = ast.parse(path.read_text())
    wiring = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name == "connect_handlers")
    class WindowCallbacks:
        def __getattr__(self, name):
            return lambda *args: None
    namespace = {cls.__name__: cls for cls in (*DRIVERS, KimiAcpDriver)}
    namespace.update(self=WindowCallbacks(), COMMON_DRIVER_SIGNALS=COMMON_DRIVER_SIGNALS)
    exec(compile(ast.Module(body=[wiring], type_ignores=[]), str(path), "exec"), namespace)
    cls = {"claude": ClaudeCliDriver, "gpt": CodexAppServerDriver,
           "gemini": GeminiCliDriver, "openrouter": OpenRouterDriver,
           "muse": MuseServeDriver, "kimi": KimiAcpDriver}[provider]
    driver = cls(cwd=str(tmp_path), model="gpt-test" if provider == "gpt" else "")
    handlers = namespace["connect_handlers"](driver)
    assert len(handlers) > len(COMMON_DRIVER_SIGNALS)
    for handler in handlers:
        driver.disconnect(handler)
