# Makes PlateBox start by itself when the computer starts (before anyone signs in), and restart if it ever stops.
# Run by the installer, as administrator:   powershell -ExecutionPolicy Bypass -File install-task.ps1 -App "C:\Program Files\PlateBox"
param(
    [Parameter(Mandatory = $true)][string]$App
)
$ErrorActionPreference = 'Stop'

$exe = Join-Path $App 'platebox.exe'
if (-not (Test-Path $exe)) { throw "Not found: $exe" }

$action    = New-ScheduledTaskAction -Execute $exe -WorkingDirectory $App
$trigger   = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = 'PT30S'                                   # let the network come up first
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName 'PlateBox' -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description 'PlateBox: reads number plates from the camera and shows them in a web page.' -Force | Out-Null
Start-ScheduledTask -TaskName 'PlateBox'
Write-Output 'PlateBox will now start with the computer, and it is running.'
