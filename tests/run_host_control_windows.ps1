param(
    [string]$Configuration = 'Release SDL2',
    [string]$Platform = 'x64',
    [string]$PlatformToolset = 'v143'
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repo = Resolve-Path (Join-Path $PSScriptRoot '..')
Push-Location $repo
try {
    perl scripts\update-build-timestamp.pl
    if ($LASTEXITCODE -ne 0) { throw "build timestamp generation failed: $LASTEXITCODE" }

    $expected = (git rev-parse --short=7 HEAD).Trim()
    if (-not $expected) { throw 'git rev-parse returned an empty commit hash' }

    $header = Get-Content -Raw include\build_timestamp.h
    if ($header -notmatch ('GIT_COMMIT_HASH "' + [regex]::Escape($expected) + '"')) {
        throw "build provenance mismatch: expected GIT_COMMIT_HASH $expected"
    }

    $msbuild = Get-Command msbuild -ErrorAction SilentlyContinue
    if (-not $msbuild) {
        $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
        if (-not (Test-Path $vswhere)) { throw 'MSBuild not found and vswhere is unavailable' }
        $installPath = & $vswhere -all -products '*' -property installationPath | Select-Object -First 1
        if (-not $installPath) { throw 'MSBuild not found: no Visual Studio installation reported by vswhere' }
        $msbuild = Get-ChildItem -Path (Join-Path $installPath 'Msbuild') -Filter MSBuild.exe -Recurse -ErrorAction SilentlyContinue |
            Sort-Object FullName | Select-Object -First 1 -ExpandProperty FullName
        if (-not $msbuild) { throw "MSBuild not found under $installPath" }
    }

    & $msbuild -m vs\dosbox-x.sln -t:dosbox-x:Rebuild `
        -p:Configuration="$Configuration" -p:Platform="$Platform" -p:PlatformToolset="$PlatformToolset"
    if ($LASTEXITCODE -ne 0) { throw "MSBuild failed: $LASTEXITCODE" }

    $binary = (Resolve-Path "bin\$Platform\$Configuration\dosbox-x.exe").Path

    & $binary -tests '--gtest_filter=*HostControl*' -set waitonerror=false -set logfile=tests.log
    if ($LASTEXITCODE -ne 0) { throw "native tests failed: $LASTEXITCODE" }

    python -m unittest -v tests.host_control_client_tests
    if ($LASTEXITCODE -ne 0) { throw "client tests failed: $LASTEXITCODE" }

    $env:DOSBOX_X_LIVE_TESTS = '1'
    $env:DOSBOX_X_BINARY = $binary
    python -m unittest -v -k pipe tests.host_control_live_tests
    if ($LASTEXITCODE -ne 0) { throw "live pipe tests failed: $LASTEXITCODE" }

    @{
        commit = (git rev-parse HEAD).Trim()
        shortCommit = $expected
        binary = $binary
        configuration = $Configuration
        platform = $Platform
    } | ConvertTo-Json | Set-Content -Encoding utf8 artifact-provenance.json
}
finally {
    Pop-Location
}
