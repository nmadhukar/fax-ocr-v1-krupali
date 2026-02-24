param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet(
        "bootstrap",
        "format",
        "lint",
        "typecheck",
        "test-quick",
        "test-full",
        "test-regression",
        "review-fast",
        "review-full",
        "smoke-api",
        "smoke-docker"
    )]
    [string]$Task
)

$ErrorActionPreference = "Stop"

$ROOT = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Resolve-Python {
    $candidates = @(
        (Join-Path $ROOT ".venv\Scripts\python.exe"),
        (Join-Path $ROOT "venv\Scripts\python.exe"),
        "python"
    )
    foreach ($candidate in $candidates) {
        if ($candidate -eq "python") {
            return $candidate
        }
        if (Test-Path $candidate) {
            return $candidate
        }
    }
    throw "Python executable not found."
}

function Run-Command {
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [Parameter(Mandatory = $true)][string[]]$Args
    )
    Write-Host ">> $Exe $($Args -join ' ')"
    & $Exe @Args
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Exe $($Args -join ' ')"
    }
}

function Check-Health {
    param([Parameter(Mandatory = $true)][string]$Url)
    Write-Host ">> GET $Url"
    $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 10
    if ($response.StatusCode -lt 200 -or $response.StatusCode -ge 300) {
        throw "Health check failed for $Url with status $($response.StatusCode)"
    }
}

$PYTHON = Resolve-Python

Push-Location $ROOT
try {
    Write-Host "Running task '$Task' in $ROOT"
    Write-Host "Using Python: $PYTHON"

    switch ($Task) {
        "bootstrap" {
            Run-Command $PYTHON @("-m", "pip", "install", "-r", "requirements.txt")
            Run-Command $PYTHON @("-m", "pip", "install", "-e", ".[dev]")
        }
        "format" {
            Run-Command $PYTHON @("-m", "ruff", "check", "--fix", "libs", "services", "workers", "tests")
            Run-Command $PYTHON @("-m", "black", "libs", "services", "workers", "tests")
            Run-Command $PYTHON @("-m", "isort", "libs", "services", "workers", "tests")
        }
        "lint" {
            Run-Command $PYTHON @("-m", "ruff", "check", "libs", "services", "workers", "tests")
            Run-Command $PYTHON @("-m", "black", "--check", "libs", "services", "workers", "tests")
            Run-Command $PYTHON @("-m", "isort", "--check-only", "libs", "services", "workers", "tests")
        }
        "typecheck" {
            Run-Command $PYTHON @("-m", "mypy", "libs", "services", "workers")
        }
        "test-quick" {
            Run-Command $PYTHON @("-m", "pytest", "-q")
        }
        "test-full" {
            Run-Command $PYTHON @("-m", "pytest", "tests", "-v")
        }
        "test-regression" {
            Run-Command $PYTHON @(
                "-m", "pytest", "-q",
                "tests/test_route_regressions.py",
                "tests/test_stage_extraction_regressions.py",
                "tests/test_review_workflow_guards.py"
            )
        }
        "review-fast" {
            Run-Command $PYTHON @("-m", "compileall", "libs", "services", "workers", "tests")
            Run-Command $PYTHON @("-m", "pytest", "-q")
        }
        "review-full" {
            Run-Command $PYTHON @("-m", "ruff", "check", "libs", "services", "workers", "tests")
            Run-Command $PYTHON @("-m", "black", "--check", "libs", "services", "workers", "tests")
            Run-Command $PYTHON @("-m", "isort", "--check-only", "libs", "services", "workers", "tests")
            Run-Command $PYTHON @("-m", "mypy", "libs", "services", "workers")
            Run-Command $PYTHON @("-m", "pytest", "tests", "-v")
        }
        "smoke-api" {
            Check-Health "http://localhost:8001/health"
            Check-Health "http://localhost:8002/health"
            Check-Health "http://localhost:8003/health"
        }
        "smoke-docker" {
            Run-Command "docker" @("compose", "ps")
        }
    }

    Write-Host "Task '$Task' completed successfully."
}
finally {
    Pop-Location
}
