"""Pruebas: los ZIP y los planes no necesitan codigo local ni Git al importar."""
from pathlib import Path
from zipfile import ZipFile
import json
import pytest
from control_plane.engine import Platform
from control_plane.importer import read_manifest, import_application
from control_plane.repositories import GitRepositories
from control_plane.specifications import normalize
from control_plane.plan import parse_plan, compare

EXAMPLES = Path(__file__).resolve().parents[1] / 'examples'


def create_platform(tmp_path):
    root = tmp_path / 'platform'
    (root / 'configs').mkdir(parents=True)
    (root / 'configs/apps.json').write_text('{"apps": {}}')
    return Platform(root, tmp_path / 'state')


def test_catologo_vacio_e_importacion_sin_git(tmp_path, monkeypatch):
    p = create_platform(tmp_path)
    def fail(*args):
        raise AssertionError('Git no debe utilizarse durante la importacion')
    monkeypatch.setattr(GitRepositories, 'clone', fail)
    assert p.list_apps() == []
    for name in ('app-a', 'app-b'):
        info = import_application(p, EXAMPLES / f'{name}.zip')
        assert info['id'] == name and info['status'] == 'registered'
        assert (p.root / 'imported' / name / 'OAS').is_dir()
        assert (p.root / 'imported' / name / 'REQ').is_dir()
        assert len(p.analyze(name)) > 0
        assert not (p.store.root / 'checkouts' / name).exists()
    p.remove_app('app-a')
    assert [r['id'] for r in p.list_apps()] == ['app-b']


def test_req_y_oas_fuentes_de_verdad():
    qos = normalize(EXAMPLES / 'app-a')
    plan = json.loads((EXAMPLES / 'app-a' / 'app.json').read_text())
    assert list(plan) == ['services']
    assert all(set(s) == {'id', 'alternative'} for s in plan['services'])
    services = parse_plan(plan, qos)
    m = services['search00'].definition['monitor']
    assert m['metrics_path'] == '/metrics'
    assert m['health_path'] == '/health'
    assert m['limits']['execution_time'] == {'max': 1547}
    assert qos['repositories']['search-binarysearch-1-0']['git_url'].endswith('app-a-classical-detector.git')
    assert qos['repositories']['search-groveralg-1-1']['git_url'].endswith('app-a-quantum-detector.git')
    assert 'binarysearch-1' in qos['categories']['search']['req']['alternatives']
    assert 'groveralg-1' in qos['categories']['search']['req']['alternatives']


def test_plan_cuantico_necesita_dos_contenedores():
    qos = normalize(EXAMPLES / 'app-a')
    initial = json.loads((EXAMPLES / 'app-a' / 'app.json').read_text())
    hybrid = json.loads((EXAMPLES / 'app-a-hybrid-plan.json').read_text())
    old, new = parse_plan(initial, qos), parse_plan(hybrid, qos)
    assert 'search00-qpu' not in old
    assert new['search00'].definition['bindings']['QPU_URL'] == 'search00-qpu'
    assert 'search00-qpu' in new
    diff = compare(old, new)
    assert {'action':'replace','service':'search00','reason':'Cambio de alternativa o configuracion OAS/REQ'} in diff
    assert any(op['service'] == 'search00-qpu' and op['action'] == 'create' for op in diff)
    assert sum(op['action'] in ('replace','create') for op in diff) == 2


def test_zip_requiere_oas_req_y_json(tmp_path):
    path = tmp_path / 'broken.zip'
    with ZipFile(path,'w') as z: z.writestr('app.json', '{"services": []}')
    with ZipFile(path) as z, pytest.raises(ValueError, match='OAS/'):
        read_manifest(z)


def test_zip_no_acepta_zip_slip(tmp_path):
    path = tmp_path / 'unsafe.zip'
    with ZipFile(path,'w') as z: z.writestr('../malicious.txt', 'bad')
    with ZipFile(path) as z, pytest.raises(ValueError, match='peligrosas'):
        read_manifest(z)


def test_sin_catalog_y_sin_codigo_local():
    for name in ('app-a','app-b'):
        with ZipFile(EXAMPLES / f'{name}.zip') as z:
            files = set(z.namelist())
            assert 'app.json' in files
            assert not any('catalog' in n or 'platform.json' in n or 'plans/' in n for n in files)
            assert not any('local_path' in z.read(n).decode() for n in files)


def test_plan_invalido_al_importar(tmp_path):
    p = create_platform(tmp_path)
    archive = tmp_path / 'bad-app.zip'
    with ZipFile(archive, 'w') as z:
        with ZipFile(EXAMPLES / 'app-a.zip') as original:
            for name in original.namelist():
                z.writestr(name, '{"services":[{"id":"sensor00","alternative":"unknown"}]}' if name=='app.json' else original.read(name))
    with pytest.raises(ValueError, match='alternativa no definida'):
        import_application(p, archive)
    assert not p.list_apps()


def test_botones_catalogo_y_api_desplegar_detener_borrar(tmp_path, monkeypatch):
    """Los botones de inicio actuan sobre la misma API que la vista de detalle."""
    from fastapi.testclient import TestClient
    from control_plane import api
    from control_plane.monitor import MonitorManager
    from control_plane.engine import Platform
    from control_plane.repositories import GitRepositories
    from test_workflow import DockerFake
    root = tmp_path / 'platform'
    (root / 'configs').mkdir(parents=True)
    (root / 'configs/apps.json').write_text('{"apps": {}}')
    platform = Platform(root, tmp_path / 'store', DockerFake())
    def fake_clone(self, app, name, spec):
        folder = self.data_dir / 'checkouts' / app / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'Dockerfile').write_text('FROM scratch\n')
        return folder
    monkeypatch.setattr(GitRepositories, 'clone', fake_clone)
    monkeypatch.setattr(api, 'platform', platform)
    monkeypatch.setattr(api, 'monitors', MonitorManager(platform))
    with TestClient(api.app) as client:
        page=client.get('/').text if (root / 'web/index.html').exists() else (EXAMPLES.parent / 'web/index.html').read_text()
        assert all(f"catalogAction('${{id}}','{operation}')" in page for operation in ('deploy','stop','delete'))
        with (EXAMPLES / 'app-a.zip').open('rb') as f:
            response = client.post('/api/apps/import', files={'file': ('app-a.zip',f,'application/zip')})
        assert response.status_code == 200
        assert client.post('/api/apps/app-a/deploy').status_code == 200
        assert client.get('/api/apps').json()[0]['status'] == 'running'
        assert client.post('/api/apps/app-a/stop').status_code == 200
        assert client.get('/api/apps').json()[0]['status'] == 'stopped'
        assert client.delete('/api/apps/app-a').status_code == 200
        assert client.get('/api/apps').json() == []
