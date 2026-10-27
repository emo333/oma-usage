"""Native, read-only dashboard. Only explicit actions contact the writer or browser.

``load_snapshot`` is the display-independent worker boundary; each call owns and
closes its own read-only connection. GTK objects never cross that boundary.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Adw, Gio, GLib, GObject, Gtk

from . import client, insights, inventory, recommendations, store

PERIODS = (7, 30, 90, 365)


def error_code(error):
    """Never render subprocess output, parser source, or private exception text."""
    return getattr(error, 'code', None) or type(error).__name__


def load_snapshot(days=30, *, include_preview=False):
    """Read actual local evidence on a worker, without creating any state."""
    result = {'report': None, 'errors': [], 'settings': None}
    # The offline catalog remains browsable without a database. These are
    # explicitly unknown badges, never invented inventory or usage statistics.
    result['observed_comparisons'] = []
    result['all_comparisons'] = [
        {**row, 'installation': 'Installation status unknown',
         'usage_state': 'Insufficient coverage', 'undated_count': None,
         'source_observed': None}
        for row in recommendations.catalog()
    ]
    try:
        result['settings'] = store.load_config()
    except Exception as exc:
        result['errors'].append('Configuration unavailable: ' + error_code(exc) + '. Repair config.json; settings were not reset.')
    database = store.paths()['data'] / 'usage.sqlite3'
    if database.exists():
        try:
            conn = store.readonly()
            try:
                result['report'] = insights.build_report(conn, days=days, now=datetime.now(timezone.utc))
            finally:
                conn.close()
            report = result['report']
            result['observed_comparisons'] = recommendations.build_recommendations(report)
            result['all_comparisons'] = recommendations.build_recommendations(report, observed_only=False)
        except Exception as exc:
            result['errors'].append('Database unavailable: ' + error_code(exc) + '. Check schema, permissions, and writer availability; the database has not been reset.')
    try:
        result['live_settings'] = client.observer_settings(timeout=0.2)
    except Exception as exc:
        result['errors'].append('Collector unavailable: ' + error_code(exc) + '. Retry refresh or retry local setup below.')
    try:
        from . import setup
        result['setup_status'] = setup.status()
    except Exception as exc:
        result['errors'].append('Setup status unavailable: ' + error_code(exc))
    if include_preview:
        try:
            from . import setup
            result['preview'] = setup.preview()
        except Exception as exc:
            result['errors'].append('Setup preview unavailable: ' + error_code(exc))
    return result


def text(value):
    if value is None:
        return 'Unknown / no dated evidence'
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def duration(value):
    seconds = float(value or 0)
    return f'{seconds / 3600:,.2f} h ({seconds:,.1f} s)'


def label(value, *, heading=False):
    widget = Gtk.Label(label=str(value), xalign=0, wrap=True, selectable=True)
    if heading:
        widget.add_css_class('heading')
    return widget


def box(spacing=12):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)


def button(title, callback):
    widget = Gtk.Button(label=title)
    widget.connect('clicked', lambda *_: callback())
    return widget


class Record(GObject.Object):
    def __init__(self, data):
        super().__init__()
        self.data = data


class EvidenceTable(Gtk.Box):
    """Plain-text sortable columns with stable data and keyboard row activation."""
    def __init__(self, columns, on_activate, *, metric=False):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.rows = []
        self.columns = columns
        self.on_activate = on_activate
        tools = Gtk.Box(spacing=8)
        self.search = Gtk.SearchEntry(placeholder_text='Search names and evidence', hexpand=True)
        self.search.set_tooltip_text('Search this table; activate a row with Enter for details')
        self.search.connect('search-changed', lambda *_: self.refilter())
        tools.append(self.search)
        self.mode = None
        if metric:
            self.mode = Gtk.DropDown.new_from_strings(['Most used', 'Least used', 'Not observed', 'All inventory'])
            self.mode.set_tooltip_text('Usage filter; All inventory includes removed identities and templates')
            self.mode.connect('notify::selected', lambda *_: self.refilter())
            tools.append(self.mode)
        self.append(tools)
        self.summary = label('No report loaded')
        self.append(self.summary)
        self.store = Gio.ListStore.new(Record)
        self.sorted = Gtk.SortListModel(model=self.store)
        self.selection = Gtk.SingleSelection(model=self.sorted)
        self.view = Gtk.ColumnView(model=self.selection, show_row_separators=True, vexpand=True)
        self.view.set_single_click_activate(False)
        self.view.connect('activate', self._activate)
        for title, key, formatter in columns:
            factory = Gtk.SignalListItemFactory()
            factory.connect('setup', self._setup_cell)
            factory.connect('bind', self._bind_cell, key, formatter)
            column = Gtk.ColumnViewColumn(title=title, factory=factory, resizable=True)
            sorter = Gtk.CustomSorter.new(lambda a, b, *_args, k=key: self._compare(a.data.get(k), b.data.get(k)))
            column.set_sorter(sorter)
            self.view.append_column(column)
        self.sorted.set_sorter(self.view.get_sorter())
        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True, min_content_height=240)
        scroller.set_child(self.view)
        self.append(scroller)
        self.append(button('Open selected details', self.open_selected))

    @staticmethod
    def _compare(a, b):
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return Gtk.Ordering.SMALLER if a < b else Gtk.Ordering.LARGER if a > b else Gtk.Ordering.EQUAL
        a, b = text(a).casefold(), text(b).casefold()
        return Gtk.Ordering.SMALLER if a < b else Gtk.Ordering.LARGER if a > b else Gtk.Ordering.EQUAL

    @staticmethod
    def _setup_cell(_factory, item):
        item.set_child(Gtk.Label(xalign=0, margin_start=8, margin_end=8, margin_top=6, margin_bottom=6))

    @staticmethod
    def _bind_cell(_factory, item, key, formatter):
        value = item.get_item().data.get(key)
        item.get_child().set_text(formatter(value) if formatter else text(value))

    def _activate(self, _view, position):
        item = self.sorted.get_item(position)
        if item:
            self.on_activate(item.data)

    def open_selected(self):
        item = self.selection.get_selected_item()
        if item:
            self.on_activate(item.data)

    def set_rows(self, rows, *, metric_key=None):
        self.rows = rows
        self.metric_key = metric_key
        self.refilter()

    def refilter(self):
        query = self.search.get_text().casefold().strip()
        rows = self.rows
        if self.mode and getattr(self, 'metric_key', None):
            mode = self.mode.get_selected()
            key = self.metric_key
            if mode in (0, 1):
                rows = sorted((r for r in rows if not r.get('template') and r.get(key, 0) > 0),
                              key=lambda r: ((-1 if mode == 0 else 1) * r[key], r['key']))
            elif mode == 2:
                rows = [r for r in rows if r.get('installed') and not r.get('template') and r.get(key, 0) == 0]
        if query:
            rows = [r for r in rows if query in ' '.join(text(v) for v in r.values()).casefold()]
        self.store.splice(0, self.store.get_n_items(), [Record(r) for r in rows])
        self.summary.set_text(f'{len(rows):,} rows · column headings sort · Enter opens details')


class UsageWindow(Adw.ApplicationWindow):
    def __init__(self, application):
        super().__init__(application=application, title='Oma Usage', default_width=1180, default_height=800)
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='oma-usage-ui')
        self.report = None
        self.snapshot = {}
        self.preview = None
        self.refreshing = False
        self.action_busy = False
        self.closed = False
        self.removing_backend = False
        self.period_generation = 0
        self.tables = {}
        root = box(0)
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title='Oma Usage', subtitle='Local evidence, not a never-used claim'))
        self.period = Gtk.DropDown.new_from_strings([f'{n} UTC days' for n in PERIODS])
        self.period.set_selected(1)
        self.period.set_tooltip_text('Shared reporting period, including today (UTC)')
        self.period.connect('notify::selected', self._period_changed)
        header.pack_end(self.period)
        header.pack_end(button('Refresh', self.refresh))
        root.append(header)
        self.global_status = label('Reading local evidence…')
        self.global_status.set_margin_start(16)
        self.global_status.set_margin_end(16)
        root.append(self.global_status)
        self.operation_status = label('')
        self.operation_status.set_margin_start(16)
        self.operation_status.set_margin_end(16)
        root.append(self.operation_status)
        body = Gtk.Box(spacing=12, vexpand=True)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, hexpand=True, vexpand=True)
        sidebar = Gtk.StackSidebar(stack=self.stack)
        sidebar.set_size_request(170, -1)
        body.append(sidebar)
        body.append(self.stack)
        root.append(body)
        self.set_content(root)
        self.overview = self._page('overview', 'Overview')
        self.overview_content = box()
        self.overview.append(self.overview_content)
        self._metrics_page('apps', 'Applications', 'app')
        self._metrics_page('commands', 'Commands', 'command')
        self._metrics_page('services', 'Services', 'service')
        review = self._page('review', 'Review')
        review.append(label('Package-level review only. No uninstall or service-disabling actions. Every mapped source needs 30 qualifying days; inventory is not usage.'))
        self.review_mode = Gtk.DropDown.new_from_strings(['Review candidates', 'All package inventory'])
        self.review_mode.connect('notify::selected', lambda *_: self._render_review())
        review.append(self.review_mode)
        self.tables['review'] = EvidenceTable([
            ('Package', 'key', None), ('Review evidence', 'review_state', None),
            ('Installed', 'installed', None), ('Explicit install', 'explicit_install', None),
            ('Last dated observation', 'last_observed_at', None),
            ('Installed bytes', 'installed_size_bytes', None)], self.show_entity)
        review.append(self.tables['review'])
        alternatives = self._page('alternatives', 'Alternatives')
        alternatives.append(label('Directed, locally reviewed comparisons, not universal rankings. Installed choices come first. Rendering this page makes no online requests.'))
        self.alternative_mode = Gtk.DropDown.new_from_strings(['Observed sources in this period', 'All curated use cases'])
        self.alternative_mode.connect('notify::selected', lambda *_: self._render_alternatives())
        alternatives.append(self.alternative_mode)
        self.use_case = Gtk.DropDown.new_from_strings(['All use cases'] + sorted({r['use_case'] for r in recommendations.catalog()}))
        self.use_case.connect('notify::selected', lambda *_: self._render_alternatives())
        alternatives.append(self.use_case)
        self.tables['alternatives'] = EvidenceTable([
            ('Use case', 'use_case', None), ('Source', 'source_name', None), ('Alternative', 'target_name', None),
            ('Installation', 'installation', None), ('Actual target evidence', 'usage_state', None),
            ('Undated mentions', 'undated_count', None)], self.show_comparison)
        alternatives.append(self.tables['alternatives'])
        settings = self._page('settings', 'Settings')
        self._build_settings(settings)
        self.connect('close-request', self._closing)
        self.connect('notify::visible', lambda *_: self.refresh() if self.get_visible() else None)
        self.timer = GLib.timeout_add_seconds(5, self._tick)
        self.refresh()

    def _page(self, name, title):
        page = box()
        for side in ('start', 'end', 'top', 'bottom'):
            getattr(page, 'set_margin_' + side)(16)
        page.append(label(title, heading=True))
        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(page)
        self.stack.add_titled(scroller, name, title)
        return page

    def _metrics_page(self, name, title, kind):
        page = self._page(name, title)
        page.append(label(insights.DEFINITIONS[kind]))
        count_title = 'Dated submitted mentions' if kind == 'command' else 'Observed starts' if kind == 'service' else 'Focus sessions'
        time_title = 'Observed running time' if kind == 'service' else 'Focused time'
        columns = [('Name', 'label', None), ('Identity', 'key', None), ('Evidence', 'usage_state', None),
                   (count_title, 'count', None)]
        if kind != 'command':
            columns.append((time_title, 'seconds', duration))
        columns += [('Undated imported mentions', 'undated_count', None), ('Installed', 'installed', None)]
        table = EvidenceTable(columns, self.show_entity, metric=True)
        self.tables[name] = table
        page.append(table)

    def _build_settings(self, page):
        self.collection_status = label('Collector status unknown')
        page.append(self.collection_status)
        controls = Gtk.Box(spacing=8)
        controls.append(button('Pause collection', lambda: self.control('pause')))
        controls.append(button('Resume collection', lambda: self.control('resume')))
        controls.append(button('Retry source status', self.refresh))
        page.append(controls)
        page.append(label('Source retry refreshes live status. A disconnected observer/bus retries through its collector; local setup can be retried below.'))
        self.setting_choices = {}
        for key, title, values in [('retention_days', 'Daily retention', (90, 365)),
                                   ('stale_days', 'Stale review threshold', (30, 90, 180)),
                                   ('idle_seconds', 'Idle threshold', (60, 120, 300))]:
            row = Gtk.Box(spacing=12)
            row.append(label(title))
            dropdown = Gtk.DropDown.new_from_strings([str(v) for v in values])
            self.setting_choices[key] = (dropdown, values)
            row.append(dropdown)
            page.append(row)
        page.append(button('Apply settings', self.apply_settings))
        page.append(label('Ninety-day retention with a 180-day stale threshold may leave insufficient review coverage. First/last dated evidence survives aggregate expiry.'))
        self.history_status = label('History import status unknown')
        page.append(self.history_status)
        self.history_file = Gtk.Entry(placeholder_text='Bash history source (empty uses setup/default source)', hexpand=True)
        self.history_file.set_tooltip_text('Explicit history source for setup or retry; never command text')
        page.append(self.history_file)
        history_controls = Gtk.Box(spacing=8)
        history_controls.append(button('Choose history file…', self.choose_history))
        history_controls.append(button('Retry one-time history import', self.retry_history))
        page.append(history_controls)
        page.append(label('A completed import is never replayed, including after clearing. Retry pauses collection transactionally and restores its previous state. History-visible names only; undated mentions do not enter date rankings.'))
        clear = button('Clear usage history…', self.confirm_clear)
        clear.add_css_class('destructive-action')
        page.append(clear)
        page.append(label('Local tracking setup', heading=True))
        self.setup_summary = label('Loading read-only setup preview…')
        page.append(self.setup_summary)
        page.append(button('Preview selected setup…', self.preview_setup))
        page.append(button('Enable local tracking…', self.confirm_setup))
        self.setup_result = label('No setup action has been requested.')
        page.append(self.setup_result)
        remove = button('Remove local backend…', self.confirm_remove_backend)
        remove.add_css_class('destructive-action')
        page.append(remove)
        page.append(label('Backend removal keeps usage and settings. Afterwards, remove the native plugin separately with: omarchy plugin remove oma.usage'))

    def _closing(self, *_):
        if self.removing_backend:
            return True
        self.closed = True
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = None
        self.pool.shutdown(wait=False, cancel_futures=True)
        return False

    def _tick(self):
        if self.closed:
            return GLib.SOURCE_REMOVE
        if self.get_visible():
            self.refresh()
        return GLib.SOURCE_CONTINUE

    def _period_changed(self, *_):
        self.period_generation += 1
        if hasattr(self, 'pool') and hasattr(self, 'global_status'):
            self.refresh()

    def submit(self, work, done):
        if self.closed or self.removing_backend:
            return
        future = self.pool.submit(work)

        def completed(task):
            try:
                value, error = task.result(), None
            except Exception as exc:
                value, error = None, error_code(exc)

            def deliver():
                if not self.closed:
                    done(value, error)
                return GLib.SOURCE_REMOVE
            GLib.idle_add(deliver)
        future.add_done_callback(completed)

    def refresh(self):
        if self.refreshing or self.closed or self.removing_backend:
            return
        self.refreshing = True
        generation = self.period_generation
        days = PERIODS[self.period.get_selected()]
        self.submit(lambda: load_snapshot(days, include_preview=self.preview is None),
                    lambda value, error: self._loaded(value, error, generation))

    def _loaded(self, value, error, generation):
        self.refreshing = False
        if generation != self.period_generation:
            self.refresh()
            return
        if error:
            self.global_status.set_text('Read failed: ' + error + '. Retry Refresh; no evidence was reset.')
            return
        self.snapshot = value
        self.report = value['report']
        if value.get('preview'):
            self.preview = value['preview']
            self.setup_summary.set_text(self.preview_text(self.preview))
        if value.get('setup_status') and not self.action_busy:
            state = value['setup_status']
            self.setup_result.set_text('Persisted local setup stages:\n' + text(state))
        errors = value['errors'][:]
        if self.report:
            for source in self.report['source_coverage']:
                if source['state'] in ('error', 'unavailable') or source.get('error_code'):
                    errors.append(f"{source['source']}: {source['state']} · {source.get('error_code') or 'no error code'}")
            for source, code in self.report['inventory']['errors'].items():
                errors.append(f'Inventory {source}: {text(code)}')
        self.global_status.set_text('\n'.join(errors) if errors else 'Local evidence refreshed. No network requests.')
        if not self.report or not (self.report['package_review']['inventory'] or any(self.report[name]['rows'] for name in ('apps', 'commands', 'services'))):
            self._first_run()
        else:
            for name in ('apps', 'commands', 'services'):
                section = self.report[name]
                self.tables[name].set_rows(section['rows'], metric_key=section['metric'])
            self._render_overview()
            self._render_review()
            self._render_alternatives()
        settings = value.get('settings')
        live = value.get('live_settings')
        if live:
            self.collection_status.set_text('Collection enabled' if live['collection_enabled'] else 'Collection paused / not enabled')
        else:
            self.collection_status.set_text('Collector unavailable — pause state cannot be confirmed')
        if settings:
            for key, (dropdown, choices) in self.setting_choices.items():
                # Do not overwrite a user's unsubmitted settings every five seconds.
                if not hasattr(dropdown, '_initialised'):
                    dropdown.set_selected(choices.index(settings[key]))
                    dropdown._initialised = True
        if self.report:
            history = self.report['history']
            self.history_status.set_text(f"History: {history['state']} · completed: {text(history['completed_at'])} · undated mentions: {history['undated_count']:,}")
        else:
            self.history_status.set_text('No database evidence available; import status is not assumed.')

    @staticmethod
    def clear_box(widget):
        while widget.get_first_child():
            widget.remove(widget.get_first_child())

    def _first_run(self):
        self.clear_box(self.overview_content)
        opening = 'The usage database has no inventoried identities yet.' if self.report else 'No readable usage database.'
        self.overview_content.append(label(opening + ' Opening this dashboard does not start a collector, inventory scan, or history import. If this is your first visit, review exactly what local setup changes before enabling tracking. An existing database error is shown above and is never reset.'))
        if self.preview:
            self.overview_content.append(label(self.preview_text(self.preview)))
        self.overview_content.append(button('Preview / enable local tracking…', self.confirm_setup))
        for table in self.tables.values():
            table.set_rows([])
        self._render_alternatives()

    def _render_overview(self):
        target = self.overview_content
        self.clear_box(target)
        report = self.report
        target.append(label(f"Period: {report['period']['start_at']} — {report['period']['end_at']} (UTC)"))
        for key, title in [('apps', 'Leading applications · focused time'), ('commands', 'Leading commands · dated submitted mentions'), ('services', 'Service activity · observed running time')]:
            target.append(label(title, heading=True))
            rows = report[key]['most'][:5]
            target.append(label('\n'.join(f"{r['label']}: {duration(r['seconds']) if key != 'commands' else str(r['count']) + ' mentions'}" for r in rows) or 'No positive dated evidence in this period.'))
            target.append(label(report[key]['definition']))
        target.append(label('Application category totals · focus time only', heading=True))
        target.append(label('\n'.join(f"{r['category']}: {duration(r['seconds'])}" for r in report['categories']) or 'No category focus evidence.'))
        target.append(label('Curated command use cases · dated command mentions', heading=True))
        target.append(label('\n'.join(f"{r['use_case']}: {r['count']} mentions" for r in report['command_use_cases']) or 'No curated command evidence.'))
        target.append(label(f"Package review candidates: {len(report['package_review']['candidates'])} · not a removal recommendation"))
        target.append(label('Source coverage and persistent status', heading=True))
        for source in report['source_coverage']:
            target.append(label(f"{source['source']}: {source['state']} · error: {text(source.get('error_code'))}\nReliable observation began: {text(source['first_coverage_at'])}\nLast success: {text(source['last_success_at'])} · availability: {duration(source['covered_seconds'])} · observations: {source['observations']} · qualifying days: {source['qualifying_days']}\n{source['definition']} · gaps in selected window: {len(source['gaps'])}"))
            target.append(button('View coverage intervals / gaps', lambda row=source: self.show_coverage(row)))
        target.append(button('Retry source status / refresh evidence', self.refresh))
        target.append(label(f"Inventory refreshed: {text(report['inventory']['at'])}"))
        target.append(label(report['boundaries']))

    def _render_review(self):
        if self.report:
            section = self.report['package_review']
            self.tables['review'].set_rows(section['candidates'] if self.review_mode.get_selected() == 0 else section['inventory'])

    def _render_alternatives(self):
        key = 'observed_comparisons' if self.alternative_mode.get_selected() == 0 else 'all_comparisons'
        rows = self.snapshot.get(key, [])
        selected = self.use_case.get_selected_item().get_string()
        rows = [{**r, 'source_name': r['source']['package'], 'target_name': r['target']['package']} for r in rows if selected == 'All use cases' or r['use_case'] == selected]
        self.tables['alternatives'].set_rows(rows)

    def dialog(self, title, content, *, width=760):
        dialog = Adw.Dialog(title=title, content_width=width, content_height=620)
        layout = box(0)
        toolbar = Adw.HeaderBar()
        toolbar.set_title_widget(Adw.WindowTitle(title=title))
        layout.append(toolbar)
        scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        for side in ('start', 'end', 'top', 'bottom'):
            getattr(content, 'set_margin_' + side)(18)
        scroll.set_child(content)
        layout.append(scroll)
        layout.append(button('Close', dialog.close))
        dialog.set_child(layout)
        dialog.present(self)
        return dialog

    def show_coverage(self, source):
        content = box()
        for key, value in source.items():
            content.append(label(f'{key}: {text(value)}'))
        self.dialog('Source coverage · ' + source['source'], content)

    def show_entity(self, entity):
        content = box()
        content.append(label(entity.get('definition') or 'Package-level evidence, not a lifetime usage total. Dependency and service relationships are warnings, never permission to delete.'))
        names = {
            'key': 'Stable identity', 'kind': 'Kind', 'installed': 'Currently installed',
            'explicit_install': 'Explicitly installed', 'review_state': 'Review evidence',
            'usage_state': 'Selected-period evidence', 'first_observed_at': 'First dated observation',
            'last_observed_at': 'Last dated observation', 'present_since': 'Continuously present in inventory since',
            'first_inventory_at': 'First inventoried', 'count': 'Dated mentions / focus sessions / observed starts (see definition)',
            'seconds': 'Selected-period focused / observed running seconds', 'undated_count': 'Undated imported mentions (not dated use)',
            'retained_count': 'Retained dated count (not lifetime)', 'retained_seconds': 'Retained seconds (not lifetime)',
            'retained_command_mentions': 'Retained command mentions', 'retained_focus_sessions': 'Retained focus sessions',
            'retained_focus_seconds': 'Retained focused seconds', 'retained_service_seconds': 'Retained observed service seconds',
            'retained_service_starts': 'Retained observed starts', 'coverage_days': 'Qualifying days by mapped source',
            'enabled_services': 'Enabled service relationships', 'running_services': 'Currently running service relationships',
            'installed_size_bytes': 'Installed size in bytes', 'install_update_at': 'Package install/update date (not original installation)',
            'required_by': 'Required by', 'optional_for': 'Optional consumers', 'unit_file_state': 'Unit file state',
            'active_state': 'Active state', 'sub_state': 'Service substate', 'template': 'Uninstantiated template (not ranked)',
            'category': 'Broad launcher category', 'version': 'Package version'}
        for key, title in names.items():
            if key in entity:
                content.append(label(f'{title}: {text(entity[key])}'))
        if entity.get('few_recorded_uses') or (entity['kind'] in ('app', 'command') and 1 <= entity.get('retained_count', 0) <= 5):
            content.append(label('Few recorded uses — retained mentions or focus sessions, not lifetime counts.'))
        if entity.get('entry_points') is not None:
            content.append(label('Tracked entry points: ' + ', '.join(e['key'] for e in entity['entry_points'])))
        package = entity if entity['kind'] == 'package' else next((p for p in self.report['package_review']['inventory'] if p['id'] == entity.get('owner_package_id')), None)
        if package:
            current = label('Reading current package dependency relationships…')
            content.append(current)
            def update(value, error):
                if error:
                    current.set_text('Current package metadata unavailable: ' + error + '. Stored relationships above may be stale.')
                else:
                    current.set_text('Current pacman metadata (read-only):\n' + '\n'.join(f"{names.get(k, k)}: {text(v)}" for k, v in value.items()))
            self.submit(lambda: inventory.package_details(package['key']), update)
        else:
            content.append(label('Package ownership unknown / not mapped.'))
        content.append(label('Curated source comparisons', heading=True))
        comparison_content = box()
        comparison_content.append(label('Reading curated comparisons…'))
        content.append(comparison_content)
        report = self.report
        def comparisons_ready(comparisons, error):
            self.clear_box(comparison_content)
            if error:
                comparison_content.append(label('Comparison evidence unavailable: ' + error))
                comparison_content.append(button('Research newer alternatives…', self.research))
                return
            if comparisons:
                for comparison in comparisons:
                    comparison_content.append(button(comparison['use_case'] + ' → ' + comparison['target']['package'], lambda row=comparison: self.show_comparison(row)))
            else:
                comparison_content.append(label('No curated comparison yet'))
            canonical = comparisons[0]['source']['package'] if comparisons else None
            use_case = comparisons[0]['use_case'] if comparisons else None
            comparison_content.append(button('Research newer alternatives…', lambda: self.research(canonical, use_case)))
        self.submit(lambda: recommendations.comparisons_for(entity, report), comparisons_ready)
        self.dialog(entity['label'] + ' · evidence', content)

    def show_comparison(self, comparison):
        content = box()
        for title, value in [
            ('Use case', comparison['use_case']), ('Source tool', comparison['source']['package']),
            ('Alternative', comparison['target']['package']), ('Installation', comparison['installation']),
            ('Actual target evidence', comparison['usage_state']), ('Undated target mentions', comparison['undated_count']),
            ('Source observed in selected period', comparison['source_observed']),
            ('Advantage', comparison['advantage']), ('Tradeoff', comparison['tradeoff']),
            ('Primary source link', comparison['source_url']), ('Locally reviewed on', comparison['reviewed_at'])]:
            content.append(label(f'{title}: {text(value)}'))
        content.append(button('Open primary source link…', lambda: self.confirm_link(comparison['source_url'])))
        content.append(button('Research newer alternatives…', lambda: self.research(comparison['source']['package'], comparison['use_case'])))
        self.dialog('Curated comparison', content)

    def confirm_link(self, url):
        dialog = Adw.AlertDialog(heading='Open external source?', body='This opens your browser. No usage evidence is sent by Oma Usage.\n\nDestination: ' + url)
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('open', 'Open source')
        dialog.set_default_response('cancel')
        dialog.set_close_response('cancel')
        dialog.connect('response', lambda _dialog, response: self.launch_uri(url) if response == 'open' else None)
        dialog.present(self)

    def research(self, canonical=None, use_case=None):
        if canonical:
            self.confirm_research(canonical, use_case)
            return
        content = box()
        content.append(label('No curated comparison yet. Enter a canonical public tool name before preparing a browser query. Do not include private filenames, history, account names, or usage evidence.'))
        entry = Gtk.Entry(placeholder_text='Canonical tool name', activates_default=True)
        content.append(entry)
        validation = label('')
        content.append(validation)
        dialog = self.dialog('Select tool for research', content, width=620)
        def prepare():
            try:
                recommendations.research_query(entry.get_text())
            except ValueError:
                validation.set_text('Enter a nonempty canonical name without control characters (maximum 255 UTF-8 bytes).')
                return
            dialog.close()
            self.confirm_research(entry.get_text(), None)
        content.append(button('Preview exact research query…', prepare))
        entry.connect('activate', lambda *_: prepare())
        entry.grab_focus()

    def confirm_research(self, canonical, use_case):
        query = recommendations.research_query(canonical, use_case)
        dialog = Adw.AlertDialog(heading='Send this research query to your browser?', body=f"Exact query:\n{query['query']}\n\nDestination:\n{query['destination']}\n\nExact URL:\n{query['url']}\n\nOnly this selected tool / use-case query is disclosed. No inventory, counts, timestamps, or history are included. Results are not fetched back by this application.")
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('open', 'Open browser')
        dialog.set_default_response('cancel')
        dialog.set_close_response('cancel')
        dialog.connect('response', lambda _dialog, response: self.launch_uri(query['url']) if response == 'open' else None)
        dialog.present(self)

    def launch_uri(self, url):
        # Only disclosure-dialog affirmative responses reach this method.
        try:
            Gio.AppInfo.launch_default_for_uri(url, self.get_display().get_app_launch_context())
        except GLib.Error:
            self.operation_status.set_text('Browser launch failed. No result was fetched by Oma Usage.')

    def action(self, title, work, callback=None):
        if self.action_busy or self.removing_backend:
            self.operation_status.set_text('Another requested operation is still running; wait before retrying.')
            return
        self.action_busy = True
        self.operation_status.set_text(title + '…')
        def complete(value, error):
            self.action_busy = False
            if error:
                self.operation_status.set_text(title + ' failed: ' + error)
            else:
                self.operation_status.set_text(title + ': ' + text(value))
            if callback:
                callback(value, error)
            self.refresh()
        self.submit(work, complete)

    def confirm_remove_backend(self):
        if self.action_busy or self.removing_backend:
            self.operation_status.set_text('Wait for the current operation before removing the backend.')
            return
        from . import uninstall
        def ready(value, error):
            if error or not value.get('ok'):
                self.operation_status.set_text('Backend removal blocked: ' + (error or value['error']))
                return
            body = ('Remove these verified backend paths:\n' + '\n'.join(value['paths'])
                    + '\n\nManaged Bash block: ' + text(value['bash_startup'])
                    + '\n\n' + value['service']
                    + '\n\n' + '\n'.join(value['scopes'])
                    + '\n\nRetained:\n' + '\n'.join(value['retained'])
                    + '\n\nThe dashboard will close because its own runtime is removed.\n'
                    + value['next_step'])
            dialog = Adw.AlertDialog(heading='Remove local backend?', body=body)
            dialog.add_response('cancel', 'Cancel')
            dialog.add_response('remove', 'Remove local backend')
            dialog.set_response_appearance('remove', Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response('cancel')
            dialog.set_close_response('cancel')
            dialog.connect('response', lambda _dialog, answer: self._remove_backend() if answer == 'remove' else None)
            dialog.present(self)
        self.submit(uninstall.preview, ready)

    def _remove_backend(self):
        if self.action_busy or self.removing_backend:
            self.operation_status.set_text('Another operation started; preview removal again after it finishes.')
            return
        from threading import Thread
        from . import uninstall
        self.removing_backend = True
        self.action_busy = True
        self.operation_status.set_text('Removing local backend; waiting for dashboard readers to finish…')
        def work():
            # Drain readers and cancel queued work before deleting this process's
            # runtime. No periodic or manual refresh can enqueue more work.
            self.pool.shutdown(wait=True, cancel_futures=True)
            result = uninstall.remove_backend()
            GLib.idle_add(self._backend_removed, result)
        Thread(target=work, name='oma-usage-removal', daemon=False).start()

    def _backend_removed(self, result):
        if not result.get('ok') and not result.get('runtime_removed'):
            self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='oma-usage-ui')
            self.removing_backend = False
            self.action_busy = False
            self.operation_status.set_text('Backend removal incomplete: ' + text(result))
            return GLib.SOURCE_REMOVE
        # Leave refresh suspended while explaining the separate native action.
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = None
        body = ('Usage and settings were retained; collection is off.\n\n' + result['next_step']
                if result.get('ok') else 'The runtime was removed, but cleanup needs attention:\n' + text(result))
        dialog = Adw.AlertDialog(heading='Local backend removed' if result.get('ok') else 'Backend cleanup incomplete', body=body)
        dialog.add_response('close', 'Close dashboard')
        dialog.set_default_response('close')
        dialog.set_close_response('close')
        def close(*_):
            self.removing_backend = False
            application = self.get_application()
            self.close()
            application.quit()
        dialog.connect('response', close)
        dialog.present(self)
        return GLib.SOURCE_REMOVE

    def control(self, action, settings=None):
        message = {'op': 'control', 'action': action}
        if settings is not None:
            message['settings'] = settings
        self.action(action.replace('_', ' ').capitalize(), lambda: client.request(message, timeout=0.2))

    def apply_settings(self):
        settings = {key: choices[dropdown.get_selected()] for key, (dropdown, choices) in self.setting_choices.items()}
        self.control('set_settings', settings)

    def confirm_clear(self):
        dialog = Adw.AlertDialog(heading='Clear all recorded usage history?', body='Deletes daily app, command, and service usage, undated imported counts, source availability/coverage, and first/last dated observation evidence. Inventory, settings, and the completed-history-import marker are retained. Previously imported Bash history will not be reread. The daemon pauses the writer during the transaction and resumes with a new observation interval only if collection was enabled.')
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('clear', 'Clear usage history')
        dialog.set_response_appearance('clear', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('cancel')
        dialog.set_close_response('cancel')
        dialog.connect('response', lambda _dialog, response: self.control('clear_history') if response == 'clear' else None)
        dialog.present(self)

    def choose_history(self):
        chooser = Gtk.FileDialog(title='Choose Bash history source')
        def selected(dialog, result):
            try:
                file = dialog.open_finish(result)
                path = file.get_path()
                if path:
                    self.history_file.set_text(path)
            except GLib.Error:
                pass  # Cancel does not change the selected source.
        chooser.open(self, None, selected)

    def retry_history(self):
        from .cli import import_history
        selected = self.history_file.get_text().strip() or (self.preview or {}).get('history_file')
        self.action('One-time history import', lambda: import_history(selected or None))

    @staticmethod
    def preview_text(preview):
        return ('Exact managed paths:\n' + '\n'.join(str(p) for p in preview['paths'])
                + '\n\nCollection scopes:\n' + '\n'.join(preview['scopes'])
                + '\n\nBash history source: ' + preview['history_file']
                + '\n\nPrivacy limits: ' + preview['privacy']
                + '\n\nBar placement: ' + preview['bar_placement'])

    def preview_setup(self):
        from . import setup
        selected = self.history_file.get_text().strip() or None
        self.action('Read-only setup preview', lambda: setup.preview(history_file=selected), self._preview_ready)

    def _preview_ready(self, value, error):
        if not error and value:
            self.preview = value
            self.setup_summary.set_text(self.preview_text(value))
            self.dialog('Local setup preview · no changes yet', self._preview_content(value))

    def _preview_content(self, preview):
        content = box()
        content.append(label(self.preview_text(preview)))
        return content

    def confirm_setup(self):
        # Rebuild for the currently edited source before asking consent. Merely
        # opening this dialog cannot create files or start any collection.
        from . import setup
        selected = self.history_file.get_text().strip() or None
        def ready(value, error):
            if error:
                self.operation_status.set_text('Setup preview failed: ' + error + '. No setup was started.')
                return
            self.preview = value
            self.setup_summary.set_text(self.preview_text(value))
            dialog = Adw.AlertDialog(heading='Enable local tracking?', body=self.preview_text(value) + '\n\nThis action starts the user service paused, performs the one-time import (or reports its failure), adds the managed Bash source block, preserves existing widget placement or adds Usage to the right bar section if absent, then resumes collection. Open or source a new shell to load the hook. Only proceed if you consent to these exact local changes.')
            dialog.add_response('cancel', 'Cancel')
            dialog.add_response('enable', 'Enable local tracking')
            dialog.set_default_response('cancel')
            dialog.set_close_response('cancel')
            def response(_dialog, answer):
                if answer == 'enable':
                    self.action('Enable local tracking', lambda: setup.enable_tracking(history_file=value['history_file']), self._setup_done)
            dialog.connect('response', response)
            dialog.present(self)
        self.submit(lambda: setup.preview(history_file=selected), ready)

    def _setup_done(self, value, error):
        if error:
            self.setup_result.set_text('Setup failed: ' + error + '. Existing stages were not assumed successful; retry setup.')
        else:
            self.setup_result.set_text(('Setup complete' if value.get('ok') else 'Setup incomplete — requested sources are not all available; retry setup') + '\n' + text(value))


class UsageApplication(Adw.Application):
    def __init__(self):
        super().__init__(application_id='org.omarchy.OmaUsage', flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        window = self.get_active_window()
        if window is None:
            window = UsageWindow(self)
        window.present()


def main(argv=None):
    return UsageApplication().run(argv)
