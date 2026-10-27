"""Read-only software discovery. Filesystem paths exist only in this transient snapshot."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import unicodedata

from .store import stamp

CATEGORY_ORDER = ('Development','Office','AudioVideo','Graphics','Game','Network','System','Utility')
_cache = {}
_cache_lock = threading.Lock()


def safe_name(value):
    return (isinstance(value,str) and bool(value) and len(value.encode('utf-8')) <= 255
            and '/' not in value and not any(unicodedata.category(c).startswith('C') for c in value))


def _run(args, timeout=60):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=True,
                          env={**os.environ,'LC_ALL':'C','TZ':'UTC'}).stdout


def _metadata(text):
    result = {}
    current = None
    for line in text.splitlines():
        if not line.strip():
            current = None
        elif line[0].isspace() and current:
            result[current] += ' ' + line.strip()
        elif ':' in line:
            key, value = line.split(':',1)
            current = key.strip()
            result[current] = value.strip()
    return result


def _package_blocks(text):
    for block in text.strip().split('\n\n'):
        row = _metadata(block)
        name = row.get('Name')
        if not name or not safe_name(name):
            continue
        size = row.get('Installed Size','').split()
        try:
            amount = float(size[0]) * {'B':1,'KiB':1024,'MiB':1024**2,'GiB':1024**3}[size[1]]
        except (ValueError,IndexError,KeyError):
            amount = None
        try:
            installed = datetime.strptime(row.get('Install Date',''),'%a %d %b %Y %I:%M:%S %p %Z').replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            installed = None
        yield name, dict(version=row.get('Version'),installed_size_bytes=int(amount) if amount is not None else None,
                         install_update_at=installed,
                         required_by=[x for x in row.get('Required By','').split() if x != 'None'],
                         optional_for=[x for x in row.get('Optional For','').split() if x != 'None'])


def package_details(name):
    """Fresh dependency relationships, called only on a worker when opening details."""
    if not safe_name(name) or name.startswith('-'):
        raise ValueError('invalid_package')
    rows = dict(_package_blocks(_run(['pacman','-Qi','--',name])))
    if name not in rows:
        raise RuntimeError('package_metadata_unavailable')
    return rows[name]


def _owned_files():
    owners = {}
    proc = subprocess.Popen(['pacman','-Ql'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, env={**os.environ,'LC_ALL':'C'})
    timer = threading.Timer(60,proc.kill)
    timer.daemon = True
    timer.start()
    try:
        for line in proc.stdout:
            package, separator, path = line.rstrip('\n').partition(' ')
            if separator and not path.endswith('/'):
                owners[path] = package
        if proc.wait() != 0:
            raise RuntimeError('ownership_unavailable')
    finally:
        timer.cancel()
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return owners


def _owner(path, owners):
    if not path:
        return None
    return owners.get(str(path)) or owners.get(os.path.realpath(path))


def _commands(owners):
    commands = {}
    complete = True
    directories = list(dict.fromkeys(os.environ.get('PATH','').split(os.pathsep) + ['/usr/bin','/bin']))
    for directory in directories:
        if not directory:
            continue
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    name = entry.name
                    if name not in commands and safe_name(name) and entry.is_file() and os.access(entry.path,os.X_OK):
                        commands[name] = dict(key=name,label=name,owner=_owner(entry.path,owners))
        except FileNotFoundError:
            continue
        except OSError:
            complete = False
    return list(commands.values()), complete


def _apps(owners):
    import gi
    gi.require_version('GioUnix','2.0')
    from gi.repository import Gio, GioUnix
    result = []
    for info in Gio.AppInfo.get_all():
        if not isinstance(info,GioUnix.DesktopAppInfo) or not info.should_show():
            continue
        key = info.get_id()
        if not safe_name(key):
            continue
        categories = (info.get_categories() or '').split(';')
        executable = os.path.basename(info.get_executable() or '')
        executable_path = shutil.which(info.get_executable() or '')
        filename = info.get_filename()
        result.append(dict(key=key,label=info.get_display_name() or key,
                           category=next((x for x in CATEGORY_ORDER if x in categories),'Other'),
                           executable=executable if safe_name(executable) else None,
                           wmclass=info.get_startup_wm_class(),
                           owner=_owner(filename,owners) or _owner(executable_path,owners)))
    return result


def service_inventory(scope, owners):
    from gi.repository import Gio, GLib
    bus = Gio.bus_get_sync(Gio.BusType.SESSION if scope == 'user' else Gio.BusType.SYSTEM,None)
    def call(method, params, result_type):
        return bus.call_sync('org.freedesktop.systemd1','/org/freedesktop/systemd1','org.freedesktop.systemd1.Manager',method,params,GLib.VariantType.new(result_type),Gio.DBusCallFlags.NONE,5000,None).unpack()[0]
    files = call('ListUnitFiles',None,'(a(ss))')
    units = call('ListUnitsByPatterns',GLib.Variant('(asas)',([],['*.service'])),'(a(ssssssouso))')
    result = {}
    for filename, state in files:
        name = os.path.basename(filename)
        if name.endswith('.service') and safe_name(name):
            result[name] = dict(key=scope+':'+name,label=name,owner=_owner(filename,owners),
                                unit_file_state=state,active_state='inactive',sub_state='dead',template='@.service' in name)
    for unit in units:
        name, description, load, active, sub, following, object_path, job_id, job_type, job_path = unit
        if not safe_name(name):
            continue
        row = result.setdefault(name,dict(key=scope+':'+name,label=name,owner=None,
                              unit_file_state='unknown',template='@.service' in name))
        row.update(active_state=active,sub_state=sub)
        if row['owner'] is None:
            try:
                fragment = bus.call_sync('org.freedesktop.systemd1',object_path,'org.freedesktop.DBus.Properties','Get',GLib.Variant('(ss)',('org.freedesktop.systemd1.Unit','FragmentPath')),GLib.VariantType.new('(v)'),Gio.DBusCallFlags.NONE,1000,None).unpack()[0]
                row['owner'] = _owner(fragment,owners)
            except GLib.Error:
                pass
    return list(result.values())


def collect_inventory():
    """Bounded worker entry. Failed domains are None, never an empty inventory."""
    snapshot: dict = dict(packages=None,apps=None,commands=None,services_user=None,services_system=None,
                    ownership_complete=False,commands_complete=False,errors={})
    owners = {}
    try:
        versions = dict(line.split(' ',1) for line in _run(['pacman','-Q']).splitlines())
        explicit = set(_run(['pacman','-Qqe']).splitlines())
        signature = (tuple(sorted(versions.items())),tuple(sorted(explicit)))
        with _cache_lock:
            cached = _cache.get('packages') if _cache.get('signature') == signature else None
            if cached:
                packages, owners = cached
            else:
                packages = dict(_package_blocks(_run(['pacman','-Qi'])))
                if set(packages) != set(versions):
                    raise RuntimeError('package_metadata_incomplete')
                for name, row in packages.items():
                    row['explicit_install'] = name in explicit
                snapshot['packages'] = packages
                try:
                    owners = _owned_files()
                except (OSError, RuntimeError):
                    snapshot['errors']['ownership'] = 'ownership_inventory_unavailable'
                else:
                    snapshot['ownership_complete'] = True
                    _cache.update(signature=signature,packages=(packages,owners))
        snapshot['packages'] = packages
        if cached:
            snapshot['ownership_complete'] = True
    except (OSError,subprocess.SubprocessError,ValueError,RuntimeError):
        snapshot['errors']['packages'] = 'package_inventory_unavailable'
    try:
        snapshot['commands'],snapshot['commands_complete'] = _commands(owners)
    except OSError:
        snapshot['errors']['commands'] = 'command_inventory_unavailable'
    try:
        snapshot['apps'] = _apps(owners)
    except Exception:
        snapshot['errors']['apps'] = 'launcher_inventory_unavailable'
    for scope in ('user','system'):
        try:
            snapshot['services_'+scope] = service_inventory(scope,owners)
        except Exception:
            snapshot['errors']['services_'+scope] = 'service_inventory_unavailable'
    return snapshot


def apply_inventory(store, snapshot, now):
    """Main-writer entry. Only sanitized identities and selected metadata survive."""
    at = stamp(now)
    package_ids = {r['key']:r['id'] for r in store.conn.execute("SELECT id,key FROM entities WHERE kind='package'")}
    packages = snapshot['packages']
    if packages is not None:
        present = []
        for name, details in packages.items():
            entity_id = store.entity('package',name)
            package_ids[name] = entity_id
            _present(store,entity_id,at,label=name,owner=None,category='Other',explicit=details['explicit_install'])
            store.conn.execute('INSERT OR REPLACE INTO package_details VALUES(?,?,?,?,?,?)',(entity_id,details['version'],details['installed_size_bytes'],details['install_update_at'],json.dumps(details['required_by']),json.dumps(details['optional_for'])))
            present.append(entity_id)
        _removed(store,'package',present)
    for domain, kind in (('apps','app'),('commands','command'),('services_user','service'),('services_system','service')):
        rows = snapshot[domain]
        if rows is None:
            continue
        present = []
        for row in rows:
            if not safe_name(row['key'].split(':',1)[-1] if kind == 'service' else row['key']):
                continue
            entity_id = store.entity(kind,row['key'])
            # Never erase known ownership merely because the ownership domain failed.
            owner = package_ids.get(row.get('owner'))
            if not snapshot['ownership_complete']:
                owner = store.conn.execute('SELECT owner_package_id FROM entities WHERE id=?',(entity_id,)).fetchone()[0]
            label = row['label']
            if any(unicodedata.category(c).startswith('C') for c in label):
                label = row['key']
            if row.get('template'):
                label += ' (template)'
            _present(store,entity_id,at,label=label,owner=owner,
                     category='Template' if row.get('template') else row.get('category','Other'))
            if kind == 'service':
                store.conn.execute('INSERT OR REPLACE INTO service_details VALUES(?,?,?,?)',(entity_id,row['unit_file_state'],row['active_state'],row['sub_state']))
            present.append(entity_id)
        if domain != 'commands' or snapshot['commands_complete']:
            _removed(store,kind,present,scope=domain.removeprefix('services_') if kind == 'service' else None)
    for key in ('ownership_complete','commands_complete'):
        store.metadata('inventory_'+key,json.dumps(snapshot[key]))
    store.metadata('inventory_errors',json.dumps(snapshot['errors']))
    store.metadata('inventory_at',at)


def _present(store, entity_id, at, *, label, owner, category, explicit=False):
    store.conn.execute('UPDATE entities SET label=?,owner_package_id=?,category=?,explicit_install=?,first_inventory_at=coalesce(first_inventory_at,?),present_since=CASE WHEN installed=0 THEN ? ELSE present_since END,installed=1 WHERE id=?',(label,owner,category,int(explicit),at,at,entity_id))


def _removed(store, kind, present, scope=None):
    # Temporary IDs avoid SQLite variable limits on large installed command sets.
    store.conn.execute('CREATE TEMP TABLE IF NOT EXISTS current_inventory(id INTEGER PRIMARY KEY)')
    store.conn.execute('DELETE FROM current_inventory')
    store.conn.executemany('INSERT INTO current_inventory VALUES(?)',((x,) for x in present))
    sql = 'UPDATE entities SET installed=0 WHERE kind=? AND id NOT IN (SELECT id FROM current_inventory)'
    params = [kind]
    if scope:
        sql += ' AND key LIKE ?'
        params.append(scope+':%')
    store.conn.execute(sql,params)


def resolve_app(store, snapshot, app_id):
    rows = snapshot.get('apps') or []
    canonical = lambda value: value.removesuffix('.desktop')
    searches = [lambda row: canonical(row['key']) == canonical(app_id),
                lambda row: row.get('wmclass') == app_id,
                lambda row: row.get('executable') == app_id]
    for search in searches:
        matched = [row for row in rows if search(row)]
        if len(matched) == 1:
            return store.entity('app',matched[0]['key'])
        if len(matched) > 1:
            return store.entity('app','app-id:'+app_id)
    for field in ('key','wmclass','executable'):
        matched = [row for row in rows if row.get(field) and canonical(row[field]).casefold() == canonical(app_id).casefold()]
        if len(matched) == 1:
            return store.entity('app',matched[0]['key'])
        if len(matched) > 1:
            break
    return store.entity('app','app-id:'+app_id)
