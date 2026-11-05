"""Backend exclusivo Docker: comandos reales de compilación y ejecución."""
from __future__ import annotations
from pathlib import Path
import re
import subprocess
import httpx


def safe_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,55}', value):
        raise ValueError('IDs deben ser alfanuméricos, con guion o subrayado, y comenzar con una letra')
    return value.lower().replace('_', '-')


class DockerBackend:
    def __init__(self):
        self.built = set()

    def cmd(self, *args, capture=True):
        try:
            proc = subprocess.run(['docker', *map(str, args)], text=True, capture_output=capture, timeout=480)
        except FileNotFoundError as exc:
            raise RuntimeError('Docker no está instalado / docker.exe no está en PATH') from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError('Tiempo de espera agotado al llamar a Docker') from exc
        if proc.returncode:
            raise RuntimeError(f'docker {args[0]}: {proc.stderr.strip() or proc.stdout.strip()}')
        return proc.stdout.strip() if capture else ''

    def network(self, app):
        return f'dp-{safe_name(app)}'

    def cname(self, app, sid):
        return f'dp-{safe_name(app)}-{safe_name(sid)}'

    def ensure_network(self, app):
        name = self.network(app)
        try:
            self.cmd('network', 'inspect', name)
        except RuntimeError:
            self.cmd('network', 'create', '--label', 'deployment.platform.managed=true', name)

    def _container_info(self, name):
        try:
            return self.cmd('inspect', '--type', 'container', '--format', '{{.State.Running}}|{{index .Config.Labels "deployment.platform.managed"}}', name)
        except RuntimeError:
            return ''

    def discover(self, app, sid):
        return self._container_info(self.cname(app,sid)) == 'true|true'

    def start(self, app, service, package_path, route_aliases=None):
        self.ensure_network(app)
        definition = service.definition
        src = Path(package_path).resolve()
        if not src.is_dir() or not (src / 'Dockerfile').exists():
            raise ValueError(f'{service.id}: origen sin Dockerfile: {src}')
        tag = f'dp-{safe_name(app)}-{safe_name(definition["repository"])}:latest'
        if tag not in self.built:
            self.cmd('build', '--label', 'deployment.platform.managed=true', '-t', tag, str(src))
            self.built.add(tag)
        name = self.cname(app, service.id)
        # Una instancia antigua parada puede seguir existiendo; eliminarla antes de crear.
        if self._container_info(name):
            raise RuntimeError(f'{name} ya existe; detén el despliegue previo antes de recrearlo')
        port = int(definition.get('port', 8080))
        if not 1 <= port <= 65535:
            raise ValueError(f'{service.id}: puerto OAS no válido')
        command = ['run', '-d', '--name', name, '--network', self.network(app), '--label', 'deployment.platform.managed=true', '--label', f'deployment.platform.app={app}', '--label', f'deployment.platform.service={service.id}', '--label', f'deployment.platform.machine={service.machine}', '-p', f'127.0.0.1::{port}']
        for alias in set([service.id] + list(route_aliases or [])):
            command += ['--network-alias', safe_name(alias)]
        environment=dict(definition.get('environment',{}))
        # Resolver enlaces declarativos: los consumidores guardan URL de grupo,
        # nunca un ID concreto ni una ruta específica de App A o B.
        for env_name, route in definition.get('bindings',{}).items():
            environment[env_name]=f'http://{safe_name(route)}:{definition.get("binding_ports", {}).get(env_name, 8080)}'
        # Solo se envian las variables de funcionamiento declaradas en OAS.
        # Los microservicios generan sus fallos con sus propias constantes.
        for k, v in environment.items():
            if k == 'QOS_LIMITS_JSON' or k.startswith('QOS_FAULT_'):
                raise ValueError('Los limites y fallos QoS son internos al microservicio')
            if not re.fullmatch(r'[A-Z_][A-Z0-9_]*', k):
                raise ValueError(f'Variable de entorno inválida: {k}')
            command += ['-e', f'{k}={v}']
        command += ['-e', f'APPLICATION_ID={app}', tag]
        # Si cualquier paso falla, retiramos el contenedor parcialmente creado.
        # Nunca consideramos un despliegue completo antes de confirmar /health
        # y /metrics del contrato OAS en el contenedor REAL.
        import time
        try:
            cid = self.cmd(*command)
            port_data = self.cmd('port', name, f'{port}/tcp')
            if not port_data.strip():
                raise RuntimeError(f'{name}: Docker no ha publicado el puerto')
            bind = port_data.splitlines()[0].rsplit(':', 1)[-1]
            endpoint = f'http://127.0.0.1:{int(bind)}'
            deadline = time.monotonic() + 75
            while time.monotonic() < deadline:
                if not self.discover(app, service.id):
                    raise RuntimeError(f'{name} terminó durante el arranque; consulta docker logs {name}')
                try:
                    with httpx.Client(timeout=2, trust_env=False) as client:
                        health = client.get(endpoint + definition['monitor']['health_path'])
                        metrics = client.get(endpoint + definition['monitor']['metrics_path'])
                    values = metrics.json() if metrics.is_success else None
                    if health.is_success and isinstance(values, dict) and values.get('status') != 'failed':
                        return {'container_id': cid[:12], 'endpoint': endpoint, 'status': 'running'}
                except (httpx.HTTPError, ValueError):
                    pass
                time.sleep(1)
            raise RuntimeError(f'{name}: /health o /metrics OAS no respondió correctamente en 75 s')
        except Exception:
            # Este método también elimina el contenedor si "docker run" devolvió
            # un error después de haberlo creado (solo con etiqueta propia).
            self.stop(app, service.id)
            raise

    def stop(self, app, sid):
        name = self.cname(app, sid)
        info=self._container_info(name)
        if info:
            if not info.endswith('|true'):
                raise RuntimeError(f'No se borrará el contenedor no gestionado: {name}')
            self.cmd('rm', '-f', name)

    def probe(self, app, service, runtime):
        if not self.discover(app, service.id):
            return {'healthy': False, 'metrics': {}, 'error': 'container missing'}
        endpoint = runtime['endpoint']
        monitor = service.definition.get('monitor', {})
        try:
            with httpx.Client(timeout=3, trust_env=False) as client:
                health = client.get(endpoint + monitor['health_path'])
                metrics = client.get(endpoint + monitor['metrics_path'])
            data = metrics.json() if metrics.is_success else {}
            return {'healthy': health.is_success and metrics.is_success and data.get('status') != 'failed', 'metrics': data, 'error': None if health.is_success and metrics.is_success else 'HTTP health/metrics error'}
        except (httpx.HTTPError, ValueError) as exc:
            return {'healthy': False, 'metrics': {}, 'error': str(exc)}

    def stop_app(self, app):
        ids = self.cmd('ps', '-a', '--filter', f'label=deployment.platform.app={app}', '--format', '{{.Names}}').splitlines()
        for name in ids:
            self.cmd('rm', '-f', name)
        net = self.network(app)
        try:
            managed=self.cmd('network', 'inspect', '--format', '{{index .Labels "deployment.platform.managed"}}', net)
        except RuntimeError:
            managed=''
        if managed=='true':
            self.cmd('network', 'rm', net)
