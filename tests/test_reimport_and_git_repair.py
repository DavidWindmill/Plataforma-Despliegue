"""Regresiones: restaurar un checkout roto y reimportar una aplicación borrada."""
import subprocess
from pathlib import Path
import pytest

from control_plane.repositories import GitRepositories
from control_plane.engine import Platform
from control_plane.importer import import_application


def git(*args, cwd=None):
    p = subprocess.run(['git', *args], cwd=cwd, text=True, capture_output=True)
    assert p.returncode == 0, p.stderr


def make_git_repo(tmp_path, with_dockerfile=True):
    repo = tmp_path / 'origin'
    repo.mkdir()
    git('init', '-b', 'main', cwd=repo)
    git('config', 'user.email', 'test@example.com', cwd=repo)
    git('config', 'user.name', 'Test', cwd=repo)
    (repo / 'README.md').write_text('Prueba de clonación\n')
    if with_dockerfile:
        (repo / 'Dockerfile').write_text('FROM scratch\n')
    git('add', '.', cwd=repo)
    git('commit', '-m', 'initial', cwd=repo)
    return {'git_url': repo.as_uri(), 'ref': 'main', 'source': '.'}


def test_reclona_cuando_falta_dockerfile(tmp_path):
    spec = make_git_repo(tmp_path)
    downloader = GitRepositories(tmp_path / 'data')
    clone = downloader.clone('app-a', 'search-binarysearch-1-0', spec)
    assert (clone / 'Dockerfile').is_file()
    # Reproduce el Error.zip: .git existe pero el archivo de trabajo falta.
    (clone / 'Dockerfile').unlink()
    repaired = downloader.clone('app-a', 'search-binarysearch-1-0', spec)
    assert repaired == clone
    assert (repaired / 'Dockerfile').is_file()
    assert not repaired.with_name(repaired.name + '.downloading').exists()


def test_clonado_invalido_no_se_acepta(tmp_path):
    spec = make_git_repo(tmp_path, with_dockerfile=False)
    downloader = GitRepositories(tmp_path / 'data')
    with pytest.raises(RuntimeError, match='no contiene Dockerfile'):
        downloader.clone('app-a', 'search-binarysearch-1-0', spec)
    assert not (tmp_path / 'data' / 'checkouts' / 'app-a' / 'search-binarysearch-1-0').exists()


def test_borrar_e_importar_no_recupera_eventos_antiguos(tmp_path):
    platform_root = tmp_path / 'platform'
    (platform_root / 'configs').mkdir(parents=True)
    (platform_root / 'configs' / 'apps.json').write_text('{"apps":{}}')
    platform = Platform(platform_root, tmp_path / 'state')
    example = Path(__file__).resolve().parents[1] / 'examples' / 'app-a.zip'
    import_application(platform, example)
    platform.store.event('app-a', 'deployment_failed', error='antiguo')
    assert platform.details('app-a')['events']
    platform.remove_app('app-a')
    import_application(platform, example)
    assert platform.details('app-a')['events'] == []
    assert platform.details('app-a')['status'] == 'registered'
