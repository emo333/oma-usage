"""Native, consent-first entry point for the Omarchy-managed plugin checkout."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import threading


def main() -> int:
    project = Path(__file__).resolve().parent
    # Loading the checkout installer must not create __pycache__ before consent
    # (or modify the Omarchy-managed checkout after approval).
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location('oma_usage_installer', project / 'oma_usage/installer.py')
    if spec is None or spec.loader is None:
        raise RuntimeError('The installed Omarchy plugin is incomplete. Update or reinstall it with Omarchy.')
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    def launch_dashboard():
        subprocess.Popen([str(installer.locations()['venv'] / 'bin/python'),
                          '-I', '-m', 'oma_usage'], cwd='/')

    try:
        if installer.runtime_current(project):
            launch_dashboard()
            return 0
    except (OSError, installer.InstallError):
        # The consent window reports ownership and startup errors explicitly.
        pass

    try:
        installer._project(project)
        import gi
        gi.require_version('Gtk', '4.0')
        gi.require_version('Adw', '1')
        # GI namespaces are generated from native typelibs, not Python source.
        from gi.repository import Adw, GLib, Gtk  # pyright: ignore[reportMissingModuleSource]
    except (ImportError, ValueError, OSError, installer.InstallError) as exc:
        message = ('Cannot open Oma Usage setup: ' + str(exc) + '\nNative prerequisites: '
                   + installer.NATIVE_PREREQUISITE)
        print(message, file=sys.stderr)
        try:
            subprocess.run(['notify-send', '--app-name=Oma Usage', '--urgency=critical',
                            'Oma Usage setup unavailable', message], check=False, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return 1

    class Bootstrap(Adw.Application):
        def __init__(self):
            super().__init__(application_id='org.omarchy.OmaUsage.RuntimeSetup')
            self.window = None
            self.busy = False

        def do_activate(self):
            if self.window is not None:
                self.window.present()
                return
            self.window = Adw.ApplicationWindow(application=self, title='Oma Usage — local backend setup')
            self.window.set_default_size(620, 560)
            self.window.connect('close-request', lambda *_: self.busy)
            layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                             margin_top=24, margin_bottom=24, margin_start=24, margin_end=24)
            self.status = Gtk.Label(label='Checking the installed local backend…', wrap=True, xalign=0)
            layout.append(self.status)
            scroll = Gtk.ScrolledWindow(vexpand=True)
            target = installer.locations()
            paths = [target[key] for key in ('venv', 'manifest', *installer.ASSETS)]
            details = ('With your approval, build and install the local Python backend and Rust GPUI dashboard. '
                       'pip downloads build tools and Python dependencies; Cargo downloads locked Rust dependencies. '
                       'The first build can take tens of minutes and requires substantial temporary disk space. '
                       'Install any missing native prerequisites before approving:\n'
                       + installer.NATIVE_PREREQUISITE + '\n'
                       'A working Vulkan driver is required to display the dashboard. Install missing packages '
                       'yourself before approving; setup does not install system packages. '
                       'An isolated temporary build directory under /var/tmp is removed after setup; '
                       'the Omarchy plugin checkout is never changed. Allow several GiB of free space there.\n\n'
                       'Files created or replaced:\n' + '\n'.join(str(path) for path in paths)
                       + '\nSetup lock: ' + str(target['config'] / 'setup.lock')
                       + '\n\nClose any existing Oma Usage dashboard before approving an update. '
                       'An owned collector that is already running will be stopped and restarted during an update; '
                       'its tracking and pause settings are preserved. Failed updates may leave it stopped. '
                       'A stopped collector is never started by this setup.\n\n'
                       'This does not enable tracking, read history, or change Bash startup. '
                       'The dashboard requests separate tracking consent and never installs a hook into your startup files. '
                       'Cancel before approval creates no files and contacts no network service.')
            scroll.set_child(Gtk.Label(label=details, wrap=True, selectable=True, xalign=0, yalign=0))
            layout.append(scroll)
            buttons = Gtk.Box(spacing=12, halign=Gtk.Align.END)
            self.cancel = Gtk.Button(label='Cancel')
            self.cancel.connect('clicked', lambda *_: self.quit())
            self.approve = Gtk.Button(label='Install local backend')
            self.approve.add_css_class('suggested-action')
            self.approve.set_sensitive(False)
            self.approve.connect('clicked', self.install)
            buttons.append(self.cancel)
            buttons.append(self.approve)
            layout.append(buttons)
            self.window.set_content(layout)
            self.window.present()
            self.work(lambda: installer.runtime_current(project), self.checked)

        def work(self, operation, callback):
            self.busy = True
            self.cancel.set_sensitive(False)
            self.approve.set_sensitive(False)

            def worker():
                try:
                    result, error = operation(), None
                except Exception as exc:
                    result, error = None, str(exc)
                GLib.idle_add(callback, result, error)

            threading.Thread(target=worker, daemon=True).start()

        def checked(self, current, error):
            self.busy = False
            self.cancel.set_sensitive(True)
            if error:
                self.status.set_text('Cannot safely use this backend: ' + error
                                     + '\nResolve the ownership or plugin problem, then reopen Usage. No files were changed.')
            elif current:
                self.launch()
            else:
                self.status.set_text('The local backend is missing or differs from this plugin update. '
                                     'Review the file and network changes below before approving.')
                self.approve.set_sensitive(True)
            return False

        def install(self, *_):
            if self.busy:
                return
            self.status.set_text('Installing the approved local backend… This can take several minutes.')
            self.work(lambda: installer.install(project), self.installed)

        def installed(self, result, error):
            self.busy = False
            self.cancel.set_sensitive(True)
            if error:
                self.status.set_text('Setup did not complete: ' + error
                                     + '\nOwned partial files may remain; tracking was not enabled. '
                                     'If an update stopped the collector, it may remain stopped. '
                                     'Resolve the reported problem and retry. Native prerequisites: '
                                     + installer.NATIVE_PREREQUISITE)
                self.approve.set_label('Retry approved setup')
                self.approve.set_sensitive(True)
            else:
                self.launch()
            return False

        def launch(self):
            try:
                launch_dashboard()
            except OSError as exc:
                self.status.set_text('Cannot launch the installed dashboard: ' + str(exc))
                self.cancel.set_sensitive(True)
            else:
                self.quit()

    return Bootstrap().run([sys.argv[0]])


if __name__ == '__main__':
    raise SystemExit(main())
