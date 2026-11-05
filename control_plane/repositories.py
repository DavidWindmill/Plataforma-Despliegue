"""Descarga bajo demanda los repositorios Git descritos en OAS.

No permite elegir carpetas locales como origen: cada servicio se obtiene de
la URL Git de su propio YAML cuando va a desplegarse.
"""
from pathlib import Path
import os
import shutil
import subprocess


class GitRepositories:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)

    def clone(self, app: str, name: str, spec: dict) -> Path:
        """Reutiliza una descarga válida o repara automáticamente un clon incompleto.

        Una carpeta .git no basta: el Dockerfile del contexto indicado en OAS
        debe existir. Descargamos primero en una carpeta temporal para no dejar
        un repositorio a medias si Git falla.
        """
        destination = self.data_dir / 'checkouts' / app / name
        dockerfile = Path(spec['source']) / 'Dockerfile'
        if (destination / '.git').is_dir() and (destination / dockerfile).is_file():
            return destination.resolve()

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + '.downloading')
        shutil.rmtree(temporary, ignore_errors=True)
        env = os.environ.copy()
        env['GIT_TERMINAL_PROMPT'] = '0'
        try:
            result = subprocess.run(
                ['git', 'clone', '--depth', '1', '--branch', spec['ref'],
                 '--', spec['git_url'], str(temporary)],
                capture_output=True, text=True, timeout=180, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            shutil.rmtree(temporary, ignore_errors=True)
            raise RuntimeError(f'{name}: no se pudo ejecutar Git: {exc}') from exc
        if result.returncode:
            shutil.rmtree(temporary, ignore_errors=True)
            raise RuntimeError(
                f'{name}: no se pudo descargar {spec["git_url"]} '
                f'(rama {spec["ref"]}). Comprueba el acceso a GitHub. '
                f'{result.stderr.strip()[-400:]}')
        if not (temporary / dockerfile).is_file():
            shutil.rmtree(temporary, ignore_errors=True)
            raise RuntimeError(
                f'{name}: el repositorio {spec["git_url"]} '
                f'(rama {spec["ref"]}) no contiene {dockerfile.as_posix()}. '
                'Añade el Dockerfile al repositorio o corrige docker_context en OAS.')

        # La copia nueva ya se ha comprobado: ahora sustituimos la incompleta.
        # En Windows, borrar un .git bloqueado puede provocar PermissionError.
        # Convertimos ese error en un mensaje controlado para la interfaz web.
        try:
            if destination.exists():
                shutil.rmtree(destination)
            temporary.rename(destination)
        except OSError as exc:
            raise RuntimeError(
                f'{name}: no se ha podido sustituir el clon Git de {destination}. '
                f'Cierra los procesos que usen esa carpeta y elimina solo ese '
                f'checkout (nunca imported/ ni data/state.json). '
                f'Detalle del sistema: {exc}'
            ) from exc
        return destination.resolve()
