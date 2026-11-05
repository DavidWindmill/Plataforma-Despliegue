"""Simula Docker y Git para verificar un ciclo completo sin conectarse a Internet."""
from pathlib import Path
from control_plane.engine import Platform
from control_plane.monitor import MonitorManager
from control_plane.importer import import_application
from control_plane.repositories import GitRepositories


class DockerFake:
    def __init__(self):
        self.seq = 0
        self.running = {}
        self.failed_service = None

    def start(self, app, service, source, aliases):
        assert (Path(source)/'Dockerfile').exists()
        self.seq += 1
        data = {'container_id':str(self.seq), 'endpoint':'http://fake', 'status':'running'}
        self.running[app, service.id] = data
        return data

    def stop(self, app, sid):
        self.running.pop((app, sid), None)

    def stop_app(self, app):
        for key in list(self.running):
            if key[0] == app: del self.running[key]

    def discover(self, app, sid):
        return (app,sid) in self.running

    def probe(self, app, service, runtime):
        metrics={'execution_time':5,'reliability':0.02,'availability':24}
        if service.id == self.failed_service:
            metrics['execution_time'] = 9999
        return {'healthy':True, 'metrics':metrics, 'error':None}


def test_full_cycle_lazily_downloads_git(tmp_path, monkeypatch):
    root=tmp_path/'platform'
    (root/'configs').mkdir(parents=True)
    (root/'configs/apps.json').write_text('{"apps": {}}')
    clones=[]
    def fake_clone(self, app, name, spec):
        clones.append(name)
        folder=self.data_dir/'checkouts'/app/name
        folder.mkdir(parents=True, exist_ok=True)
        (folder/'Dockerfile').write_text('FROM scratch\n')
        return folder
    monkeypatch.setattr(GitRepositories,'clone',fake_clone)
    backend=DockerFake()
    platform=Platform(root,tmp_path/'store',backend)
    z=Path(__file__).resolve().parents[1]/'examples/app-a.zip'
    imported=import_application(platform,z)
    assert imported['status']=='registered'
    assert not clones
    assert sum(op['action']=='create' for op in platform.analyze('app-a'))==6
    first=platform.apply('app-a')
    assert first['status']=='running' and len(first['active'])==6
    assert len(clones)==6
    assert not any('groveralg' in n for n in clones)
    ids={sid:info['runtime']['container_id'] for sid,info in first['active'].items()}
    # El monitor consulta el mismo endpoint OAS y los limites REQ.
    backend.failed_service='search00'
    MonitorManager(platform).check_all()
    after=platform.details('app-a')
    assert after['version']==first['version']+1
    assert len(after['active'])==7
    assert after['current_plan']['services'][4]['alternative']=='groveralg-1'
    assert 'search00-qpu' in after['active']
    assert len(clones)==8  # Solo gateway+QPU; no se descarga otra vez el resto.
    assert all(after['active'][sid]['runtime']['container_id']==ids[sid]
               for sid in ('sensor00','sensor10','sensor20','aggregator00','resultprocessing10'))
    assert any(e['type']=='adaptation_completed' for e in after['events'])
    assert any(e['type']=='planner_plan_generated' for e in after['events'])
    platform.stop('app-a')
    assert platform.details('app-a')['status']=='stopped'
    platform.remove_app('app-a')
    assert not platform.list_apps()


def test_http_import_analysis_and_delete(tmp_path, monkeypatch):
    """La web usa exactamente el mismo metodo de importacion sin clonar Git."""
    from fastapi.testclient import TestClient
    from control_plane import api
    root=tmp_path/'platform'
    (root/'configs').mkdir(parents=True)
    (root/'configs/apps.json').write_text('{"apps": {}}')
    backend=DockerFake()
    platform=Platform(root,tmp_path/'state',backend)
    monkeypatch.setattr(api,'platform',platform)
    monkeypatch.setattr(api,'monitors',MonitorManager(platform))
    with TestClient(api.app) as client:
        assert client.get('/api/apps').json()==[]
        path=Path(__file__).resolve().parents[1]/'examples/app-a.zip'
        with path.open('rb') as f:
            response=client.post('/api/apps/import',files={'file':('app-a.zip',f,'application/zip')})
        assert response.status_code==200, response.text
        assert response.json()['status']=='registered'
        assert len(client.get('/api/apps/app-a/analysis').json()['operations'])==6
        assert client.delete('/api/apps/app-a').status_code==200
        assert client.get('/api/apps').json()==[]
