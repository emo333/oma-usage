# Development and release verification

## Source is not runtime

This repository is the source of truth for development. Do not install a runtime from your working tree to check whether distribution works. The supported end-user entry point is Omarchy's Git-managed plugin installation, described in the [user guide](../README.md#install-through-omarchy).

Omarchy owns the installed Git checkout. Backend setup installs a non-editable Python wheel and a compiled GPUI executable outside that checkout. Neither runtime may depend on the development tree, `PYTHONPATH`, or Cargo's development output. See the [tested Omarchy release](../README.md#omarchy-baseline) and [native prerequisites](../README.md#install-through-omarchy) before checking a new platform.

## Components

| Component | Responsibility |
| --- | --- |
| [`manifest.json`](../manifest.json), [`BarWidget.qml`](../BarWidget.qml) | Declare the mixed service/widget plugin. The bar button invokes the adjacent `bootstrap.py` with the system Python in isolated mode. |
| [`bootstrap.py`](../bootstrap.py), [`installer.py`](../oma_usage/installer.py) | Launch a current runtime directly, otherwise show GTK/Adwaita consent. After approval, build the wheel and locked Rust executable in a disposable `/var/tmp` directory and record installed file hashes. |
| [`cli.py`](../oma_usage/cli.py) | The installed launcher owns the `org.omarchy.OmaUsage` session-bus name, checks the executable hash and starts one GPUI child. Later launches activate that window. The collector runs as a separate process. |
| [`dashboard/src/main.rs`](../dashboard/src/main.rs), [`render.rs`](../dashboard/src/render.rs) | Manage navigation, per-page filters, pending requests and confirmation screens. Render all seven dashboard pages and inline SVG help. |
| [`input.rs`](../dashboard/src/input.rs), [`theme.rs`](../dashboard/src/theme.rs) | Handle native text input and selection. Read Omarchy's `colors.toml` from its current theme in the XDG state directory, with per-color fallbacks. |
| [`lib.rs`](../dashboard/src/lib.rs), [`dashboard_bridge.py`](../oma_usage/dashboard_bridge.py) | Exchange one JSON request and reply per installed Python child (`-I -m oma_usage.dashboard_bridge`, working directory `/`). Search terms and selected history paths travel on stdin, not in process arguments. |
| [`dashboard_reports.py`](../oma_usage/dashboard_reports.py), [`dashboard_actions.py`](../oma_usage/dashboard_actions.py) | Build read-only report pages and details. Handle settings and consent separately. Setup and removal confirmations recheck the preview before making changes. |
| [`Service.qml`](../Service.qml), [`daemon.py`](../oma_usage/daemon.py), [`services.py`](../oma_usage/services.py) | The Quickshell observer sends foreground app IDs and idle state. The collector checks lock state, monitors user/system services, owns the database writer, and serves same-UID socket requests. |
| [`store.py`](../oma_usage/store.py), [`accounting.py`](../oma_usage/accounting.py) | Validate configuration and schema, retain daily aggregates, and credit only valid app/service observation intervals. |
| [`inventory.py`](../oma_usage/inventory.py), [`insights.py`](../oma_usage/insights.py), [`recommendations.py`](../oma_usage/recommendations.py) | Discover packages and entry points, attribute evidence, decide review eligibility, and serve the offline alternatives catalog. |
| [`setup.py`](../oma_usage/setup.py), [`uninstall.py`](../oma_usage/uninstall.py), [`integrations/`](../integrations/) | Manage consented tracking stages, user unit, desktop entry, and opt-in Bash wrapper. Removal preserves usage and does not delete Omarchy's checkout. |

The bar button checks a **payload fingerprint**, not just the manifest version. It covers the plugin manifest, QML, bootstrap, `pyproject.toml`, Python package, integrations, and dashboard sources, assets and lockfile. Top-level documentation and `dashboard/target/` are excluded. Omarchy has no plugin lifecycle script hooks: `plugin update` fetches source, the next bar activation offers any required rebuild, and the installed desktop launcher opens the existing runtime without checking the checkout.

## Development environment

A development venv is for local code and regression checks only:

```sh
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pyright --pythonpath .venv/bin/python
cargo build --locked --manifest-path dashboard/Cargo.toml
```

GTK renders only the runtime consent window. Gio/GLib still power activation and the collector. The GPUI window requires a Vulkan driver as well as the loader. Use `dashboard/Cargo.lock` for Rust resolution and `pyproject.toml` for Python parser pins. Keep Cargo output, virtual environments, wheel metadata, local databases and credentials out of the distribution fixture.

The `dev` extra pins Pyright and the upstream PyGObject stubs (GTK4 and libadwaita); neither is a runtime dependency. Pass `--pythonpath` so Pyright reads the interpreter's real search paths, including native GI packages inherited through `--system-site-packages`. Directory-only `venvPath`/`venv` configuration misses those system packages. Editors should select the same `.venv/bin/python` interpreter. Keep standard checking enabled; missing imports and nullable values are not suppressed.

GI repository namespaces are generated from native typelibs, so they have no Python source modules. The import-line `reportMissingModuleSource` exemptions cover only that fact; upstream stubs still check all GI calls and values. Unnecessary exemptions are errors. Native namespace availability must also pass setup preflight and the GUI/collector smoke checks.

To inspect the report contract without a window or a running collector, run this from the repository root after preparing `.venv`:

```sh
(
  set -eu
  fixture=$(mktemp -d)
  trap 'rmdir "$fixture"' EXIT
  export HOME="$fixture" XDG_CONFIG_HOME="$fixture/config" XDG_DATA_HOME="$fixture/data"
  export XDG_STATE_HOME="$fixture/state" XDG_RUNTIME_DIR="$fixture"
  printf '%s\n' '{"op":"page","page":"overview","days":7}' |
    .venv/bin/python -I -m oma_usage.dashboard_bridge
)
```

Expect a JSON view titled `Overview` with a `Getting started` section and a collector warning. This read-only request should leave the fixture empty. It checks the bridge and report model, not GPUI rendering, D-Bus activation, or installation.

For collector, import, tracking, or removal checks, isolate `HOME`, XDG paths, and the session bus. Isolate or substitute the systemd and Omarchy shell boundaries too. A temporary config directory alone does not redirect the user manager, protect the real history file, or prevent a live bar change. Use synthetic history and never run these operations against your desktop as part of a repository check.

## Verification boundaries

Use a disposable installation fixture, synthetic history, and a separate installed Git checkout. Keep the development repository off the runtime import path. At minimum, exercise these behaviors:

| Scenario | Required observable result |
| --- | --- |
| Native add/enable | Omarchy validates the root manifest and discovers/enables the mixed service/widget plugin. No custom installer is invoked by cloning. |
| Plugin scan readiness | An explicit transient `not ready` response can complete setup once discovery finishes. An unfinished scan has a bounded wait; permanent errors and ambiguous IPC failures are not replayed. Failures leave collection paused. |
| Fresh widget activation | Runtime consent opens despite no `~/.local/bin/oma-usage`. |
| Decline runtime setup | No runtime, service, startup block, history read, or dependency download. |
| Approve runtime setup | The external venv contains the installed Python package and `bin/oma-usage-dashboard`. The executable's SHA-256 matches `dashboard_digest` in `installed-files.json`. The installed window opens, tracking stays off and the source checkout is unchanged. |
| Repeat activation | The existing setup/dashboard is reused rather than producing duplicate windows. |
| Current bar sections | The installed release's native registry preserves the widget's existing section and per-widget settings when setup requests the default placement. An absent widget gets the default. |
| Native collector socket | Installed-wheel GLib watches accept peers, read/write responses, handle disconnects, and shut down without closing another socket or leaking the owned endpoint. The real Quickshell observer receives settings while collection is off. |
| Native report lifecycle | On the rendered dashboard, expand **Info**, search for a synthetic app, open its details and change the period. Resize to a narrow window and navigate with **Pages**. Values and confirmation text must remain readable. Automatic refresh must not discard pending settings, dismiss dialogs or retain stale rows after navigation. |
| Tracking preview/cancel | Exact managed paths and history source are shown; cancel starts no collectors and changes no shell startup configuration. |
| Approve tracking | Actual stage outcomes are reported, selected synthetic history imports once, and app/service collection resumes. A retry after a legacy Bash startup failure succeeds without touching the startup file or following its symlink to write. |
| Focus and source health | In an approved graphical-session test, focus a browser, then a terminal running a TUI. Expect browser and terminal app IDs, not website or TUI titles. Check **Overview → Source health and coverage** against the samples. A lock-state failure must stop app credit without presenting old service totals as live status. |
| Tracked terminal | The Commands action opens the default terminal with a one-session Bash wrapper. Real Bash loads normal settings, submits name-only prompt observations while enabled, and leaves ordinary terminals untracked, even with an old source block. |
| Native update | A Rust source, SVG or lockfile change prompts for a runtime update. A docs-only change does not. An open dashboard blocks replacement. A successful update preserves settings and pause state, restarts a previously running owned collector and leaves a stopped one stopped. |
| Removal preview/cancel | Open **Settings → Destructive actions**. Inspect the clear-history and removal previews separately, then cancel. Evidence, service state and the installed runtime must be unchanged. |
| Approved backend removal | Owned service stops and cannot autostart, owned runtime/integration files disappear, any exact legacy startup block is removed, usage/import state remains, and the plugin checkout survives. |
| Modified/foreign target | The conflicting file is preserved and a precise error is visible; unrelated files are not deleted. |
| Plugin removed first | The still-installed backend CLI can clean up without reading the deleted checkout. |

UI checks need a real rendered window. Shell integration needs a loaded widget, and login startup needs the graphical user target, not just a running daemon. Check the [user-facing diagnostics](../README.md#troubleshooting). For a documentation screenshot, use a disposable profile with synthetic evidence, expand an **Info** explanation and capture only the dashboard. Do not publish real counts, local paths or session details.

For host checks, obtain explicit consent for the exact temporary files and settings. Use synthetic history instead of the real history file. Never lock the screen automatically, inspect credentials, or overwrite the full shell configuration to restore one field. Remove only owned test resources and preserve unrelated concurrent changes.

## Release checklist

- **A1:** Run the regression suite and validate a clean distribution checkout with `omarchy plugin validate`. Do not validate a payload containing the development venv.
- **A2:** Exercise installation, update, consent and removal in a disposable environment through the native Omarchy CLI. Record which platform boundaries were real. A fixture with a substituted user manager does not verify login startup, and a local Git remote does not verify GitHub installation.
- **A3:** Launch the installed dashboard from outside both source directories, then activate it again. Verify one window, an installed-wheel bridge running in Python isolated mode, and a manifest-owned executable. Neither import nor binary may resolve into a checkout.
- **A4:** Review first-use, tracking, update, and removal consent text against the actual files and effects. Keep the README aligned with those operations.
- **A5:** Publish the reviewed repository to GitHub and record the clone URL. Publishing is an explicit user action, not an automatic verification step.
- **A6:** On a clean Omarchy user environment, install from that GitHub URL using the documented end-user process. Validate real service startup and native bar launch before calling the published installation verified.

No distribution check should require `./install.sh`, an editable runtime, an ad hoc launcher in the real home directory, or manual copying into Omarchy's plugin directory.
