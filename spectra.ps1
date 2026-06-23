$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
& "$ScriptDir\scripts\launchers\spectra.ps1" @args
exit $LASTEXITCODE
