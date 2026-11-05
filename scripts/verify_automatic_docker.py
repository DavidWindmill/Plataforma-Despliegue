"""Observa un ciclo de adaptacion automatica con Docker real y las QoS del REQ."""
import argparse
import sys
import time
import httpx

parser = argparse.ArgumentParser()
parser.add_argument('--url', default='http://127.0.0.1:8000')
parser.add_argument('--timeout', type=int, default=360)
args = parser.parse_args()
client = httpx.Client(base_url=args.url, timeout=15, trust_env=False)


def get(path):
    """Consulta los eventos del estado de un despliegue."""
    response=client.get(path)
    response.raise_for_status()
    return response.json()


try:
    baseline = {x['id']: x['version'] for x in get('/api/apps') if x['status']=='running'}
    if not baseline:
        raise RuntimeError('Despliega una aplicacion primero')
    deadline=time.monotonic()+args.timeout
    while time.monotonic()<deadline:
        for name, version in baseline.items():
            record=get('/api/apps/'+name)
            types={e['type'] for e in record['events']}
            if record['version']>version and {'qos_violated','planner_plan_generated','adaptation_completed'}<=types:
                print(f'[OK] Adaptacion autonoma: {name}, version {record["version"]}')
                print('Planes generados en data/generated_plans/',name)
                sys.exit(0)
        time.sleep(3)
    raise TimeoutError('No se observo una adaptacion automatica durante este periodo')
except Exception as exc:
    print(f'[ERROR] {exc}',file=sys.stderr)
    sys.exit(1)
finally:
    client.close()
