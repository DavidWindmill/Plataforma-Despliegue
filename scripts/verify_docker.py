"""Comprueba el flujo real de importacion y despliegue (requiere Docker y GitHub).

Ejecutar con la web arrancada: python scripts/verify_docker.py --cleanup
"""
from pathlib import Path
import argparse
import sys
import time
import httpx

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--cleanup', action='store_true')
parser.add_argument('--url', default='http://127.0.0.1:8000')
args = parser.parse_args()
client = httpx.Client(base_url=args.url, timeout=480, trust_env=False)


def request(method, path):
    """Llama a la plataforma y muestra el error si falla una operacion."""
    response = client.request(method, path)
    response.raise_for_status()
    return response.json()


def add_example(name):
    """Sube un paquete de especificaciones. Todavia no clona ningun Git."""
    if name in {x['id'] for x in request('GET', '/api/apps')}:
        return
    path = root / 'examples' / f'{name}.zip'
    with path.open('rb') as file:
        response = client.post('/api/apps/import', files={'file': (path.name, file, 'application/zip')})
    response.raise_for_status()
    print('Importado:', name)


def main():
    """Verifica Docker, conexiones de App B, monitores y un replanificador manual."""
    if not request('GET', '/api/system')['docker_available']:
        raise RuntimeError('Docker no disponible: arranca Docker Desktop')
    for name, size in [('app-a', 6), ('app-b', 3)]:
        add_example(name)
        record = request('GET', f'/api/apps/{name}')
        if record['status'] != 'running':
            record = request('POST', f'/api/apps/{name}/deploy')
        assert len(record['active']) == size, record['active'].keys()
        print(f'{name}: {size} contenedores iniciados')
    original = request('GET', '/api/apps/app-a')
    before = {k: s['runtime']['container_id'] for k, s in original['active'].items()}
    newer = request('POST', '/api/apps/app-a/replan')
    assert newer['version'] > original['version']
    after = {k: s['runtime']['container_id'] for k, s in newer['active'].items()}
    assert before != after, 'No ha cambiado ningun contenedor'
    print('Replanificacion real App A comprobada; servicios vigentes:',list(after))
    # Los monitores pueden necesitar unos segundos tras crear cada contenedor.
    for attempt in range(15):
        record = request('GET', '/api/apps/app-a')
        if all(s['monitor']['status'] != 'pending' for s in record['active'].values()):
            break
        time.sleep(2)
    assert all(s['monitor']['status'] in ('healthy','violation') for s in record['active'].values())
    assert request('GET', '/api/apps/app-b')['status'] == 'running'
    print('Monitores activos, adaptacion funcional y App B independiente')


try:
    main()
except Exception as exc:
    print(f'ERROR: {exc}', file=sys.stderr)
    sys.exit(1)
finally:
    if args.cleanup:
        for name in ('app-a','app-b'):
            try:
                request('POST', f'/api/apps/{name}/stop')
            except Exception as exc:
                print(f'No se pudo detener {name}: {exc}',file=sys.stderr)
    client.close()
