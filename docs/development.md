# Development and release verification

## Source is not runtime

This repository is the source of truth for development. Do not install a runtime from your working tree to check whether distribution works. The supported end-user entry point is Omarchy's Git-managed plugin installation, described in the [user guide](../README.md#install-through-omarchy).

Omarchy owns the installed checkout. Backend setup owns only the external runtime and generated integrations recorded in its ownership manifest. The runtime must remain a normal installed Python package, never an editable install or a `PYTHONPATH` pointing at the development tree. Installing or updating the backend must not create build artifacts inside the plugin checkout.

## Components

| Component | Responsibility |
| --- | --- |
| `manifest.json` | Native Omarchy plugin identity, service/widget entry points, suggested bar placement. |
| `BarWidget.qml` | Resolve the bootstrap beside the installed widget and launch it with the system Python in isolated mode. |
| `bootstrap.py` | Native single-instance runtime consent, dependency preparation, update detection, and launch of the installed dashboard. |
| `oma_usage/installer.py` | Validate the installed source location, serialize lifecycle operations, prepare the external venv, and track ownership of generated backend files. |
| `oma_usage/setup.py` | Separate tracking consent and resumable setup stages: service, history import, Bash integration, plugin readiness, collectors. |
| `oma_usage/uninstall.py` | Preview and remove the owned backend while retaining usage data and leaving Omarchy's checkout untouched. |
| `Service.qml` | Foreground app-ID/idle observer. Send no titles or paths; reconnect without backfilling gaps. |
| `oma_usage/daemon.py` | Single database writer, lock-state gate, collector status, and private same-UID socket protocol. |
| `oma_usage/store.py`, `accounting.py` | Storage, validation, daily retention, and focus/service accounting invariants. |
| `inventory.py`, `insights.py`, `recommendations.py` | Read-only inventory discovery, evidence reports, conservative package review, curated alternatives. |
| `ui.py`, `cli.py` | Native dashboard, explicit actions, and bounded collector/maintenance commands. |
| `integrations/` | Generated user-unit and desktop-entry templates plus the name-only Bash prompt hook. |

The plugin manager has no install/update/remove script hooks. Never assume that cloning the repository runs Python setup or that removing the widget cleans up its external service. Runtime refresh follows a payload fingerprint rather than the manifest version alone. A native plugin update and an approved backend update are separate operations.

## Development environment

A development venv is for local code and regression checks only:

```sh
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```

GTK/Gio dependencies come from native system packages. Python parser versions are pinned in `pyproject.toml`. Do not commit the venv, build products, egg metadata, local databases, or credentials. Native plugin validation rejects symlinks inside a release checkout, excluding Git's own metadata; an ignored development venv must not be included in the distribution fixture.

The following commands are developer/collector interfaces, not substitutes for installing the plugin:

```sh
.venv/bin/python -m oma_usage --help
.venv/bin/python -m oma_usage daemon
.venv/bin/python -m oma_usage import-history --file /path/to/synthetic-history
```

Run mutating commands only with an isolated `HOME` and XDG environment. An isolated config directory alone is insufficient: Omarchy plugins, the launcher, and `.bashrc` use `HOME`. A private runtime directory alone is also insufficient if commands still reach the host's session bus or systemd user manager.

## Verification boundaries

Use a disposable installation fixture, synthetic history, and a separate installed Git checkout. Keep the development repository off the runtime import path. At minimum, exercise these behaviors:

| Scenario | Required observable result |
| --- | --- |
| Native add/enable | Omarchy validates the root manifest and discovers/enables the mixed service/widget plugin. No custom installer is invoked by cloning. |
| Fresh widget activation | Runtime consent opens despite no `~/.local/bin/oma-usage`. |
| Decline runtime setup | No runtime, service, startup block, history read, or dependency download. |
| Approve runtime setup | External venv and owned integration files appear; the installed dashboard opens; tracking stays off. Source checkout remains unchanged. |
| Repeat activation | The existing setup/dashboard is reused rather than producing duplicate windows. |
| Tracking preview/cancel | Exact managed paths and history source are shown; cancel starts no collectors and changes no shell startup configuration. |
| Approve tracking | Actual stage outcomes are reported, selected synthetic history imports once, source states are honest, and only the managed startup block is added. |
| Native update | Changed backend payload prompts a runtime update; unchanged payload does not. Approved update preserves user settings and pause state. |
| Removal preview/cancel | No destructive effects. |
| Approved backend removal | Owned service stops and cannot autostart, owned runtime/integration files disappear, exact source block is removed, usage/import state remains, plugin checkout survives. |
| Modified/foreign target | The conflicting file is preserved and a precise error is visible; unrelated files are not deleted. |
| Plugin removed first | The still-installed backend CLI can clean up without reading the deleted checkout. |

UI checks need the actual native surface. Unit tests do not establish that a widget was loaded into a live bar. Compositor focus, idle, suspend, and manual lock checks need their own evidence. Never claim a real service was enabled if a fixture substituted the platform boundary, or claim a GitHub installation if the test used a local Git remote.

For host checks, obtain explicit consent for the exact temporary files and settings. Use synthetic history instead of the real history file. Never lock the screen automatically, inspect credentials, or overwrite the full shell configuration to restore one field. Remove only owned test resources and preserve unrelated concurrent changes.

## Release checklist

- **A1:** Run the regression suite and validate a clean distribution checkout with `omarchy plugin validate`. Do not validate a payload containing the development venv.
- **A2:** Exercise the installation and removal scenarios above through the native Omarchy CLI. Record which platform boundaries were real and which were isolated fixtures.
- **A3:** Verify the runtime from outside both source directories, with Python isolated mode. Confirm no runtime import points back to the development checkout.
- **A4:** Review first-use, tracking, update, and removal consent text against the actual files and effects. Keep the README aligned with those operations.
- **A5:** Publish the reviewed repository to GitHub and record the clone URL. Publishing is an explicit user action, not an automatic verification step.
- **A6:** On a clean Omarchy user environment, install from that GitHub URL using the documented end-user process. Validate real service startup and native bar launch before calling the published installation verified.

No distribution check should require `./install.sh`, an editable runtime, an ad hoc launcher in the real home directory, or manual copying into Omarchy's plugin directory.
