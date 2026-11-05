"""Planificador sencillo: sortea otra alternativa del mismo REQ.

El JSON generado es IDENTICO al JSON inicial: lista de {id, alternative}.
"""
from copy import deepcopy
import random


class RandomPlanProvider:
    def __init__(self, rng=None):
        self.rng = rng or random.SystemRandom()

    def request_plan(self, app, failed_service, record):
        """Busca la categoria del servicio que falla y selecciona otra variante."""
        active = record['active'].get(failed_service)
        if not active:
            raise ValueError('El servicio que fallo ya no esta desplegado')
        definition = active['service']['definition']
        return self.alternative_for(record, definition['logical_id'])

    def alternative_for(self, record, logical_id):
        """Elige en REQ sin repetir la alternativa actual de esa instancia."""
        current = record.get('current_plan')
        if not current:
            raise ValueError('No hay plan vigente')
        entry = next((x for x in current['services'] if x['id'] == logical_id), None)
        if entry is None:
            raise ValueError(f'No existe {logical_id} en el plan actual')
        category = record['qos']['alternative_category'][entry['alternative']]
        req = record['qos']['categories'][category]['req']
        options = [name for name in req['alternatives'] if name != entry['alternative']]
        if not options:
            raise ValueError(f'{category}: REQ no tiene alternativa diferente')
        chosen = self.rng.choice(options)
        generated = deepcopy(current)
        for service in generated['services']:
            if service['id'] == logical_id:
                service['alternative'] = chosen
                break
        return generated, chosen, f'{logical_id}: {entry["alternative"]} -> {chosen}'

    def alternatives_for(self, record, logical_ids):
        """Cambia varias alternativas en una copia del plan vigente de la app.

        Si un REQ tiene solo una opción no se inventa otra implementación.
        """
        current = record['current_plan']
        if not current:
            raise ValueError('No hay plan vigente')
        generated = deepcopy(current)
        changes, unavailable = [], []
        for logical_id in dict.fromkeys(logical_ids):
            entry = next((s for s in generated['services'] if s['id'] == logical_id), None)
            if not entry:
                continue
            previous = entry['alternative']
            category = record['qos']['alternative_category'][previous]
            options = [x for x in record['qos']['categories'][category]['req']['alternatives']
                       if x != previous]
            if not options:
                unavailable.append(logical_id)
                continue
            chosen = self.rng.choice(options)
            entry['alternative'] = chosen
            changes.append({'service': logical_id, 'from': previous, 'to': chosen})
        return generated, changes, unavailable

    def random_plan(self, record):
        """Plan manual: escoge una instancia con alternativas distintas en REQ."""
        choices = []
        for entry in record['current_plan']['services']:
            category = record['qos']['alternative_category'][entry['alternative']]
            if len(record['qos']['categories'][category]['req']['alternatives']) > 1:
                choices.append(entry['id'])
        if not choices:
            raise ValueError('Ningun servicio tiene alternativas en REQ')
        return self.alternative_for(record, self.rng.choice(choices))
