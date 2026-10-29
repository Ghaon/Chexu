param(
    [ValidateSet('start', 'stop', 'status', 'shell')]
    [string]$Action = 'status',
    [string]$InstallRoot = 'E:\PostgreSQL'
)

$ErrorActionPreference = 'Stop'
$binDirectory = Join-Path $InstallRoot '18.6\pgsql\bin'
$dataDirectory = Join-Path $InstallRoot 'data\chexu'
$logDirectory = Join-Path $InstallRoot 'logs'
$pgControl = Join-Path $binDirectory 'pg_ctl.exe'

if (-not (Test-Path -LiteralPath $pgControl)) {
    throw "PostgreSQL executable not found: $pgControl"
}

switch ($Action) {
    'start' {
        & $pgControl status -D $dataDirectory *> $null
        if ($LASTEXITCODE -eq 0) {
            Write-Output 'PostgreSQL is already running.'
            exit 0
        }

        # Hide the background helper and keep server output on the E drive.
        $serverLog = Join-Path $logDirectory 'postgresql.log'
        $startArguments = @('start', '-D', "`"$dataDirectory`"", '-l', "`"$serverLog`"", '-w', '-t', '30')
        $startProcess = Start-Process -FilePath $pgControl -ArgumentList $startArguments `
            -WorkingDirectory $InstallRoot -WindowStyle Hidden -PassThru
        if (-not $startProcess.WaitForExit(35000)) {
            throw "PostgreSQL startup timed out. See $serverLog"
        }
        if ($startProcess.ExitCode -ne 0) {
            throw "PostgreSQL could not start. See $serverLog"
        }
        Write-Output 'PostgreSQL is listening on 127.0.0.1:5432.'
    }
    'stop' {
        & $pgControl stop -D $dataDirectory -m fast -w -t 30
        exit $LASTEXITCODE
    }
    'status' {
        & $pgControl status -D $dataDirectory
        exit $LASTEXITCODE
    }
    'shell' {
        $previousPassFile = $env:PGPASSFILE
        try {
            $env:PGPASSFILE = Join-Path $InstallRoot 'secrets\chexu.pgpass'
            & (Join-Path $binDirectory 'psql.exe') -X --no-password `
                -h 127.0.0.1 -p 5432 -U chexu -d chexu
            $shellExitCode = $LASTEXITCODE
        }
        finally {
            $env:PGPASSFILE = $previousPassFile
        }
        exit $shellExitCode
    }
}
