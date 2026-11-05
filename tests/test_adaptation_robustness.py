"""Casos críticos del ciclo de redespliegue, sin simular el éxito solo en la web."""
from pathlib import Path
import json
import runpy
import pytest
from fastapi.testclient import TestClient

from control_plane.engine import Platform
from control_plane.importer import import_application
from control_plane.monitor import MonitorManager
from control_plane.repositories import GitRepositories
from test_workflow import DockerFake

ROOT = Path(__file__).resolve().parents[1]
REPOS = ROOT.parent


class TrackingDocker(DockerFake):
    def __init__(self):
        super().__init__()
        self.failures = set()
        self.fail_new = set()
        self.actions = []

    def start(self, app, service, source, aliases):
        self.actions.append(('start', service.id, service.definition['alternative']))
        if (service.id, service.definition['alternative']) in self.fail_new:
            raise RuntimeError(f'Falló el nuevo contenedor {service.id}')
        return super().start(app, service, source, aliases)

    def stop(self, app, sid):
        self.actions.append(('stop', sid))
        super().stop(app, sid)

    def probe(self, app, service, runtime):
        metrics = {'execution_time':5, 'reliability':0.02, 'availability':24}
        if service.id in self.failures:
            metrics['execution_time'] = 9999
        return {'healthy':True, 'metrics':metrics, 'error':None}


def platform_with_docker(tmp_path, monkeypatch, apps=('app-a',)):
    root = tmp_path / 'platform'
    (root / 'configs').mkdir(parents=True)
    (root / 'configs' / 'apps.json').write_text('{"apps":{}}')

    def clone(self, app, name, spec):
        folder = self.data_dir / 'checkouts' / app / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'Dockerfile').write_text('FROM scratch\n')
        return folder
    monkeypatch.setattr(GitRepositories, 'clone', clone)
    backend = TrackingDocker()
    platform = Platform(root, tmp_path / 'store', backend)
    for name in apps:
        import_application(platform, ROOT / 'examples' / f'{name}.zip')
    return platform, backend


def test_reemplazo_real_y_varios_fallos_a_la_vez(tmp_path, monkeypatch):
    platform, docker = platform_with_docker(tmp_path, monkeypatch)
    first = platform.apply('app-a')
    before = {sid:s['runtime']['container_id'] for sid,s in first['active'].items()}
    docker.failures = {'sensor00', 'sensor10'}

    MonitorManager(platform).check_all()
    after = platform.details('app-a')
    assert after['status'] == 'running'
    assert after['version'] == first['version'] + 1  # un solo redespliegue
    plan = {x['id']:x['alternative'] for x in after['current_plan']['services']}
    assert plan['sensor00'] != 'sensor-1'
    assert plan['sensor10'] != 'sensor-2'
    assert after['active']['sensor00']['runtime']['container_id'] != before['sensor00']
    assert after['active']['sensor10']['runtime']['container_id'] != before['sensor10']
    assert after['active']['search00']['runtime']['container_id'] == before['search00']
    assert any(x[:2] == ('stop','sensor00') for x in docker.actions)
    assert any(x[:2] == ('stop','sensor10') for x in docker.actions)
    assert docker.running['app-a','sensor00']['container_id'] == after['active']['sensor00']['runtime']['container_id']
    event = next(x for x in after['events'] if x['type']=='adaptation_completed')
    assert len(event['changes']) == 2
    assert event['removed_containers']['sensor00']['container_id'] == before['sensor00']
    assert event['created_containers']['sensor00']['container_id'] == after['active']['sensor00']['runtime']['container_id']


def test_fallo_del_nuevo_contenedor_restaura_solo_los_afectados(tmp_path, monkeypatch):
    platform, docker = platform_with_docker(tmp_path, monkeypatch)
    first = platform.apply('app-a')
    before = {sid:s['runtime']['container_id'] for sid,s in first['active'].items()}
    docker.fail_new.add(('search00','groveralg-1'))
    docker.failures.add('search00')
    MonitorManager(platform).check_all()
    after = platform.details('app-a')

    assert after['status'] == 'running'
    assert after['version'] == first['version']
    assert after['current_plan'] == first['current_plan']
    assert after['active']['search00']['service']['definition']['alternative'] == 'binarysearch-1'
    assert after['active']['search00']['runtime']['container_id'] != before['search00']
    assert 'search00-qpu' not in after['active']
    assert ('app-a','search00-qpu') not in docker.running
    assert all(after['active'][sid]['runtime']['container_id'] == before[sid]
               for sid in ('sensor00','sensor10','sensor20','aggregator00','resultprocessing10'))
    assert any(x['type']=='deployment_failed' for x in after['events'])
    assert any(x['type']=='deployment_rolled_back' for x in after['events'])
    assert not any(x['type']=='adaptation_completed' for x in after['events'])
    attempts = len([x for x in docker.actions if x[:2] == ('start','search00')])
    MonitorManager(platform).check_all()  # no reintento cada cuatro segundos
    assert len([x for x in docker.actions if x[:2] == ('start','search00')]) == attempts


def test_sin_alternativa_se_registra_y_otros_servicios_si_cambian(tmp_path, monkeypatch):
    platform, docker = platform_with_docker(tmp_path, monkeypatch)
    original = platform.apply('app-a')
    docker.failures = {'aggregator00','sensor00'}  # aggregator solo tiene una alternativa
    MonitorManager(platform).check_all()
    final = platform.details('app-a')
    assert final['version'] == original['version'] + 1
    assert final['active']['sensor00']['service']['definition']['alternative'] != 'sensor-1'
    assert final['active']['aggregator00']['runtime']['container_id'] == original['active']['aggregator00']['runtime']['container_id']
    assert any(x['type']=='planner_no_alternative' and x['service']=='aggregator00' for x in final['events'])


def test_desplegar_todas_y_nombre_editable_persiste(tmp_path, monkeypatch):
    platform, backend = platform_with_docker(tmp_path, monkeypatch, ('app-a','app-b'))
    res = platform.deploy_all()
    assert len(res['results']) == 2
    assert all(x['status']=='deployed' for x in res['results'])
    assert all(x['status']=='already_running' for x in platform.deploy_all()['results'])
    platform.rename_app('app-a','Incubadora principal')
    assert platform.details('app-a')['display_name'] == 'Incubadora principal'
    restarted = Platform(platform.root, platform.store.root, backend)
    assert restarted.details('app-a')['display_name'] == 'Incubadora principal'
    assert restarted.list_apps()[0]['display_name'] == 'Incubadora principal'
    with pytest.raises(ValueError): platform.rename_app('app-a',' ') 


def test_api_desplegar_todas_renombrar_y_web(tmp_path, monkeypatch):
    from control_plane import api
    p, docker = platform_with_docker(tmp_path, monkeypatch, ('app-a','app-b'))
    monkeypatch.setattr(api,'platform',p)
    monkeypatch.setattr(api,'monitors',MonitorManager(p))
    with TestClient(api.app) as client:
        rename = client.patch('/api/apps/app-a/name',json={'display_name':'App de pruebas'})
        assert rename.status_code == 200
        assert rename.json()['display_name'] == 'App de pruebas'
        result = client.post('/api/apps/deploy-all').json()
        assert len(result['results']) == 2
        assert all(x['status']=='deployed' for x in result['results'])
    page = (ROOT/'web/index.html').read_text(encoding='utf8')
    assert 'Desplegar todas' in page and 'Renombrar' in page
    assert 'spec.alternative' in page and 'container_id' in page


def test_fallos_menos_frecuentes_sin_jitter_en_metricas_de_req(monkeypatch):
    monkeypatch.setenv('SERVICE_VARIANT','sensor-2')
    for file in REPOS.glob('app-*/**/qos_faults.py'):
        data = runpy.run_path(str(file))
        fault = data['FaultProfile']()
        assert fault.low == 240 and fault.high == 480
        assert fault.probability == 0.06
    sensor = runpy.run_path(str(REPOS/'app-a-sensor/sensor_service/qos_faults.py'))
    fault = sensor['FaultProfile']()
    for _ in range(100):
        metrics = fault.decorate({'execution_time':10,'availability':24,'reliability':0.1})
        assert metrics['execution_time'] == 10   # no falso positivo por jitter
        assert metrics['availability'] == 24


def test_error_in_recovery_marks_app_as_error_and_stops_monitoring(tmp_path, monkeypatch):
    platform, docker = platform_with_docker(tmp_path, monkeypatch)
    platform.apply('app-a')
    docker.fail_new.update({('search00','groveralg-1'), ('search00','binarysearch-1')})
    docker.failures.add('search00')
    MonitorManager(platform).check_all()
    final = platform.details('app-a')
    assert final['status'] == 'error'
    assert any(x['type']=='rollback_failed' for x in final['events'])
    num_actions = len(docker.actions)
    MonitorManager(platform).check_all()
    assert len(docker.actions) == num_actions


def test_deploy_all_does_not_abort_after_error(tmp_path, monkeypatch):
    p, backend = platform_with_docker(tmp_path, monkeypatch, ('app-a','app-b'))
    backend.fail_new.add(('sensor00','sensor-1'))
    result = p.deploy_all()['results']
    assert result[0]['status'] == 'error'
    assert result[1]['status'] == 'deployed'
    assert p.details('app-b')['status'] == 'running'
    assert p.details('app-a')['status'] == 'registered'


def test_comandos_docker_de_sustitucion_no_solo_estado_web(tmp_path, monkeypatch):
    """Se verifica que DockerBackend recibe rm + run con IDs físicos distintos."""
    from control_plane.backends import DockerBackend
    import control_plane.backends as backends
    class SimulatedCLI(DockerBackend):
        def __init__(self):
            super().__init__()
            self.containers = {}
            self.calls = []
            self.counter = 0
        def ensure_network(self, app): pass
        def cmd(self, *args, **kw):
            self.calls.append(args)
            kind = args[0]
            if kind == 'build': return ''
            if kind == 'run':
                name = args[args.index('--name')+1]
                assert name not in self.containers
                self.counter += 1
                self.containers[name] = ('%012x' % self.counter) + 'a'*52
                return self.containers[name]
            if kind == 'port': return '127.0.0.1:23456'
            if kind == 'inspect':
                name = args[-1]
                if name not in self.containers: raise RuntimeError('Not found')
                return 'true|true'
            if kind == 'rm':
                self.containers.pop(args[-1],None)
                return ''
            return ''
    class FakeHTTPResponse:
        is_success = True
        def json(self):return {'status':'ok','execution_time':5}
    class FakeHTTP:
        def __init__(self, **kw):pass
        def __enter__(self):return self
        def __exit__(self, *args):pass
        def get(self, url):return FakeHTTPResponse()
    monkeypatch.setattr(backends.httpx,'Client', FakeHTTP)
    root=tmp_path/'platform'
    (root/'configs').mkdir(parents=True)
    (root/'configs/apps.json').write_text('{"apps": {}}')
    def clone(self, app, name, spec):
        folder=self.data_dir/'checkouts'/app/name
        folder.mkdir(parents=True,exist_ok=True)
        (folder/'Dockerfile').write_text('FROM scratch\n')
        return folder
    monkeypatch.setattr(GitRepositories,'clone',clone)
    docker=SimulatedCLI()
    platform=Platform(root,tmp_path/'store',docker)
    import_application(platform,ROOT/'examples/app-a.zip')
    first=platform.apply('app-a')
    original=first['active']['search00']['runtime']['container_id']
    result=platform.on_violation('app-a','search00',['execution_time: incumplimiento'],original)
    assert result['current_plan']['services'][4]['alternative']=='groveralg-1'
    new=result['active']['search00']['runtime']['container_id']
    assert original!=new
    name='dp-app-a-search00'
    assert ('rm','-f',name) in docker.calls
    assert name in docker.containers
    assert 'dp-app-a-search00-qpu' in docker.containers
    assert any(x[0]=='run' and x[x.index('--name')+1]==name for x in docker.calls)


def test_replan_manual_deja_evidencia_fisica(tmp_path, monkeypatch):
    platform, docker = platform_with_docker(tmp_path, monkeypatch)
    first = platform.apply('app-a')
    previous = first['active']['search00']['runtime']['container_id']
    from copy import deepcopy
    requested = deepcopy(first['current_plan'])
    for x in requested['services']:
        if x['id'] == 'search00':
            x['alternative'] = 'groveralg-1'
    final = platform.replan('app-a', requested)
    assert final['active']['search00']['runtime']['container_id'] != previous
    assert next(x for x in final['events'] if x['type']=='adaptation_completed')['origin']=='manual'
