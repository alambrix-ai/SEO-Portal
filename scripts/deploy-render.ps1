# Creates free-tier Render API + static console from local .env.
# Usage: set RENDER_API_KEY in the environment, then run this script.
$ErrorActionPreference = "Stop"
$apiKey = $env:RENDER_API_KEY
if (-not $apiKey) { throw "Set RENDER_API_KEY first" }

$ownerId = "tea-dago4a95efls73bd191g"
$repo = "https://github.com/alambrix-ai/SEO-Portal"
$envPath = Join-Path (Split-Path $PSScriptRoot -Parent) "backend\.env"

function Get-DotEnv([string]$path) {
  $map = @{}
  Get-Content $path | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#")) { return }
    $i = $line.IndexOf("=")
    if ($i -lt 1) { return }
    $k = $line.Substring(0, $i).Trim()
    $v = $line.Substring($i + 1).Trim()
    if ($v.StartsWith('"') -and $v.EndsWith('"')) { $v = $v.Substring(1, $v.Length - 2) }
    $map[$k] = $v
  }
  return $map
}

function Invoke-Render([string]$method, [string]$path, $body = $null) {
  $headers = @{
    Authorization = "Bearer $apiKey"
    Accept = "application/json"
  }
  $uri = "https://api.render.com/v1$path"
  if ($null -eq $body) {
    return Invoke-RestMethod -Method $method -Uri $uri -Headers $headers
  }
  $json = $body | ConvertTo-Json -Depth 20 -Compress
  return Invoke-RestMethod -Method $method -Uri $uri -Headers $headers -ContentType "application/json" -Body $json
}

$cfg = Get-DotEnv $envPath
$required = @(
  "DATABASE_URL","SECRET_KEY","MASTER_ENCRYPTION_KEY","BLIND_INDEX_KEY",
  "LLM_MODEL","ANTHROPIC_API_KEY","SMTP_HOST","SMTP_USER","SMTP_PASSWORD","EMAIL_FROM"
)
foreach ($k in $required) {
  if (-not $cfg[$k]) { throw "Missing $k in backend/.env" }
}

Write-Host "Creating automarket-api (Docker, free)..."
$apiBody = @{
  type = "web_service"
  name = "automarket-api"
  ownerId = $ownerId
  repo = $repo
  branch = "main"
  autoDeploy = "yes"
  envVars = @(
    @{ key = "APP_ENV"; value = "production" },
    @{ key = "POSTGRES_SSLMODE"; value = "require" },
    @{ key = "FORCE_HTTPS"; value = "true" },
    @{ key = "SECURE_COOKIES"; value = "true" },
    @{ key = "RATE_LIMIT_ENABLED"; value = "true" },
    @{ key = "DATABASE_URL"; value = $cfg["DATABASE_URL"] },
    @{ key = "PUBLIC_BASE_URL"; value = "https://automarket-console.onrender.com" },
    @{ key = "TRUSTED_HOSTS"; value = "automarket-api.onrender.com" },
    @{ key = "CORS_ORIGINS"; value = "https://automarket-console.onrender.com" },
    @{ key = "SECRET_KEY"; value = $cfg["SECRET_KEY"] },
    @{ key = "MASTER_ENCRYPTION_KEY"; value = $cfg["MASTER_ENCRYPTION_KEY"] },
    @{ key = "BLIND_INDEX_KEY"; value = $cfg["BLIND_INDEX_KEY"] },
    @{ key = "SCHEDULER_ENABLED"; value = "true" },
    @{ key = "SCHEDULER_SPEED"; value = "1.0" },
    @{ key = "CELERY_ENABLED"; value = "false" },
    @{ key = "LLM_PROVIDER"; value = "anthropic" },
    @{ key = "LLM_MODEL"; value = $cfg["LLM_MODEL"] },
    @{ key = "ANTHROPIC_API_KEY"; value = $cfg["ANTHROPIC_API_KEY"] },
    @{ key = "SMTP_HOST"; value = $cfg["SMTP_HOST"] },
    @{ key = "SMTP_PORT"; value = $(if ($cfg["SMTP_PORT"]) { $cfg["SMTP_PORT"] } else { "587" }) },
    @{ key = "SMTP_USER"; value = $cfg["SMTP_USER"] },
    @{ key = "SMTP_PASSWORD"; value = $cfg["SMTP_PASSWORD"] },
    @{ key = "EMAIL_FROM"; value = $cfg["EMAIL_FROM"] },
    @{ key = "EMAIL_FROM_NAME"; value = $(if ($cfg["EMAIL_FROM_NAME"]) { $cfg["EMAIL_FROM_NAME"] } else { "AutoMarket AI" }) }
  )
  serviceDetails = @{
    runtime = "docker"
    plan = "free"
    region = "oregon"
    healthCheckPath = "/health"
    envSpecificDetails = @{
      dockerCommand = 'sh -c "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers"'
      dockerContext = "./backend"
      dockerfilePath = "./backend/Dockerfile"
    }
  }
}

$apiResp = Invoke-Render POST "/services" $apiBody
$apiService = $apiResp.service
$apiUrl = $apiService.serviceDetails.url
Write-Host "API service id: $($apiService.id)"
Write-Host "API URL: $apiUrl"

Write-Host "Creating automarket-console (static, free)..."
$consoleBody = @{
  type = "static_site"
  name = "automarket-console"
  ownerId = $ownerId
  repo = $repo
  branch = "main"
  autoDeploy = "yes"
  envVars = @(
    @{ key = "VITE_API_BASE_URL"; value = "$apiUrl/api/v1" }
  )
  serviceDetails = @{
    buildCommand = "cd frontend && npm ci && npm run build"
    publishPath = "frontend/dist"
    routes = @(
      @{ type = "rewrite"; source = "/*"; destination = "/index.html" }
    )
  }
}

$consoleResp = Invoke-Render POST "/services" $consoleBody
$consoleService = $consoleResp.service
$consoleUrl = $consoleService.serviceDetails.url
Write-Host "Console service id: $($consoleService.id)"
Write-Host "Console URL: $consoleUrl"

# Fix cross-service URLs to the real assigned hostnames.
$apiHost = ([uri]$apiUrl).Host
$consoleOrigin = $consoleUrl.TrimEnd("/")

Write-Host "Updating API public URLs to match assigned hostnames..."
# Fetch current env keys first — Render PUT replaces the whole set.
$existing = Invoke-Render GET "/services/$($apiService.id)/env-vars"
$merged = @{}
foreach ($item in $existing) {
  if ($item.envVar.key -and $null -ne $item.envVar.value) {
    $merged[$item.envVar.key] = $item.envVar.value
  }
}
$merged["PUBLIC_BASE_URL"] = $consoleOrigin
$merged["TRUSTED_HOSTS"] = $apiHost
$merged["CORS_ORIGINS"] = $consoleOrigin
$envUpdate = @()
foreach ($k in $merged.Keys) {
  $envUpdate += @{ key = $k; value = $merged[$k] }
}
Invoke-Render PUT "/services/$($apiService.id)/env-vars" $envUpdate | Out-Null
Invoke-Render POST "/services/$($apiService.id)/deploys" @{} | Out-Null

Write-Host ""
Write-Host "DONE"
Write-Host "API:     $apiUrl"
Write-Host "Console: $consoleUrl"
Write-Host "Deploys are building now in the Render dashboard."
