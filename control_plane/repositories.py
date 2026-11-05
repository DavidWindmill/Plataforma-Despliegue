"""Descarga de código fuente exclusivamente desde repositorios GitHub.

No existe posibilidad de aportar rutas de código locales en la especificación.
"""
from pathlib import Path
import os
import shutil
import subprocess


class GitRepositories:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)

    def clone(self, app: str, name: str, spec: dict) -> Path:
        """Clona una sola vez una referencia Git y reutiliza su copia local."""
        destination = self.data_dir / 'checkouts' / app / name
        if (destination / '.git').is_dir():
            return destination.resolve()
        if destination.exists():
            raise ValueError(f'Clonado incompleto: {destination}')
        destination.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env['GIT_TERMINAL_PROMPT'] = '0'  # Evita dejar colgada la petición de la web.
        try:
            result = subprocess.run(
                ['git', 'clone', '--depth', '1', '--branch', spec['ref'],
                 '--', spec['git_url'], str(destination)],
                capture_output=True, text=True, timeout=180, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            shutil.rmtree(destination, ignore_errors=True)
            raise RuntimeError(f'{name}: fallo al ejecutar Git: {exc}') from exc
        if result.returncode:
            shutil.rmtree(destination, ignore_errors=True)
            raise RuntimeError(f'{name}: fallo al descargar {spec["git_url"]}. '
                               'Comprueba Git y la autenticación de GitHub (gh auth setup-git). '
                               f'{result.stderr.strip()[-400:]}')
        return destination.resolve()
