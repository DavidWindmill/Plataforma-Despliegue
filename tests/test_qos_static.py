"""Comprobaciones de que los límites viajan solo hacia los monitores."""
from pathlib import Path
from types import SimpleNamespace
import pytest
from control_plane.backends import DockerBackend


def test_backend_no_inyecta_limites_qos(tmp_path, monkeypatch):
    backend = DockerBackend()
    calls = []
    monkeypatch.setattr(backend, "ensure_network", lambda app: None)
    monkeypatch.setattr(backend, "_container_info", lambda name: "")
    monkeypatch.setattr(backend, "discover", lambda app, sid: True)
    def fake_cmd(*args, **kwargs):
        calls.append(args)
        if args[0] == "run": return "idcontenedor"
        if args[0] == "port": return "127.0.0.1:18081"
        return ""
    monkeypatch.setattr(backend, "cmd", fake_cmd)
    class Response:
        status_code = 200
        is_success = True
        def json(self): return {'status':'ok', 'execution_time':5}
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def get(self,*args): return Response()
    monkeypatch.setattr("control_plane.backends.httpx.Client", Client)
    (tmp_path/"Dockerfile").write_text("FROM scratch")
    srv = SimpleNamespace(id="sensor00", machine="docker", definition={
        "repository":"test", "port":8080, "environment":{"SENSOR_ID":"sensor00"},
        "bindings":{}, "monitor":{"health_path":"/health", "metrics_path":"/metrics",
        "limits":{"execution_time":{"max":15}}}})
    backend.start("app-a",srv,tmp_path)
    run_args = next(a for a in calls if a[0] == "run")
    assert "-e" in run_args and "SENSOR_ID=sensor00" in run_args
    assert "QOS_LIMITS_JSON" not in str(run_args)
    assert "QOS_FAULT_" not in str(run_args)


def test_backend_rechaza_configuracion_simulacion_desde_oas(tmp_path,monkeypatch):
    backend = DockerBackend()
    monkeypatch.setattr(backend,"ensure_network", lambda app:None)
    monkeypatch.setattr(backend,"_container_info",lambda name:"")
    monkeypatch.setattr(backend,"cmd",lambda *a,**kw: "")
    (tmp_path/"Dockerfile").write_text("FROM scratch")
    srv = SimpleNamespace(id="sensor00",machine="docker", definition={
        "repository":"test", "port":8080,"environment":{"QOS_LIMITS_JSON":"{}"},
        "bindings":{}, "monitor":{"health_path":"/health"}})
    with pytest.raises(ValueError,match="internos"):
        backend.start("app-a",srv,tmp_path)
