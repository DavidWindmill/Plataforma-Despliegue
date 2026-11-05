"""El JSON inicial y los JSON del planificador tienen el mismo formato.

{"services": [{"id": "sensor00", "alternative": "sensor-1"}, ...]}
Los repositorios, limites, conexiones y endpoints NO aparecen en el plan.
"""
from dataclasses import dataclass, asdict
from typing import Any
import json


@dataclass(frozen=True)
class Service:
    id: str
    machine: str
    deployment: str
    links: tuple[str, ...]
    hybrid_links: tuple[str, ...]
    definition: dict[str, Any]

    def deployment_signature(self):
        """Detecta cambios de implementacion aunque el identificador sea el mismo."""
        return json.dumps([self.machine, self.definition], sort_keys=True)

    def to_dict(self):
        """Permite guardar el estado de una instancia activa en JSON."""
        return asdict(self)


def _resolve(value, logical_id):
    """Sustituye $id para referenciar la instancia seleccionada, sin codigo por app."""
    return value.replace('$id', logical_id) if isinstance(value, str) else value


def parse_plan(plan: dict, qos: dict) -> dict[str, Service]:
    """Expande cada servicio logico en los contenedores descritos en su OAS.

    Una alternativa cuantica puede implicar pasarela + QPU. El JSON no
    tiene por que conocer ese detalle de infraestructura.
    """
    entries = plan.get('services') if isinstance(plan, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError('El JSON requiere una lista no vacia services')
    instances = set()
    result = {}
    for selected in entries:
        if not isinstance(selected, dict) or set(selected) != {'id', 'alternative'}:
            raise ValueError('Cada servicio del JSON solo admite id y alternative')
        sid, alternative = selected['id'], selected['alternative']
        if not isinstance(sid, str) or not sid or sid in instances:
            raise ValueError(f'Instancia duplicada o vacia: {sid}')
        instances.add(sid)
        category = qos['alternative_category'].get(alternative)
        if not category:
            raise ValueError(f'{sid}: alternativa no definida en REQ: {alternative}')
        contract = qos['categories'][category]
        ext = contract['deployment']
        variant = ext['alternatives'][alternative]
        # Los valores de entorno y bindings se declaran en OAS, no en el plan.
        instance = ext.get('instances', {}).get(sid, {})
        defaults = ext.get('environment', {})
        for index, component in enumerate(variant['components']):
            real_id = sid + component.get('suffix', '')
            if real_id in result:
                raise ValueError(f'Colision de contenedores: {real_id}')
            environment = {**defaults, **instance.get('environment', {}),
                           **variant.get('environment', {}), **component.get('environment', {})}
            environment = {key: _resolve(value, sid) for key, value in environment.items()}
            bindings = {**ext.get('bindings', {}), **variant.get('bindings', {}),
                        **component.get('bindings', {})}
            bindings = {key: _resolve(value, sid) for key, value in bindings.items()}
            definition = {
                'repository': f'{category}-{alternative}-{index}',
                'source': qos['repositories'][f'{category}-{alternative}-{index}']['source'],
                'environment': environment,
                'bindings': bindings,
                'binding_ports': {k: 8080 for k in bindings},
                'port': contract['port'],
                'monitor': {
                    **contract['monitoring'],
                    'limits': qos_limits(contract['req'], alternative) if component.get('monitor_metrics', True) else {},
                },
                'logical_id': sid,
                'alternative': alternative,
                'category': category,
                'architecture': 'hybrid' if len(variant['components']) > 1 else 'classical',
            }
            result[real_id] = Service(real_id, 'docker', '', tuple(bindings.values()), (), definition)
    # Asegurar que los destinos de las conexiones definidas en OAS existen.
    for actual in result.values():
        for target in actual.links:
            if target not in result:
                raise ValueError(f'{actual.id}: enlace OAS a un servicio no seleccionado: {target}')
    return result


def qos_limits(req, alternative):
    """Punto unico de lectura de los umbrales del REQ de cada alternativa."""
    from .specifications import limits_of
    return limits_of(req, alternative)


def compare(old, new):
    """Operaciones necesarias para convertir el despliegue actual en el nuevo."""
    operations = []
    for sid in sorted(set(old) - set(new)):
        operations.append({'action': 'remove', 'service': sid, 'reason': 'Alternativa retirada'})
    for sid in sorted(set(new) - set(old)):
        operations.append({'action': 'create', 'service': sid, 'reason': 'Servicio nuevo'})
    for sid in sorted(set(old) & set(new)):
        if old[sid].deployment_signature() != new[sid].deployment_signature():
            action, reason = 'replace', 'Cambio de alternativa o configuracion OAS/REQ'
        else:
            action, reason = 'keep', 'Sin cambios'
        operations.append({'action': action, 'service': sid, 'reason': reason})
    return operations
