# Oma Usage

A local GTK4/libadwaita dashboard and Omarchy bar plugin for software usage evidence. No web server, privileged collector, automatic software removal, or online backend.

## Install through Omarchy

Requires Python 3.12+, pacman, systemd user services, and an Omarchy shell with Git-managed plugins and mixed `service`/`bar-widget` support. Run as your normal desktop user, never root.

Install native prerequisites if missing:

```sh
omarchy pkg add python-gobject gtk4 libadwaita
```

Use Omarchy's normal plugin installation process:

```sh
omarchy plugin add
```

Paste the published oma-usage repository's GitHub clone URL when prompted. Review the unsandboxed-plugin warning, enable `oma.usage`, and choose its bar section. The suggested section is right. If you added it without enabling:

```sh
omarchy plugin enable oma.usage --section right
```

Click **Usage**. First use has two separate approvals:

1. **Runtime setup:** review the backend files and Python dependency downloads. Approval prepares an external private virtual environment and opens the dashboard. It does not import history, edit `.bashrc`, or enable collection.
2. **Enable local tracking:** review exact paths, the selected Bash history source, collection scopes, and privacy limits. Approval enables the user service, performs the one-time history import, adds the managed Bash source block, and starts collection.

Canceling either approval does not authorize the next stage. Existing shells load the Bash hook only after opening a new shell or explicitly sourcing the installed hook. Failed tracking stages remain visible and can be retried.

Omarchy owns the installed Git checkout at `~/.config/omarchy/plugins/oma.usage`. The development repository is source only. The runtime is an installed copy in a separate virtual environment, not an editable import of either checkout. Backend setup never overwrites the plugin checkout, replaces `shell.json`, modifies `/usr/share/omarchy`, or installs native packages.

### Update

```sh
omarchy plugin update oma.usage
```

Click **Usage** after an update. When backend source has changed, close the existing dashboard, then review and approve the runtime update. Updating the Git checkout alone does not update the installed Python runtime. An approved runtime update preserves tracking settings and may restart an already-running owned collector. It does not repeat the completed history import.

### Remove

Remove the backend before removing the plugin:

1. In dashboard **Settings**, choose **Remove local backend…**, review the removal preview, and confirm.
2. Remove the Git-managed widget:

   ```sh
   omarchy plugin remove oma.usage
   ```

The equivalent backend CLI first previews without changes, then requires an explicit affirmative flag:

```sh
~/.local/bin/oma-usage uninstall
~/.local/bin/oma-usage uninstall --yes
```

Backend removal stops/disables the owned user service, removes the owned runtime and generated integration files, removes only the exact managed Bash source block, and leaves collection disabled. It preserves every byte outside that block, the usage database, history-import marker, and other user settings. Foreign or modified files are reported rather than silently deleted. Already-open Bash shells may retain the in-memory prompt function until closed; its missing helper is quiet.

`omarchy plugin remove` only removes the plugin checkout. It has no backend uninstall hook. If you removed the plugin first, the installed `oma-usage uninstall` command still works. If you never approved runtime setup, there is no backend to remove.

### Installation troubleshooting

| Symptom | Action |
| --- | --- |
| Usage does not open a setup window | Check Python/native prerequisites above. Run `/usr/bin/python3 -I ~/.config/omarchy/plugins/oma.usage/bootstrap.py` from a terminal to see startup errors. |
| Dependency download or runtime build fails | Check network/package-index access and retry runtime setup. No tracking approval is implied. |
| Setup reports a foreign or modified installation target | Review the reported file. Resolve the conflict explicitly; setup does not overwrite user files. |
| `.bashrc` is linked, read-only, or has an edited managed block | Follow the displayed manual source instruction or repair that conflict before retrying tracking setup. |
| No command observations from an existing terminal | Open a new Bash shell after setup. History omissions and disabled history remain coverage gaps. |
| History import is incomplete | Check the selected readable history file in Settings and retry. Raw history is never persisted by the plugin. |
| A source is unavailable | Inspect Settings and source coverage. Missing coverage is not evidence of zero use. |

## What the numbers mean

| Source | Metric | Important boundary |
| --- | --- | --- |
| Applications | Estimated non-idle focused seconds and focus sessions | Five-second snapshots. The idle threshold defaults to 60 seconds. No background runtime, playback, window titles, or websites. |
| Commands | History-visible syntactic mentions | Not successful executions, branch execution, loop iterations, or command duration. Arguments and executable paths are discarded. |
| Services | Observed running seconds and observed starts | User and system scopes remain separate. Already-running baselines add no starts. Active/exited oneshots add no running time. |
| Inventory | Installed packages and discovered entry points | Installation is not usage. Package install/update date is not original installation or first use. |

Most and Least lists contain positive evidence only. Zero rows distinguish **Not observed** from **Insufficient coverage**. Bash prompts establish point observations, not continuous daily coverage. Untimestamped history counts remain separate from every date-based ranking and have no fabricated last-used timestamp.

The dashboard offers 7/30/90/365 UTC-day windows. App categories sum focus time only. Curated command use cases sum mentions separately. There is no combined usage score.

## Review and alternatives

Review candidates must be explicitly installed, have tracked entry points, have been present for the stale threshold, have no recent dated signal, and have at least 30 qualifying days for every mapped source. A qualifying desktop/service day has 30 minutes of connected coverage. Bash needs a live prompt observation. Running owned services block review. Unknown ownership, dependency installs, and insufficient coverage exclude candidates.

Review details show retained evidence, undated counts, coverage, installed size, package install/update date, dependencies, and enabled/running services. Dependency relationships are refreshed read-only when details open. “Few recorded uses” means 1–5 retained mentions or focus sessions, not lifetime use. Package review provides no package-uninstall button, service-disabling action, or “safe to remove” claim. Removing Oma Usage's own backend is a separate Settings operation.

The offline catalog contains directed comparisons for text search, filename search, resource monitoring, modal editing, terminal emulation, and container workflows. Installed options come first, including already-used targets. Each comparison states its advantage, tradeoff, primary source, review date, installation status, and available usage evidence. Catalog review dates are not claims about current online releases.

Online research is optional. A confirmation previews the exact canonical-tool/use-case query and DuckDuckGo destination before opening the default browser. Unsupported tools require an editable name selection. Merely opening the dashboard or Alternatives makes no online request. Browser results are not fetched or summarized by the plugin.

## Privacy, storage, and settings

Only sanitized identities and aggregates are stored. No raw history records, command arguments, working directories, user executable paths, document names, browsing history, or window titles are retained. Ownership paths are transient inventory metadata. The collector exposes only a same-UID Unix socket.

| Artifact | Default location |
| --- | --- |
| Usage database | `$XDG_DATA_HOME/oma-usage/usage.sqlite3` or `~/.local/share/oma-usage/usage.sqlite3` |
| Configuration | `$XDG_CONFIG_HOME/oma-usage/config.json` or `~/.config/oma-usage/config.json` |
| Collector socket | `$XDG_RUNTIME_DIR/oma-usage/collector.sock` |
| Launcher | `~/.local/bin/oma-usage` |
| Plugin | `~/.config/omarchy/plugins/oma.usage/` |
| Bash hook | `$XDG_DATA_HOME/oma-usage/oma-usage.bash` |
| Installed Python runtime | `$XDG_DATA_HOME/oma-usage/venv/` |
| Backend ownership manifest | `$XDG_DATA_HOME/oma-usage/installed-files.json` |
| User service unit | `$XDG_CONFIG_HOME/systemd/user/oma-usage.service` |
| Desktop entry | `$XDG_DATA_HOME/applications/org.omarchy.OmaUsage.desktop` |

`XDG_RUNTIME_DIR` is required. Private directories use 0700 and configuration/database/socket files use 0600. SQLite uses WAL, foreign keys, and a single daemon writer. The dashboard opens read-only connections. Malformed configuration or an incompatible/corrupt database produces an error, not an automatic reset.

Collection defaults off. Settings permit retention of 90/365 days, stale thresholds of 30/90/180 days, and idle thresholds of 60/120/300 seconds. Daily usage and coverage expire together. Entity first/last evidence and undated imported counts survive daily expiry. Missing expired coverage cannot qualify a package for review.

Clear usage history requires confirmation. It deletes usage, coverage, and first/last evidence while retaining inventory, settings, sequence deduplication, and the completed-import marker. Previously imported Bash history is not reread after clearing.

History is imported once transactionally, including an empty snapshot. Unreadable history remains explicitly not imported. The Bash hook preserves scalar/array prompt hooks and exit status, respects history omissions, and never rewrites history settings or forces history writes. Each helper invocation has a 250 ms deadline. Paused/unavailable preflight avoids reading the private record. Duplicate suppression, erased duplicates, disabled history, aliases/functions, unsupported packaging, remote activity, and undiscovered scripts remain coverage gaps.

Daemon downtime, observer reload/disconnect, idle, lock, suspend, pause, and clock discontinuities are never backfilled into app attention. An unknown lock state prevents app-time credit and qualifying desktop coverage.

## Development

See [Development and release verification](docs/development.md) for the source/runtime boundary, component responsibilities, isolated checks, and the GitHub release checklist.

