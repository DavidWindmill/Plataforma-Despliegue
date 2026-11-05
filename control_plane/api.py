"""Web y API: convierte los botones del navegador en llamadas a la plataforma."""
from pathlib import Path
from contextlib import asynccontextmanager
import os
import subprocess
from fastapi import FastAPI, HTTPException, UploadFile, File
from pydantic import BaseModel
from fastapi.responses import FileResponse, JSONResponse
from fastapi.concurrency import run_in_threadpool
from .engine import Platform
from .monitor import MonitorManager
from .importer import import_application

ROOT = Path(__file__).resolve().parent.parent
platform = Platform(ROOT, Path(os.getenv('DEPLOYMENT_DATA_DIR', str(ROOT / 'data'))))
monitors = MonitorManager(platform)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Recupera despliegues anteriores y arranca los monitores internos."""
    await run_in_threadpool(platform.recover_all)
    monitors.start()
    yield
    monitors.stop()


app = FastAPI(title='Plataforma de despliegue', lifespan=lifespan)


@app.exception_handler(KeyError)
async def missing(req, exc):
    return JSONResponse(status_code=404, content={'detail':str(exc)})


@app.exception_handler(ValueError)
async def invalid(req, exc):
    return JSONResponse(status_code=422, content={'detail':str(exc)})


@app.exception_handler(RuntimeError)
async def failed(req, exc):
    return JSONResponse(status_code=503, content={'detail':str(exc)})


@app.get('/')
def index():
    return FileResponse(ROOT / 'web/index.html')


@app.get('/health')
def health():
    return {'status':'ok'}


@app.get('/api/system')
def system():
    """Información básica de Docker y de los monitores."""
    try:
        result = subprocess.run(['docker','info','--format','{{.ServerVersion}}'],
                                capture_output=True, text=True, timeout=10)
        available = result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        available = False
    return {'docker_available': available,
            'monitor_worker_connected': bool(monitors.thread and monitors.thread.is_alive()),
            'app_count': len(platform.list_apps())}


@app.get('/api/apps')
def apps():
    return platform.list_apps()


@app.post('/api/apps/import')
async def add_application(file: UploadFile = File(...)):
    """Recibe un ZIP con REQ/, OAS/ y SOLO el JSON inicial."""
    # TemporaryDirectory no bloquea reabrir el ZIP en Windows.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='deployment-upload-') as dirname:
        path = Path(dirname) / 'uploaded.zip'
        size = 0
        with path.open('wb') as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 10_000_000:
                    raise HTTPException(413, 'Paquete demasiado grande')
                output.write(chunk)
        return await run_in_threadpool(import_application, platform, path, file.filename)



@app.post('/api/apps/deploy-all')
async def deploy_all_apps():
    """Despliega las aplicaciones una a una; un fallo no bloquea al resto."""
    return await run_in_threadpool(platform.deploy_all)


class RenameApp(BaseModel):
    display_name: str


@app.patch('/api/apps/{app_id}/name')
async def rename_application(app_id: str, payload: RenameApp):
    """Cambia el nombre mostrado sin afectar al identificador Docker."""
    return await run_in_threadpool(platform.rename_app, app_id, payload.display_name)


@app.get('/api/apps/{app_id}')
def details(app_id: str):
    return platform.details(app_id)


@app.delete('/api/apps/{app_id}')
async def delete_application(app_id: str):
    """Detiene y elimina una aplicación del catálogo."""
    return await run_in_threadpool(platform.remove_app, app_id)


@app.get('/api/apps/{app_id}/analysis')
def analyze(app_id: str):
    return {'operations':platform.analyze(app_id)}


@app.post('/api/apps/{app_id}/deploy')
async def deploy(app_id: str):
    return await run_in_threadpool(platform.apply, app_id)


@app.post('/api/apps/{app_id}/stop')
async def stop(app_id: str):
    return await run_in_threadpool(platform.stop, app_id)



@app.post('/api/apps/{app_id}/replan')
async def replan(app_id: str):
    """Pide un plan nuevo al planificador, que consulta REQ y cambia una alternativa."""
    return await run_in_threadpool(platform.replan, app_id, None, 'manual_replan')
