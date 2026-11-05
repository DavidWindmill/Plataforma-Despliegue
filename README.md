# TFG · Plataforma sencilla de despliegue con OAS y REQ

La plataforma comienza **sin aplicaciones**. Desde la web permite añadir un ZIP de
especificaciones, analizarlo, desplegar los servicios de los repositorios GitHub,
monitorizarlos y sustituir automáticamente una implementación que incumple los
límites especificados en REQ. No se ejecutan microservicios desde directorios
locales, ni se descarga un repositorio al importar el ZIP.

## 1. Arranque en Windows

1. Necesitas **Python 3.12+**, **Git** y **Docker Desktop** con Docker arrancado.
2. Los repositorios GitHub del ejemplo pueden ser privados. Configura el acceso:

   ```powershell
   gh auth login
   gh auth setup-git
   git ls-remote https://github.com/DavidWindmill/app-a-sensor.git HEAD
   ```

   Si la última orden falla, el despliegue no podrá descargar ese servicio.
   No incluyas tokens ni contraseñas en los archivos YAML.
3. Entra en la carpeta de la plataforma y ejecuta `start_platform.bat`.
   La web estará en **http://127.0.0.1:8000**.
4. Pulsa **Añadir aplicación** y elige `examples/app-a.zip` o `examples/app-b.zip`.
5. Pulsa **Analizar** y después **Desplegar**. Solo en el paso de despliegue se
   descargan los repositorios Git necesarios. El monitor arranca con la web y
   encuentra los contenedores nuevos automáticamente.
6. Cuando una métrica supera los límites de REQ, el monitor llama al
   planificador Python, este escoge otra alternativa de la misma categoría,
   genera un nuevo JSON y vuelve a invocar el análisis y el despliegue.

En la **pantalla inicial**, cada tarjeta ofrece **Desplegar**, **Detener**, **Borrar**
y **Ver estado y QoS**. Estos botones utilizan las mismas operaciones que
la vista de detalles. **Borrar** confirma antes de detener y eliminar una aplicación,
mientras que **Detener** conserva sus OAS, REQ y JSON inicial para volver a desplegarla.

### Simulación de fallos de QoS (solo servicios de ejemplo)

Los servicios aportados incluyen su propio `qos_faults.py`: generan sus
métricas y posibles desviaciones al consultar `/metrics`. La plataforma no
contiene un generador de fallos, no decide su frecuencia o probabilidad ni
llama al endpoint `/admin/simulate` de estos servicios.

**Los umbrales de simulación están fijados como constantes** en el `qos_faults.py`
de cada repositorio de microservicio. La plataforma no envía `QOS_LIMITS_JSON`
ni ninguna variable `QOS_FAULT_*`. El simulador decide autónomamente cuándo
alterar las métricas publicadas en `/metrics`.

Las variantes que comparten repositorio (por ejemplo, los tres sensores) tienen
límites estáticos distintos dentro del propio código. `SERVICE_VARIANT`, declarada
en OAS, identifica la implementación elegida, **no** transmite requisitos.
Los límites usados para evaluar los incumplimientos **solo** se obtienen de REQ.
Mantén sincronizados los valores estáticos del ejemplo con REQ si los editas.

> **Importante:** la plataforma descarga los repositorios desde GitHub durante
> el despliegue. Los cambios de `repositorios_modificados/` deben publicarse en
> dichos repositorios antes de probarlos mediante la web. No se usan rutas locales
> como alternativa de despliegue.


## 2. ZIP de la aplicación: exactamente OAS, REQ y un JSON

```text
app-a.zip
├── app.json             # SOLO identificador de instancia + alternativa inicial
├── OAS/
│   ├── sensor.yml
│   ├── aggregator.yml
│   ├── search.yml        # código de búsqueda clásica y cuántica en GitHub
│   └── result_processing.yml
└── REQ/
    ├── sensor.yml
    ├── aggregator.yml
    ├── search.yml        # binarysearch-1 y groveralg-1; limites distintos
    └── result_processing.yml
```

**No** hacen falta `platform.json`, `catalog.yml`, `qos.json` ni la carpeta `plans/`.
El nombre del archivo ZIP determina el identificador de la aplicación en la web
(por ejemplo, `app-a.zip` se registra como `app-a`). El ZIP solo contiene
especificaciones, **nunca** copias del código fuente de los servicios.

`app.json` no contiene URLs, rutas, especificaciones ni máquinas:

```json
{
  "services": [
    {"id": "sensor00", "alternative": "sensor-1"},
    {"id": "aggregator00", "alternative": "aggregator-1"},
    {"id": "search00", "alternative": "binarysearch-1"},
    {"id": "resultprocessing10", "alternative": "resultprocessing-1"}
  ]
}
```

El ejemplo real añade también `sensor10` y `sensor20`.

## 3. OAS: repositorio, conectividad y rutas

Los archivos OpenAPI **originales** se conservan, y se amplían con las rutas que
sí tienen las implementaciones en GitHub (`/health` y `/metrics`) y con el bloque
estándar de extensión `x-deployment`. Ejemplo simplificado de `OAS/search.yml`:

```yaml
x-deployment:
  port: 8080
  monitoring:
    health_path: /health
    metrics_path: /metrics
  alternatives:
    binarysearch-1:
      components:
        - suffix: ""
          repository:
            url: https://github.com/DavidWindmill/app-a-classical-detector.git
            ref: main
            docker_context: .
    groveralg-1:
      components:
        - suffix: ""
          repository:
            url: https://github.com/DavidWindmill/app-a-hybrid-gateway.git
            ref: main
            docker_context: .
          bindings:
            QPU_URL: $id-qpu
        - suffix: -qpu
          repository:
            url: https://github.com/DavidWindmill/app-a-quantum-detector.git
            ref: main
            docker_context: .
```

La alternativa cuántica necesita **pasarela + detector cuántico** en este caso
concreto. El JSON solo nombra `search00` y `groveralg-1`: el analizador crea los
contenedores `search00` y `search00-qpu`. `$id` representa el identificador de
la instancia. Las conexiones entre servicios (`AGGREGATOR_URL`, `DETECTOR_URL`,
`QPU_URL`...) se configuran en los mismos OAS y utilizan nombres DNS Docker.

`docker_context` no es una fuente local: indica dónde está el Dockerfile **una
vez clonado** el repositorio Git. No se aceptan `local_path` ni `file://`.

Para cada alternativa de un REQ debe existir una alternativa homónima en el OAS
de su categoría. El monitor no tiene codificadas rutas de servicios: lee
`health_path` y `metrics_path` del OAS correspondiente.

## 4. REQ: alternativas y limites del monitor

Los REQ originales SearchApp se conservan intactos. La plataforma toma las
alternativas de cada `REQ/<categoria>.yml`, por ejemplo:

```yaml
alternatives:
  binarysearch-1:
    behaviour:
      execution_time: 1547
      availability: 24
      reliability: 0.15
  groveralg-1:
    behaviour:
      execution_time: 1.3
      availability: 24
      reliability: 0.12
```

**Interpretación implementada:**

- `execution_time`: valor máximo permitido.
- `availability`: valor mínimo permitido.
- `reliability`: valor máximo permitido, interpretado aquí como indicador de
  riesgo/tasa de fallo en el ejemplo (por sus valores cercanos a 0).

Estos son los campos numéricos que el monitor compara con `/metrics`. No se
inventan límites para métricas que no figuran en REQ. `minimum_hw_req`,
`mode`, `requests`, `shots` y `depth` se conservan en REQ, pero **no se utilizan
para decidir capacidad física del host** en esta versión Docker simplificada;
eso requeriría un gestor de recursos adicional. La QPU del ejemplo ejecuta
una simulación con Qiskit, no reserva hardware cuántico real.

Si una categoría tiene un solo elemento en `alternatives` no existe sustituto
válido: la plataforma deja constancia de que no pudo planificar otro servicio,
pero no inventa uno. Si tiene varios, elige aleatoriamente uno distinto del
actual, incluso cuando se reutiliza el mismo código con parámetros diferentes.

## 5. Componentes (comunicación directa por Python)

- `control_plane/api.py`: web y botones HTTP.
- `control_plane/importer.py`: descomprime el ZIP, sin clonar Git.
- `control_plane/specifications.py`: carga OAS y REQ.
- `control_plane/plan.py`: convierte el JSON en objetos `Service` y compara planes.
- `control_plane/repositories.py`: hace Git clone **solo al desplegar**.
- `control_plane/engine.py`: coordina análisis, Docker y adaptación.
- `control_plane/backends.py`: operaciones Docker y HTTP hacia microservicios.
- `control_plane/monitor.py`: hilo periódico interno sin peticiones HTTP a otros
  componentes de la plataforma.
- `control_plane/planning.py`: selección aleatoria desde `REQ.alternatives`.
- `control_plane/store.py`: conserva estados, eventos y planes generados.

**Ciclo:** importador → analizador → desplegador → monitor → planificador →
analizador → desplegador → monitor... Los nuevos planes se guardan en
`data/generated_plans/<app>/*.json` con **el mismo esquema** de `app.json`.

## 6. Pruebas y limites de la validación

```powershell
python -m pytest -q tests
```

Las pruebas simulan Docker y Git, cubren la importación y la API web, y verifican
la sustitución clásica → cuántica sin volver a descargar los servicios
conservados. Se incluye además `scripts/verify_docker.py` para probar el sistema
contra Docker real una vez autorizados los repositorios.

**No se ha confirmado aquí un despliegue real**: para esa validación se necesita
Docker operativo y acceso a todos los repositorios remotos. Importar YAML de
origen desconocido puede llevar a ejecutar Dockerfiles ajenos; usa únicamente
repositorios en los que confíes.


## Actualización: fallos menos frecuentes y redespliegue comprobado

- **Fallos autónomos:** los nueve servicios de ejemplo generan sus desviaciones en
  `qos_faults.py`. Cada contenedor tiene una oportunidad cada **240–480 s**
  (4–8 min), con **6 % de probabilidad** en cada oportunidad. No es una
  periodicidad fija ni se garantiza un fallo en ese intervalo. La degradación
  dura **12–20 s**. Los valores de QoS se mantienen estáticos en cada servicio.
- **Sin falsos fallos por jitter:** las métricas limitadas por REQ ya no sufren
  oscilaciones añadidas por `qos_faults.py` fuera de las degradaciones. Las
  mediciones reales fuera de límite siguen siendo detectadas.
- **Despliegue real:** la plataforma elimina con `docker rm -f` los contenedores
  cambiados, inicia otros mediante `docker run` y no confirma el plan hasta que
  `/health` y `/metrics` de OAS responden. El nombre lógico puede ser igual:
  la web muestra **alternativa activa** e **ID del nuevo contenedor**.
- **Varios fallos a la vez:** el monitor recopila todas las violaciones de una
  ronda por aplicación y el planificador crea un único plan con los cambios
  posibles. Si una categoría no tiene alternativa en REQ, se registra el hecho
  sin inventar una.
- **Si el nuevo despliegue falla:** se intenta volver al plan anterior,
  restaurando solo los contenedores afectados. Los demás siguen en funcionamiento.
  Si incluso la reversión falla, la app queda en estado **error** y los monitores
  dejan de intervenir hasta corregirla o detenerla. Tras un intento fallido,
  el mismo servicio lógico espera 120 s antes de otro intento.
- **Pantalla principal:** botones **Desplegar todas**, **Desplegar**, **Detener**,
  **Borrar** y **Renombrar** (solo cambia el nombre mostrado).

### Importante: publicar las nuevas versiones de los microservicios

La plataforma **siempre descarga las fuentes de las URLs de GitHub indicadas en
OAS**. Los nueve cambios en `qos_faults.py` de la entrega no tendrán efecto en
Docker hasta que se suban a esas ramas (habitualmente `main`).

En cada repositorio modificado ejecuta (desde su carpeta Git original):

```powershell
# Repetir dentro de cada repositorio modificado
git add .
git commit -m "Reducir frecuencia de fallos QoS autonomos"
git push origin main
```

Antes de probar la nueva versión, **detén las apps desde la web y cierra la
plataforma**. Elimina las copias clonadas anteriores desde
`Plataforma-Despliegue/data/checkouts/` (solo ese caché, **no** las carpetas
`imported/`, `configs/` ni `data/state.json`) y vuelve a iniciarla. Así la
siguiente ejecución volverá a descargar GitHub y reconstruirá las imágenes.

Pruebas automáticas:

```powershell
cd Plataforma-Despliegue
python -m pytest -q
```

La prueba integrada usa un backend Docker controlado y otra prueba verifica la
composición de `docker rm` y `docker run` con un intérprete de CLI simulado.
**No sustituyen una prueba extremo a extremo en un equipo con Docker Desktop**.

### Cómo comprobar que el cambio no es solo visual

En un terminal, mientras la aplicación se está ejecutando:

```powershell
docker ps --filter "label=deployment.platform.managed=true" --format "table {{.Names}}\t{{.ID}}\t{{.Image}}"
```

Al producirse un cambio, el ID de `dp-<app>-search00` debe ser distinto al
anterior y aparecerá la nueva alternativa en el detalle de la web. El **nombre
Docker de `search00` se reutiliza deliberadamente** para conservar la dirección
interna de los otros microservicios. En un cambio a Grover aparece también
`dp-<app>-search00-qpu`. El historial muestra los identificadores retirados y
creados una vez terminado el procedimiento.

Recuerda subir también a GitHub los cambios de `Plataforma-Despliegue/` si
quieres mantener el repositorio remoto sincronizado con esta entrega.
