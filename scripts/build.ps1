param([switch]$Clean)
$ErrorActionPreference = 'Stop'
Push-Location (Split-Path $PSScriptRoot -Parent)
try {
    $buildPath = Join-Path (Get-Location).Path 'build'
    if ($Clean -and (Test-Path -LiteralPath $buildPath)) {
        $resolved = (Resolve-Path -LiteralPath $buildPath).Path
        if ($resolved -ne $buildPath) { throw "Unexpected build path: $resolved" }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
    & cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo
    if ($LASTEXITCODE -ne 0) { throw 'CMake configure failed' }
    & cmake --build build --parallel 4
    if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
} finally { Pop-Location }
