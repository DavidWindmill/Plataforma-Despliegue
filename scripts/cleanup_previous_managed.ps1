# Limpiar solamente los contenedores gestionados por esta plataforma
# para las apps app-a y app-b. No toca otros contenedores Docker.
$ErrorActionPreference = 'Stop'
foreach ($app in @('app-a','app-b')) {
    $names = @(docker ps -aq --filter "label=deployment.platform.app=$app")
    foreach ($id in $names) {
        if ($id) {
            Write-Host "Eliminando contenedor anterior $app : $id"
            docker rm -f $id
        }
    }
    $net = "dp-$app"
    $label = docker network inspect $net --format '{{index .Labels "deployment.platform.managed"}}' 2>$null
    if ($LASTEXITCODE -eq 0 -and $label -eq 'true') {
        docker network rm $net
    }
}
Write-Host 'Limpieza finalizada. No se han borrado imagenes ni volumenes.'
