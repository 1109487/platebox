# Watches this computer's network while PlateBox is tested at a site, and writes what it sees to a log.
# It changes nothing. Start it, then do the test; read the log afterwards (or send it to support).
#   powershell -ExecutionPolicy Bypass -File monitor-site.ps1 [-Minutes 30] [-Log C:\ba-build\site-monitor\monitor.log]
param(
    [int]$Minutes = 30,
    [string]$Log = 'C:\ba-build\site-monitor\monitor.log',
    [string]$CameraMacPrefix = 'f8-20-97',
    [int]$SftpPort = 2222
)
$ErrorActionPreference = 'SilentlyContinue'
New-Item -ItemType Directory -Force (Split-Path $Log) | Out-Null

function Say($text) { Add-Content -Path $Log -Value ((Get-Date -Format 'HH:mm:ss') + '  ' + $text) -Encoding utf8 }

function Wifi {
    $t = netsh wlan show interfaces | Out-String
    $ssid = if ($t -match '(?m)^\s+SSID\s+:\s+(.+)$') { $Matches[1].Trim() } else { '-' }
    $state = if ($t -match '(?m)^\s+State\s+:\s+(.+)$') { $Matches[1].Trim() } else { '-' }
    "$ssid ($state)"
}

function MyAddress {
    (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notmatch '^(127|169\.254|172\.2[0-9])\.' -and $_.PrefixOrigin -ne 'WellKnown' } |
        ForEach-Object { $_.IPAddress }) -join ', '
}

function Sweep($mine) {
    # touch every address on our /24 so the ARP table lists whoever is there, then pick out the camera by its maker prefix
    $net = ($mine -split '\.')[0..2] -join '.'
    $pings = 1..254 | ForEach-Object { $p = New-Object System.Net.NetworkInformation.Ping; $p.SendPingAsync("$net.$_", 400) }
    try { [void][System.Threading.Tasks.Task]::WaitAll($pings, 5000) } catch {}
    $found = @()
    foreach ($line in (arp -a)) {
        if ($line -match '^\s*(\d+\.\d+\.\d+\.\d+)\s+([0-9a-f-]{17})\s+dynamic') {
            if ($Matches[2] -like "$CameraMacPrefix*") { $found += $Matches[1] }
        }
    }
    $found
}

function Sftp {
    $rows = netstat -ano -p tcp | Select-String ":$SftpPort\s" | Where-Object { $_.Line -notmatch 'LISTENING' }
    @($rows | ForEach-Object { $c = $_.Line -split '\s+' | Where-Object { $_ }; "$($c[2])/$($c[3])" })
}

Say "=== monitor started, $Minutes minutes, log $Log ==="
Say "PlateBox web: $((Invoke-WebRequest http://localhost/healthz -UseBasicParsing -TimeoutSec 4).StatusCode)   listening: $((netstat -ano | Select-String 'LISTENING' | Select-String ":($SftpPort|80) " | ForEach-Object { ($_.Line -split '\s+' | Where-Object { $_ })[1] }) -join ', ')"
$end = (Get-Date).AddMinutes($Minutes)
$last = ''; $lastSweep = [datetime]::MinValue; $cameras = @(); $seen = @{}; $tick = 0

while ((Get-Date) -lt $end) {
    $tick++
    $now = "Wi-Fi $(Wifi) | this computer $(MyAddress)"
    if ($now -ne $last) { Say "NETWORK CHANGED: $now"; $last = $now; $lastSweep = [datetime]::MinValue }

    if (((Get-Date) - $lastSweep).TotalSeconds -ge 20) {
        $lastSweep = Get-Date
        $mine = (MyAddress -split ', ')[0]
        if ($mine) {
            $cameras = @(Sweep $mine)
            if ($cameras.Count) {
                foreach ($c in $cameras) {
                    $web = (New-Object System.Net.Sockets.TcpClient).ConnectAsync($c, 443).Wait(1500)
                    Say "camera at $c  (its web page answers: $web)"
                }
            } else { Say "no camera ($CameraMacPrefix...) found on this network" }
        }
    }

    foreach ($row in (Sftp)) {
        if (-not $seen.ContainsKey($row)) { $seen[$row] = Get-Date; Say "CAMERA CONNECTION on $SftpPort : $row" }
    }
    if ($tick % 20 -eq 0) {
        Say ("alive: PlateBox web " + (Invoke-WebRequest http://localhost/healthz -UseBasicParsing -TimeoutSec 4).StatusCode + ", connections seen so far: " + $seen.Count)
    }
    Start-Sleep -Milliseconds 700
}
Say "=== monitor finished. Distinct connections to port ${SftpPort}: $($seen.Count) ==="
