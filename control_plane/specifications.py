"""Lee OAS y REQ originales. No conoce las aplicaciones ni sus repositorios.

OAS/<tipo>.yml define las implementaciones y sus URL Git, rutas HTTP y conexiones.
REQ/<tipo>.yml define las alternativas y los limites que evalua cada monitor.
"""
from pathlib import Path
from urllib.parse import urlsplit
import re
import yaml


def read_yaml(path: Path) -> dict:
    """Carga un YAML de especificaciones sin ejecutar codigo contenido en el archivo."""
    data = yaml.safe_load(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError(f'{path.name}: YAML debe ser un objeto')
    return data


def validate_repository(data: dict, name: str) -> dict:
    """Acepta unicamente repositorios GitHub HTTPS, nunca codigo local."""
    if not isinstance(data, dict):
        raise ValueError(f'{name}: falta repository en OAS')
    url = data.get('url')
    address = urlsplit(url) if isinstance(url, str) else None
    if (not address or address.scheme != 'https' or address.hostname != 'github.com'
            or address.username or address.password or address.port
            or address.query or address.fragment
            or not re.fullmatch(r'/[\w.-]+/[\w.-]+(?:\.git)?', address.path)):
        raise ValueError(f'{name}: se necesita una URL HTTPS de repositorio GitHub')
    ref = data.get('ref', 'main')
    context = data.get('docker_context', '.')
    if (not isinstance(ref, str) or not re.fullmatch(r'[\w./-]{1,100}', ref)
            or '..' in ref or ref.startswith(('/', '-')) or ref.endswith(('/', '.lock'))):
        raise ValueError(f'{name}: rama Git no valida')
    if (not isinstance(context, str) or not context or context.startswith(('/', '\\'))
            or '\\' in context or '..' in Path(context).parts):
        raise ValueError(f'{name}: docker_context debe estar dentro del repositorio')
    return {'git_url': url, 'ref': ref, 'source': context}


def limits_of(req: dict, alternative: str) -> dict:
    """Traduce los REQ OASP4HA a comprobaciones numericas de QoS.

    execution_time: maximo; availability: minimo; reliability: maximo
    (en estas especificaciones reliability representa una tasa de fallo).
    Nunca se generan limites ficticios para metricas no declaradas.
    """
    rule = req['alternatives'][alternative]
    behaviour = rule.get('behaviour', {})
    result = {}
    for key, bound in [('execution_time', 'max'), ('availability', 'min'), ('reliability', 'max')]:
        value = behaviour.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result[key] = {bound: value}
    # Compatibilidad con otros requisitos de QoS expresados como min/max.
    other = rule.get('monitoring', {}).get('limits', {})
    if isinstance(other, dict):
        for key, value in other.items():
            if isinstance(value, dict) and any(x in value for x in ('min', 'max')):
                result[key] = value
    if not result:
        raise ValueError(f'{alternative}: REQ no contiene limites monitorizables')
    return result


def normalize(package: Path) -> dict:
    """Relaciona los YAML OAS/REQ del mismo nombre, sin descargar repositorios.

    El JSON solo indicara {id, alternative}; REQ determina su categoria,
    y OAS describe como ejecutar la alternativa elegida.
    """
    oas_dir, req_dir = package / 'OAS', package / 'REQ'
    if not oas_dir.is_dir() or not req_dir.is_dir():
        raise ValueError('Se necesitan las carpetas OAS/ y REQ/')
    req_files = sorted(req_dir.glob('*.yml')) + sorted(req_dir.glob('*.yaml'))
    if not req_files:
        raise ValueError('REQ/ no contiene ningun YAML')
    result = {'categories': {}, 'alternative_category': {}, 'repositories': {}}
    for req_path in req_files:
        name = req_path.stem
        matching = [p for p in (oas_dir / f'{name}.yml', oas_dir / f'{name}.yaml') if p.is_file()]
        if not matching:
            raise ValueError(f'Falta OAS/{name}.yml para REQ/{req_path.name}')
        req, oas = read_yaml(req_path), read_yaml(matching[0])
        if not isinstance(oas.get('paths'), dict):
            raise ValueError(f'OAS/{name}: falta paths de OpenAPI')
        if not isinstance(req.get('alternatives'), dict) or not req['alternatives']:
            raise ValueError(f'REQ/{name}: alternatives debe contener opciones')
        ext = oas.get('x-deployment')
        if not isinstance(ext, dict) or not isinstance(ext.get('alternatives'), dict):
            raise ValueError(f'OAS/{name}: faltan x-deployment.alternatives')
        paths = ext.get('monitoring', {})
        for key in ('health_path', 'metrics_path'):
            route = paths.get(key)
            if not isinstance(route, str) or not route.startswith('/') or 'get' not in oas['paths'].get(route, {}):
                raise ValueError(f'OAS/{name}: ruta GET {key} no declarada en paths')
        test_path = paths.get('simulation_path')
        if test_path is not None and (not isinstance(test_path, str) or not test_path.startswith('/')
                                      or 'post' not in oas['paths'].get(test_path, {})):
            raise ValueError(f'OAS/{name}: simulation_path no declara POST en paths')
        port = ext.get('port', 8080)
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError(f'OAS/{name}: puerto no valido')
        for alt_name in req['alternatives']:
            if alt_name in result['alternative_category']:
                raise ValueError(f'La alternativa {alt_name} aparece en mas de un REQ')
            if alt_name not in ext['alternatives']:
                raise ValueError(f'OAS/{name} no describe la alternativa {alt_name} de REQ')
            variant = ext['alternatives'][alt_name]
            components = variant.get('components') if isinstance(variant, dict) else None
            if not isinstance(components, list) or not components:
                raise ValueError(f'OAS/{name} alternativa {alt_name}: necesita components')
            suffixes = []
            for index, component in enumerate(components):
                if not isinstance(component, dict):
                    raise ValueError(f'{alt_name}: componente debe ser un objeto')
                suffix = component.get('suffix', '')
                if not isinstance(suffix, str) or not re.fullmatch(r'(?:-[a-z0-9-]+)?', suffix):
                    raise ValueError(f'{alt_name}: suffix no valido')
                suffixes.append(suffix)
                key = f'{name}-{alt_name}-{index}'
                result['repositories'][key] = validate_repository(component.get('repository'), key)
            if suffixes.count('') != 1 or len(set(suffixes)) != len(suffixes):
                raise ValueError(f'{alt_name}: debe existir un componente principal (suffix vacio)')
            result['alternative_category'][alt_name] = name
        if set(ext['alternatives']) != set(req['alternatives']):
            raise ValueError(f'OAS/{name}: alternativas no coinciden con REQ/{name}')
        result['categories'][name] = {
            'req': req, 'oas': oas, 'deployment': ext, 'port': port,
            'monitoring': paths,
        }
    return result
