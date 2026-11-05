"""La importacion solo extrae OAS/, REQ/ y el plan inicial en JSON.

No clona Git: el repositorio se descargara si y cuando se despliegue el servicio.
"""
from pathlib import Path, PurePosixPath
from zipfile import ZipFile, BadZipFile
import json
import re
import shutil
import stat


def read_manifest(archive: ZipFile):
    """Comprueba la estructura y evita escapes de ruta o ZIP desproporcionados."""
    files = archive.infolist()
    if len(files) > 100 or sum(i.file_size for i in files) > 10_000_000:
        raise ValueError('El ZIP es demasiado grande para un paquete de especificaciones')
    names = set()
    for info in files:
        name = info.filename
        parts = PurePosixPath(name).parts
        mode = info.external_attr >> 16
        if (not name or name.startswith('/') or '\\' in name or '..' in parts or
                stat.S_ISLNK(mode) or name in names):
            raise ValueError('El ZIP contiene rutas peligrosas, duplicadas o enlaces simbolicos')
        names.add(name)
        if not info.is_dir() and name != 'app.json' and not (
                len(parts) == 2 and parts[0] in ('OAS', 'REQ') and name.endswith(('.yml', '.yaml'))):
            raise ValueError('El ZIP solo admite app.json, OAS/*.yml y REQ/*.yml')
    if 'app.json' not in names or not any(x.startswith('OAS/') and x.endswith('.yml') for x in names) or not any(x.startswith('REQ/') and x.endswith('.yml') for x in names):
        raise ValueError('Se requiere app.json y las carpetas OAS/ y REQ/ con sus YAML')
    try:
        plan = json.loads(archive.read('app.json'))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError('app.json debe ser JSON valido') from exc
    if not isinstance(plan, dict) or set(plan) != {'services'}:
        raise ValueError('app.json solo puede contener la lista services')
    return plan


def import_application(platform, zip_file: Path, filename: str | None = None):
    """Registra especificaciones sin descargar ningun repositorio ni ejecutar Docker."""
    zip_file = Path(zip_file)
    name = Path(filename or zip_file.name).stem.lower()
    app_id = re.sub(r'[^a-z0-9-]', '-', name).strip('-')[:30]
    if not app_id or not app_id[0].isalpha():
        raise ValueError('El ZIP debe tener un nombre de aplicacion valido (ej. app-a.zip)')
    try:
        with ZipFile(zip_file) as archive:
            read_manifest(archive)
            if app_id in platform.registry:
                raise ValueError(f'La aplicacion {app_id} ya esta registrada')
            folder = platform.root / 'imported' / app_id
            if folder.exists():
                raise ValueError('La carpeta de la aplicacion ya existe')
            folder.mkdir(parents=True)
            try:
                archive.extractall(folder)
                return platform.add_app({'package': f'imported/{app_id}'}, app_id)
            except Exception:
                shutil.rmtree(folder, ignore_errors=True)
                raise
    except BadZipFile as exc:
        raise ValueError('El archivo subido no es ZIP') from exc
