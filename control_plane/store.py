"""Almacén local y atómico de metadatos; NO almacena datos funcionales de las apps."""
import json
import os
from pathlib import Path
from threading import RLock
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.file = self.root / 'state.json'
        self.lock = RLock()
        self.data = json.loads(self.file.read_text(encoding='utf-8')) if self.file.exists() else {'apps': {}, 'events': []}

    def save(self):
        with self.lock:
            path = self.file.with_suffix('.tmp')
            path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding='utf-8')
            os.replace(path, self.file)

    def event(self, app, type_, **details):
        with self.lock:
            self.data['events'].append({'time': now(), 'app': app, 'type': type_, **details})
            self.data['events'] = self.data['events'][-1500:]
            self.save()

    def events(self, app, limit=80):
        with self.lock:
            return [e for e in self.data['events'] if e['app'] == app][-limit:][::-1]
