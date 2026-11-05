# Guía: estructura OAS/REQ sin catálogo adicional

Una aplicación se define mediante los YAML **ya existentes** dentro de `OAS/`
y `REQ/`, más el JSON inicial `app.json`. No existe `catalog.yml`.

- **OAS/<categoria>.yml**: se conservan los contratos OpenAPI y se añade
  `x-deployment`. Dentro de cada alternativa figuran los repositorios GitHub,
  la ubicación del Dockerfile dentro de la descarga, las variables de entorno,
  los enlaces a otras instancias y las rutas de salud y estadísticas.
- **REQ/<categoria>.yml**: la sección original `alternatives` contiene la lista
  de implementaciones disponibles, sus requisitos y los límites de comportamiento.
- **app.json**: solo `services: [{id, alternative}]`; se utiliza exactamente el
  mismo formato al calcular redespliegues.

El identificador de categoría procede del nombre del YAML, por ejemplo
`REQ/search.yml` y `OAS/search.yml`. Para desplegar `search00` con
`binarysearch-1`, la plataforma consulta REQ/search para sus límites y OAS/search
para el repositorio y el endpoint del monitor. Si hay fallo, el planificador
selecciona `groveralg-1` en REQ/search, y Docker descarga solo los nuevos
repositorios definidos en OAS/search.

Para desplegar una nueva aplicación, empaqueta exclusivamente:

```text
OAS/*.yml
REQ/*.yml
app.json
```

El archivo ZIP debe tener nombre significativo, porque se usa para identificar
la aplicación al importarla. Por seguridad, solo se admiten URL HTTPS con
anfitrión `github.com`, referencias de Git y rutas de contexto relativas a
la raíz del repositorio clonado. No se necesita ningún directorio de servicios
preinstalado junto a la plataforma.

Lee `README.md` para conocer cómo se interpretan `reliability`, `availability`
y `execution_time`, y las limitaciones de la simulación de QPU.
