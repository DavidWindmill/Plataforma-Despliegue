"""Monitores internos: un hilo periódico comprueba los servicios desplegados.

No necesitamos HTTP entre monitor y plataforma: todos los objetos están
cargados en el mismo programa. HTTP solo se usa para consultar cada microservicio.
"""
import os
import threading


class MonitorManager:
    def __init__(self, platform):
        self.platform = platform
        self.interval = max(1.0, float(os.getenv('MONITOR_INTERVAL_SECONDS', '4')))
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        """Inicia el bucle de monitorización cuando arranca la web."""
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self.run, daemon=True, name='monitor-manager')
        self.thread.start()

    def stop(self):
        """Detiene el bucle al cerrar la web."""
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)

    def run(self):
        """Descubre servicios en cada vuelta, incluidos los recién redesplegados."""
        while not self.stop_event.is_set():
            self.check_all()
            self.stop_event.wait(self.interval)

    def check_all(self):
        """Comprueba los servicios y solicita adaptación si incumplen su QoS."""
        with self.platform.store.lock:
            targets = [(app, sid, info['runtime']['container_id'])
                       for app, record in self.platform.store.data['apps'].items()
                       if app in self.platform.registry and record['status'] == 'running'
                       for sid, info in record['active'].items()]
        failures = {}
        for app, sid, container_id in targets:
            if self.stop_event.is_set():
                return
            try:
                violations = self.platform.check(app, sid, container_id)
                if violations:
                    failures.setdefault(app, []).append((sid, violations, container_id))
            except Exception as exc:
                self.platform.store.event(app, 'monitor_error', service=sid, error=str(exc))
        # Todas las infracciones de una misma aplicación forman un plan único.
        for app, detected in failures.items():
            try:
                self.platform.on_violations(app, detected)
            except Exception as exc:
                self.platform.store.event(app, 'monitor_error', error=str(exc))
