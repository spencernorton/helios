"""Launch the installed application and exercise native widgets outside the source tree.

Run under a private D-Bus session and Xvfb after installing the candidate .deb.
No provider command or model request is sent.
"""

import importlib.metadata
import importlib.resources
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    with tempfile.TemporaryDirectory(prefix="helios-installed-") as temporary:
        root = Path(temporary)
        for variable, suffix in (("HOME", "home"), ("HELIOS_STATE_DIR", "state"),
                                 ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                                 ("XDG_CACHE_HOME", "cache"), ("CLAUDE_HOME", "claude")):
            location = root / suffix
            location.mkdir()
            os.environ[variable] = str(location)

        executable = shutil.which("helios")
        assert executable and os.access(executable, os.X_OK), "The installed launcher is missing or not executable"
        help_result = subprocess.run([executable, "--help"], capture_output=True, text=True, timeout=10)
        assert help_result.returncode == 0 and help_result.stdout, help_result.stderr

        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gio, GLib
        import helios
        from helios.__main__ import main as launch
        from helios import log

        log.setup()
        log.install_excepthook()

        package = Path(helios.__file__).resolve()
        source = Path(__file__).resolve().parents[1]
        assert not package.is_relative_to(source), f"Loaded source instead of the installed package: {package}"
        assert helios.__version__ == importlib.metadata.version("helios")
        resources = importlib.resources.files("helios")
        assert resources.joinpath("resources/style/helios.css").read_text().strip(), "Installed CSS is missing"
        assert any(p.name.endswith(".svg") for p in resources.joinpath("resources/icons").iterdir()), "Installed icons are missing"
        failures = []
        completed = []
        phase = []
        browser = []
        fixture = root / "home" / "runtime.html"
        fixture.write_text("<title>Installed Runtime Fixture</title><h1>Native browser loaded</h1>")

        def fail(error):
            failures.append(str(error))
            app = Gio.Application.get_default()
            if app:
                app.quit()

        def exception(_kind, value, _traceback):
            fail(value)

        # GI callback exceptions normally print and let the event loop continue.
        # They must make this acceptance run fail even if a window remains open.
        previous_hook = sys.excepthook
        sys.excepthook = exception

        def inspect():
            try:
                app = Gio.Application.get_default()
                if app is None or app.props.active_window is None:
                    return GLib.SOURCE_CONTINUE
                window = app.props.active_window
                if not window.get_mapped():
                    return GLib.SOURCE_CONTINUE
                from helios.main_window import MainWindow
                assert isinstance(window, MainWindow), "The normal main window did not launch"
                if not phase:
                    window._composer.set_text("Synthetic installed-package draft")
                    assert window._composer.current_text() == "Synthetic installed-package draft"
                    assert window._browser.available, "The installed WebKit dependency did not load"
                    browser.append(window._browser.open(fixture.as_uri()))
                    phase.append("browser")
                    return GLib.SOURCE_CONTINUE
                if phase[-1] == "browser":
                    if browser[0].is_loading() or not browser[0].get_title():
                        return GLib.SOURCE_CONTINUE
                    assert browser[0].get_title() == "Installed Runtime Fixture", "The native browser did not load the synthetic document"
                    app.activate_action("about", None)
                    phase.append("about")
                    return GLib.SOURCE_CONTINUE
                if phase[-1] == "closing-about":
                    if window.get_visible_dialog() is not None:
                        return GLib.SOURCE_CONTINUE
                    completed.append(True)
                    app.activate_action("quit", None)
                    return GLib.SOURCE_REMOVE
                dialog = window.get_visible_dialog()
                assert isinstance(dialog, Adw.AboutDialog), "The About action did not open its native dialog"
                dialog.close()
                phase.append("closing-about")
                return GLib.SOURCE_CONTINUE
            except Exception as error:
                fail(error)
                return GLib.SOURCE_REMOVE

        def expired():
            watchdog.clear()
            fail("The installed application did not complete startup and widget checks within 20 seconds")
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(100, inspect)
        watchdog = [GLib.timeout_add_seconds(20, expired)]
        try:
            status = launch()
        finally:
            if watchdog:
                GLib.source_remove(watchdog.pop())
            sys.excepthook = previous_hook
        assert status == 0 and completed and not failures, {"status": status, "failures": failures}
        print(json.dumps({"package": str(package), "version": helios.__version__,
                          "checks": ["installed executable", "installed resources", "normal entrypoint", "mapped main window",
                                     "composer text", "native WebKit document load", "About dialog", "normal quit"]}, indent=2))


if __name__ == "__main__":
    main()
