"""Coordinador: importacion -> analisis -> despliegue -> monitor -> planificador.

Cada componente es un objeto Python normal; solo se usa HTTP contra los
microservicios desplegados y entre la web y FastAPI.
"""
from pathlib import Path
from datetime import datetime, timezone
from threading import RLock
import time
import json
import re
import shutil

from .backends import DockerBackend
from .plan import Service, compare, parse_plan
from .store import Store
from .planning import RandomPlanProvider
from .specifications import normalize
from .repositories import GitRepositories


class Platform:
    def __init__(self, root: Path, data_dir: Path | None = None, backend=None):
        """Abre el catalogo inicialmente vacio y conecta los objetos internos."""
        self.root = Path(root).resolve()
        self.store = Store(data_dir or self.root / 'data')
        self.backend = backend if backend is not None else DockerBackend()
        self.planner = RandomPlanProvider()
        self.git = GitRepositories(self.store.root)
        self.locks = {}
        self.retry_after = {}  # Evita repetir cada 4 s un redespliegue fallido.
        self.locks_lock = RLock()
        self.registry = json.loads((self.root / 'configs/apps.json').read_text(encoding='utf8'))['apps']
        self._load_apps()

    def _lock(self, app):
        """Evita que dos redespliegues de la misma app se ejecuten a la vez."""
        with self.locks_lock:
            return self.locks.setdefault(app, RLock())

    @staticmethod
    def _service_from_dict(data):
        """Recupera del estado un servicio que ya fue desplegado."""
        return Service(data['id'], data['machine'], data['deployment'],
                       tuple(data['links']), tuple(data['hybrid_links']), data['definition'])

    def _app(self, app):
        if app not in self.registry:
            raise KeyError(f'Aplicacion no registrada: {app}')
        return self.store.data['apps'][app]

    def _package(self, app, config):
        """Las especificaciones deben proceder del ZIP importado, nunca de otro directorio."""
        path = (self.root / config['package']).resolve()
        if path.parent != (self.root / 'imported').resolve() or path.name != app or not path.is_dir():
            raise ValueError('Paquete importado no valido')
        return path

    def _load_apps(self):
        """Al iniciar solo lee YAML y JSON. No llama a Git ni a Docker."""
        for app, config in self.registry.items():
            package = self._package(app, config)
            qos = normalize(package)
            initial = json.loads((package / 'app.json').read_text(encoding='utf-8'))
            parse_plan(initial, qos)  # Validacion sin operaciones externas.
            with self.store.lock:
                record = self.store.data['apps'].get(app)
                if record is None:
                    record = {'status': 'registered', 'version': 0, 'active': {},
                              'current_plan': None}
                record.update({'display_name': record.get('display_name', app), 'package': config['package'],
                               'qos': qos, 'initial_plan': initial,
                               'source_config': config})
                self.store.data['apps'][app] = record
                self.store.save()

    def _save_registry(self):
        """Persiste solo la lista de aplicaciones importadas."""
        path = self.root / 'configs/apps.json'
        path.write_text(json.dumps({'apps': self.registry}, indent=2), encoding='utf-8')

    def add_app(self, config, app_id):
        """Valida el paquete ZIP y lo registra, sin descargar fuentes."""
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,30}', app_id):
            raise ValueError('ID de aplicacion no valido')
        with self.store.lock:
            if app_id in self.registry:
                raise ValueError('La aplicacion ya existe')
            self.registry[app_id] = config
            try:
                self._load_apps()
                self._save_registry()
            except Exception:
                self.registry.pop(app_id, None)
                self.store.data['apps'].pop(app_id, None)
                raise
        return self.details(app_id)

    def remove_app(self, app_id):
        """Primero detiene los contenedores y despues borra el registro y el ZIP."""
        with self._lock(app_id):
            record = self._app(app_id)
            if record['active']:
                self.stop(app_id)
            with self.store.lock:
                self.registry.pop(app_id)
                self.store.data['apps'].pop(app_id)
                # Borrar definitivamente una app también elimina sus eventos
                # antiguos: al reimportarla, su historial debe empezar vacío.
                self.store.data['events'] = [e for e in self.store.data['events']
                                             if e['app'] != app_id]
                self._save_registry()
                self.store.save()
            shutil.rmtree(self.root / 'imported' / app_id, ignore_errors=True)
            shutil.rmtree(self.store.root / 'checkouts' / app_id, ignore_errors=True)
            # Las imágenes de la app anterior no deben reutilizarse al reimportar.
            if hasattr(self.backend, 'forget_app'):
                self.backend.forget_app(app_id)
        return {'removed': app_id}

    def list_apps(self):
        """Datos resumidos para la pagina principal."""
        return [{'id': app, 'display_name': rec['display_name'],
                 'status': rec['status'], 'version': rec['version'],
                 'services': len(rec['active']),
                 'monitors': len(rec['active']) if rec['status'] == 'running' else 0}
                for app, rec in sorted(self.store.data['apps'].items()) if app in self.registry]

    def details(self, app):
        """Devuelve estado, metricas y eventos a la interfaz web."""
        rec = self._app(app)
        return {'id': app, 'display_name': rec['display_name'], 'status': rec['status'],
                'version': rec['version'], 'active': rec['active'],
                'initial_plan': rec['initial_plan'], 'current_plan': rec['current_plan'],
                'qos': rec['qos'], 'events': self.store.events(app),
                'source': {'package': rec['source_config']['package'],
                           'repositories': {k: v['git_url'] for k, v in rec['qos']['repositories'].items()},
                           'plan': 'app.json'}, 'available_plans': []}

    def rename_app(self, app, display_name):
        """Cambia solo el nombre visible; el ID, los contenedores y OAS no varían."""
        if not isinstance(display_name, str) or not 1 <= len(display_name.strip()) <= 70:
            raise ValueError('El nombre debe tener entre 1 y 70 caracteres')
        with self._lock(app):
            rec = self._app(app)
            rec['display_name'] = display_name.strip()
            self.store.event(app, 'application_renamed', display_name=rec['display_name'])
            return self.details(app)

    def deploy_all(self):
        """Despliega todas las aplicaciones no activas y continúa si falla alguna.

        Si una aplicación falla, su reversión se gestiona dentro de apply().
        El resultado contiene el éxito o el error de cada aplicación.
        """
        results = []
        for app in list(self.registry):
            state = self._app(app)['status']
            if state == 'running':
                results.append({'id': app, 'status': 'already_running'})
                continue
            try:
                self.apply(app)
                results.append({'id': app, 'status': 'deployed'})
            except Exception as exc:
                results.append({'id': app, 'status': 'error', 'error': str(exc)})
        return {'results': results}

    def _active_specs(self, record):
        return {sid: self._service_from_dict(state['service']) for sid, state in record['active'].items()}

    def analyze(self, app, plan=None):
        """Compara el plan deseado con los servicios activos, sin tocar Docker/Git."""
        record = self._app(app)
        return compare(self._active_specs(record), parse_plan(plan or record['initial_plan'], record['qos']))

    def _start_service(self, app, service, record, target):
        """DESCARGA BAJO DEMANDA el Git indicado en OAS y arranca el contenedor."""
        repo_name = service.definition['repository']
        source = record['qos']['repositories'][repo_name]
        root = self.git.clone(app, repo_name, source)
        context = (root / source['source']).resolve()
        if context != root and root not in context.parents:
            raise ValueError('El Dockerfile debe estar dentro del repositorio')
        if not (context / 'Dockerfile').is_file():
            raise ValueError(f'{service.id}: no hay Dockerfile en {source["git_url"]}')
        # Los puertos de los destinatarios tambien vienen de OAS.
        definition = service.definition.copy()
        definition['binding_ports'] = {key: target[dest].definition['port']
                                       for key, dest in definition['bindings'].items()}
        configured = Service(service.id, service.machine, service.deployment,
                             service.links, service.hybrid_links, definition)
        return self.backend.start(app, configured, context, [])

    @staticmethod
    def _creation_order(operations, target):
        """Arranca proveedores antes que consumidores (según bindings de OAS)."""
        pending = {x['service'] for x in operations if x['action'] in ('create', 'replace')}
        result, visited = [], set()

        def visit(sid):
            if sid in visited:
                return
            visited.add(sid)
            for dependency in target[sid].links:
                if dependency in pending:
                    visit(dependency)
            result.append(sid)

        for sid in sorted(pending):
            visit(sid)
        return result

    def _remove_changed(self, app, operations, record):
        """Libera nombres de contenedor antes de recrear alternativas."""
        for operation in operations:
            if operation['action'] in ('remove', 'replace'):
                sid = operation['service']
                self.backend.stop(app, sid)
                record['active'].pop(sid, None)
                self.store.event(app, 'service_removed', service=sid)

    def _create_changed(self, app, operations, record, target):
        """Crea los contenedores en orden de dependencias declarado en OAS."""
        for sid in self._creation_order(operations, target):
            srv = target[sid]
            runtime = self._start_service(app, srv, record, target)
            record['active'][sid] = {'service': srv.to_dict(), 'runtime': runtime,
                                     'monitor': {'status': 'pending', 'metrics': {},
                                                 'violations': [], 'history': []}}
            self.store.event(app, 'service_created', service=sid,
                             alternative=srv.definition['alternative'],
                             container_id=runtime['container_id'])

    def _rollback(self, app, record, backup, previous_plan, reason, operations):
        """Deshace SOLO las sustituciones: los contenedores intactos se conservan.

        Detiene incluso contenedores nuevos que pudieron arrancar antes de
        producirse la excepción. Restaura los anteriores desde Git/OAS.
        """
        affected = {op['service'] for op in operations if op['action'] != 'keep'}
        prior = {sid: self._service_from_dict(v['service']) for sid, v in backup.items()}
        # Primero retirar contenedores parciales para liberar los nombres Docker.
        for sid in sorted(affected):
            self.backend.stop(app, sid)
            record['active'].pop(sid, None)
        # Volver a crear únicamente los anteriores que fueron retirados.
        restore = [{'service': sid, 'action': 'create'} for sid in affected if sid in backup]
        for sid in self._creation_order(restore, prior):
            runtime = self._start_service(app, prior[sid], record, prior)
            record['active'][sid] = {
                'service': backup[sid]['service'], 'runtime': runtime,
                'monitor': {'status': 'pending', 'metrics': {}, 'violations': [], 'history': []}}
        record['current_plan'] = previous_plan
        record['status'] = 'running' if backup else 'registered'
        record.pop('pending_operation', None)
        self.store.event(app, 'deployment_rolled_back', reason=reason,
                         affected=sorted(affected))

    def apply(self, app, plan=None, reason='manual'):
        """Analiza, descarga lo necesario y ejecuta los cambios en Docker.

        Se guarda una copia del estado para revertir una operacion incompleta.
        Los servicios sin cambios se mantienen; el monitor descubre los nuevos.
        """
        with self._lock(app):
            rec = self._app(app)
            selected = plan if plan is not None else rec['initial_plan']
            target = parse_plan(selected, rec['qos'])
            operations = compare(self._active_specs(rec), target)
            previous_plan = rec.get('current_plan')
            backup = json.loads(json.dumps(rec['active']))
            rec['status'] = 'deploying'
            rec['pending_operation'] = {'previous_plan': previous_plan, 'target_plan': selected}
            self.store.event(app, 'deployment_started', reason=reason, operations=operations)
            try:
                self._remove_changed(app, operations, rec)
                self._create_changed(app, operations, rec, target)
                # Incluso si se mantiene un contenedor, se actualiza su plan logico.
                for sid, srv in target.items():
                    rec['active'][sid]['service'] = srv.to_dict()
                rec['current_plan'] = selected
                rec['version'] += 1
                rec['status'] = 'running'
                rec.pop('pending_operation', None)
                self.store.event(app, 'deployment_completed', version=rec['version'], operations=operations)
                return self.details(app)
            except Exception as exc:
                self.store.event(app, 'deployment_failed', error=str(exc))
                try:
                    self._rollback(app, rec, backup, previous_plan, str(exc), operations)
                except Exception as rollback_exc:
                    rec['status'] = 'error'
                    self.store.event(app, 'rollback_failed', error=str(rollback_exc))
                raise

    def recover_all(self):
        """Tras reiniciar la web, recupera un plan confirmado si quedo incompleto."""
        for app in list(self.registry):
            record = self._app(app)
            incomplete = record.get('pending_operation') or record['status'] in ('deploying', 'error')
            missing = record['status'] == 'running' and any(
                not self.backend.discover(app, sid) for sid in record['active'])
            if not incomplete and not missing:
                continue
            old = (record.get('pending_operation') or {}).get('previous_plan') or record['current_plan']
            try:
                self.backend.stop_app(app)
                record['active'] = {}
                record['status'] = 'registered'
                record.pop('pending_operation', None)
                if old:
                    self.apply(app, old, reason='startup_recovery')
                else:
                    self.store.save()
                self.store.event(app, 'startup_recovery_completed')
            except Exception as exc:
                record['status'] = 'error'
                self.store.event(app, 'startup_recovery_failed', error=str(exc))

    def stop(self, app):
        """Detiene la aplicacion; conserva sus OAS/REQ para otro despliegue."""
        with self._lock(app):
            rec = self._app(app)
            rec['status'] = 'stopping'
            self.store.save()
            for sid in list(rec['active']):
                self.backend.stop(app, sid)
                rec['active'].pop(sid, None)
                self.store.event(app, 'service_removed', service=sid)
            self.backend.stop_app(app)
            rec['status'] = 'stopped'
            rec['current_plan'] = None
            self.store.event(app, 'application_stopped')
            return self.details(app)

    def choose_plan(self, app):
        """Genera un nuevo JSON con otra alternativa de REQ al pulsar Replanificar."""
        plan, _, _ = self.planner.random_plan(self._app(app))
        return plan

    @staticmethod
    def _snapshot_containers(record):
        """Obtiene alternativa e identificador Docker de cada contenedor activo."""
        return {sid: {'alternative': state['service']['definition']['alternative'],
                      'container_id': state['runtime']['container_id']}
                for sid, state in record['active'].items()}

    def _record_adaptation(self, app, changes, before, previous_mode, origin):
        """Registra la sustitución únicamente tras confirmarse en el backend Docker."""
        record = self._app(app)
        after = self._snapshot_containers(record)
        self.store.event(app, 'adaptation_completed', origin=origin, changes=changes,
                         removed_containers={k: before[k] for k in before if k not in after or before[k] != after[k]},
                         created_containers={k: after[k] for k in after if k not in before or before[k] != after[k]},
                         kept=sorted(k for k in before.keys() & after.keys() if before[k] == after[k]),
                         previous_mode=previous_mode, new_mode=self._architecture_mode(record))

    def replan(self, app, plan=None, reason='manual'):
        """Replanificación manual: misma ejecución Docker que la adaptación automática."""
        with self._lock(app):
            record = self._app(app)
            if record['status'] != 'running':
                raise ValueError('La aplicacion debe estar desplegada')
            previous = {x['id']: x['alternative'] for x in record['current_plan']['services']}
            before = self._snapshot_containers(record)
            mode_before = self._architecture_mode(record)
            result = self.apply(app, plan or self.choose_plan(app), reason=reason)
            updated = {x['id']: x['alternative'] for x in record['current_plan']['services']}
            changes = [{'service': sid, 'from': old, 'to': updated[sid]}
                       for sid, old in previous.items() if updated[sid] != old]
            if changes:
                self._record_adaptation(app, changes, before, mode_before, origin='manual')
            return self.details(app)

    def check(self, app, sid, expected_container_id=None, sample=None):
        """Consulta /health y /metrics DESDE OAS y contrasta valores con REQ."""
        with self.store.lock:
            record = self._app(app)
            if record['status'] != 'running' or sid not in record['active']:
                return None
            state = record['active'][sid]
            service = self._service_from_dict(state['service'])
            runtime = state['runtime'].copy()
            cid = runtime['container_id']
            if expected_container_id is not None and cid != expected_container_id:
                return None
        result = sample if sample is not None else self.backend.probe(app, service, runtime)
        limits = service.definition['monitor']['limits']
        measurements = result.get('metrics', {})
        metrics = {key: value for key, value in measurements.items()
                   if key in limits and isinstance(value, (int, float)) and not isinstance(value, bool)}
        violations = [] if result.get('healthy') else ['health']
        for name, bound in limits.items():
            value = metrics.get(name)
            if value is None:
                violations.append(f'{name}: no disponible')
            elif 'max' in bound and value > bound['max']:
                violations.append(f'{name}: {value} > {bound["max"]}')
            elif 'min' in bound and value < bound['min']:
                violations.append(f'{name}: {value} < {bound["min"]}')
        with self.store.lock:
            latest = record['active'].get(sid)
            if not latest or record['status'] != 'running' or latest['runtime']['container_id'] != cid:
                return None
            previous = latest['monitor'].get('violations', [])
            history = latest['monitor'].get('history', [])[-39:]
            history.append({'time': datetime.now(timezone.utc).isoformat(), 'metrics': metrics})
            latest['monitor'] = {'status': 'violation' if violations else 'healthy',
                                 'metrics': metrics, 'violations': violations,
                                 'history': history, 'error': result.get('error')}
            if violations and previous != violations:
                self.store.event(app, 'qos_violated', service=sid, violations=violations)
            else:
                self.store.save()
        return violations

    def on_violations(self, app, failures):
        """Planifica y aplica UN SOLO cambio para varios fallos simultáneos.

        failures: [(servicio, lista_violaciones, container_id), ...].
        Cada ID se comprueba para evitar actuar sobre un contenedor sustituido.
        Los que no tienen alternativa se informan sin bloquear los demás.
        """
        with self._lock(app):
            rec = self._app(app)
            if rec['status'] != 'running':
                return None
            now = time.monotonic()
            valid = []
            for sid, violations, cid in failures:
                state = rec['active'].get(sid)
                if not state or (cid and state['runtime']['container_id'] != cid):
                    continue
                key = (app, state['service']['definition']['logical_id'])
                if now >= self.retry_after.get(key, 0):
                    valid.append((sid, violations, key))
            if not valid:
                return None

            # Una categoría híbrida tiene más de un contenedor, pero una
            # sola alternativa lógica que aparece en el JSON.
            logical_ids = list(dict.fromkeys(
                rec['active'][sid]['service']['definition']['logical_id']
                for sid, _, _ in valid))
            self.store.event(app, 'planner_requested',
                             services=[sid for sid, _, _ in valid],
                             violations={sid: v for sid, v, _ in valid})
            plan, changes, without_alternative = self.planner.alternatives_for(rec, logical_ids)
            for logical_id in without_alternative:
                self.store.event(app, 'planner_no_alternative', service=logical_id,
                                 reason='REQ solo contiene la alternativa actual')
            if not changes:
                for _, _, key in valid:
                    self.retry_after[key] = now + 120
                return None

            parse_plan(plan, rec['qos'])
            generated_dir = self.store.root / 'generated_plans' / app
            generated_dir.mkdir(parents=True, exist_ok=True)
            filename = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S%f.json')
            (generated_dir / filename).write_text(json.dumps(plan, indent=2), encoding='utf-8')
            self.store.event(app, 'planner_plan_generated', changes=changes, plan_file=filename)
            before = self._snapshot_containers(rec)
            previous_mode = self._architecture_mode(rec)
            try:
                result = self.apply(app, plan, reason='qos_failure:' + ','.join(logical_ids))
            except Exception as exc:
                # Tras rollback válido se conserva el plan anterior; si falla
                # incluso el rollback, el estado ERROR detiene los monitores.
                for _, _, key in valid:
                    self.retry_after[key] = now + 120
                self.store.event(app, 'planner_error', services=logical_ids, error=str(exc))
                return None

            for _, _, key in valid:
                if key[1] in without_alternative:
                    self.retry_after[key] = now + 120
                else:
                    self.retry_after.pop(key, None)
            self._record_adaptation(app, changes, before, previous_mode, origin='automatic')
            return self.details(app)

    def on_violation(self, app, sid, violations, expected_container_id=None):
        """Compatibilidad con llamadas individuales; utiliza el mismo flujo por lotes."""
        return self.on_violations(app, [(sid, violations, expected_container_id)])

    @staticmethod
    def _architecture_mode(record):
        """Etiqueta la arquitectura segun la alternativa realmente desplegada."""
        types = [s['service']['definition']['architecture'] for s in record['active'].values()]
        return 'híbrido clásico-cuántico' if 'hybrid' in types else 'clásico'
