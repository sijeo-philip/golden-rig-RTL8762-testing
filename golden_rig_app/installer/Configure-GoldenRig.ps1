<#
.SYNOPSIS
    Configure the Golden Rig PC: rig ID, mDNS name, computer name, certificates, firewall.

.DESCRIPTION
    Called by the installer, and from the Start menu shortcut "Configure Golden Rig" to change
    settings later. Run without parameters for interactive prompts. Self-elevates (admin needed
    for renaming the computer, firewall rules and certificate permissions).

    Data folder (default %ProgramData%\GoldenRig):
        rig_config.json   application configuration
        slots.json        slot -> COM port binding
        certs\            rig.pem, rig.key, golden_rig_ca.pem, rig.csr, rig.sha256.txt
        logs\             daily rotated logs

.EXAMPLE
    .\Configure-GoldenRig.ps1
    Interactive.

.EXAMPLE
    .\Configure-GoldenRig.ps1 -RigId GOLDEN-RIG-02 -MdnsName golden-rig-02 -RenameComputer -NonInteractive
    Change identity and rename the PC to GOLDEN-RIG-02 (restart required).

.EXAMPLE
    .\Configure-GoldenRig.ps1 -CertFile D:\rig.pem -CaCertFile D:\golden_rig_ca.pem -NonInteractive
    Import a signed certificate for the key previously generated with -GenerateCsr.
#>
[CmdletBinding()]
param(
    [string]$RigId,
    [string]$MdnsName,
    [int]$Port = 0,
    [switch]$RenameComputer,
    [string]$NewComputerName,
    [string]$CertFile,
    [string]$KeyFile,
    [string]$CaCertFile,
    [switch]$GenerateCsr,
    [switch]$ConfigureFirewall,
    [string]$FirewallRemoteAddress = 'LocalSubnet',
    [string]$RigUser,
    [string]$InstallDir = $PSScriptRoot,
    [string]$DataDir = (Join-Path $env:ProgramData 'GoldenRig'),
    [switch]$NonInteractive,
    [switch]$PauseAtEnd
)

$ErrorActionPreference = 'Stop'
$FirewallGroup = 'Golden Rig'

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal $id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# ---------------------------------------------------------------- self-elevate
if (-not (Test-Admin)) {
    if (-not $RigUser) { $RigUser = "$env:USERDOMAIN\$env:USERNAME" }
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-RigUser', "`"$RigUser`"")
    foreach ($kv in $PSBoundParameters.GetEnumerator()) {
        if ($kv.Key -eq 'RigUser') { continue }
        if ($kv.Value -is [System.Management.Automation.SwitchParameter]) {
            if ($kv.Value.IsPresent) { $argList += "-$($kv.Key)" }
        } else {
            $argList += "-$($kv.Key)"; $argList += "`"$($kv.Value)`""
        }
    }
    if (-not $PauseAtEnd) { $argList += '-PauseAtEnd' }
    Start-Process powershell.exe -Verb RunAs -ArgumentList $argList
    return
}
if (-not $RigUser) { $RigUser = "$env:USERDOMAIN\$env:USERNAME" }

# ---------------------------------------------------------------- helpers
function Write-Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Write-Warn($text) { Write-Host "WARNING: $text" -ForegroundColor Yellow }

function Read-Default([string]$prompt, [string]$default) {
    if ($NonInteractive) { return $default }
    $v = Read-Host "$prompt [$default]"
    if ([string]::IsNullOrWhiteSpace($v)) { return $default } else { return $v.Trim() }
}

function Read-YesNo([string]$prompt, [bool]$default) {
    if ($NonInteractive) { return $default }
    $d = if ($default) { 'Y/n' } else { 'y/N' }
    $v = Read-Host "$prompt ($d)"
    if ([string]::IsNullOrWhiteSpace($v)) { return $default }
    return $v.Trim().ToLower().StartsWith('y')
}

function ConvertTo-MdnsLabel([string]$value) {
    $v = $value.ToLower()
    if ($v.EndsWith('.local')) { $v = $v.Substring(0, $v.Length - 6) }
    return (($v -replace '[^a-z0-9]+', '-').Trim('-'))
}

function Test-MdnsLabel([string]$label) {
    return ($label.Length -le 63 -and $label -match '^[a-z0-9]([a-z0-9-]*[a-z0-9])?$')
}

function Test-NetBiosName([string]$name) {
    return ($name.Length -ge 1 -and $name.Length -le 15 -and $name -match '^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$' -and $name -notmatch '^[0-9]+$')
}

function Write-Utf8NoBom([string]$path, [string]$text) {
    [IO.File]::WriteAllText($path, $text, (New-Object Text.UTF8Encoding $false))
}

function Find-Tool([string]$exeName, [string]$scriptName) {
    $exe = Join-Path $InstallDir $exeName
    if (Test-Path $exe) { return @($exe) }
    # Source checkout: installer\..\<script>.py with the project venv
    $src = Split-Path $InstallDir -Parent
    $py = @((Join-Path $src '.venv\Scripts\python.exe'), (Join-Path (Split-Path $src -Parent) '.venv\Scripts\python.exe')) |
        Where-Object { Test-Path $_ } | Select-Object -First 1
    $script = Join-Path $src $scriptName
    if ($py -and (Test-Path $script)) { return @($py, $script) }
    return $null
}

function Invoke-Tool([string[]]$tool, [string[]]$toolArgs) {
    $exe = $tool[0]; $pre = @(); if ($tool.Count -gt 1) { $pre = $tool[1..($tool.Count - 1)] }
    & $exe @pre @toolArgs | Out-Host
    return $LASTEXITCODE
}

# ---------------------------------------------------------------- folders + existing config
Write-Host "Golden Rig configuration  (data folder: $DataDir)" -ForegroundColor Green
$certDir = Join-Path $DataDir 'certs'
$logDir = Join-Path $DataDir 'logs'
New-Item -ItemType Directory -Force -Path $DataDir, $certDir, $logDir | Out-Null

$configPath = Join-Path $DataDir 'rig_config.json'
$existing = $null
if (Test-Path $configPath) {
    try { $existing = Get-Content $configPath -Raw | ConvertFrom-Json } catch { Write-Warn "Existing $configPath is not valid JSON; it will be replaced." }
}

$slotsPath = Join-Path $DataDir 'slots.json'
if (-not (Test-Path $slotsPath)) {
    $example = Join-Path $InstallDir 'slots.example.json'
    if (-not (Test-Path $example)) { $example = Join-Path (Split-Path $InstallDir -Parent) 'slots.example.json' }
    if (Test-Path $example) {
        Copy-Item $example $slotsPath
        Write-Warn "Created $slotsPath from the example. Edit it with the real slot -> COM port mapping."
    }
}

# ---------------------------------------------------------------- identity
Write-Step 'Rig identity'
$defaultRigId = if ($existing -and $existing.rig_id) { $existing.rig_id } else { $env:COMPUTERNAME }
if (-not $RigId) { $RigId = Read-Default 'Rig ID (must equal the certificate CN)' $defaultRigId }
$RigId = $RigId.Trim()
if (-not $RigId) { throw 'Rig ID must not be empty.' }

$defaultMdns = if ($existing -and $existing.mdns -and $existing.mdns.name -and -not $PSBoundParameters.ContainsKey('RigId')) { $existing.mdns.name } else { ConvertTo-MdnsLabel $RigId }
if (-not $MdnsName) { $MdnsName = Read-Default 'mDNS hostname (stations connect to <name>.local)' $defaultMdns }
$MdnsName = ConvertTo-MdnsLabel $MdnsName
if (-not (Test-MdnsLabel $MdnsName)) { throw "mDNS name '$MdnsName' is not a valid DNS label (a-z, 0-9, '-', max 63)." }

$defaultPort = if ($existing -and $existing.port) { [int]$existing.port } else { 8762 }
if ($Port -eq 0) { $Port = [int](Read-Default 'TCP port' "$defaultPort") }
if ($Port -lt 1 -or $Port -gt 65535) { throw "Invalid port $Port." }

# ---------------------------------------------------------------- write config (keep unknown-to-us values)
$cfg = [ordered]@{
    rig_id             = $RigId
    host               = '0.0.0.0'
    port               = $Port
    backend            = 'serial-at'
    slots_config       = 'slots.json'
    cert               = 'certs/rig.pem'
    key                = 'certs/rig.key'
    ca_cert            = 'certs/golden_rig_ca.pem'
    mdns               = [ordered]@{ enabled = $true; name = $MdnsName }
    parallel           = 3
    log_dir            = 'logs'
    log_retention_days = 90
}
if ($existing) {
    foreach ($k in @('host', 'backend', 'slots_config', 'cert', 'key', 'ca_cert', 'parallel', 'log_dir', 'log_retention_days')) {
        if ($existing.PSObject.Properties.Name -contains $k) { $cfg[$k] = $existing.$k }
    }
    if ($existing.mdns -and ($existing.mdns.PSObject.Properties.Name -contains 'enabled')) { $cfg.mdns.enabled = [bool]$existing.mdns.enabled }
}
Write-Utf8NoBom $configPath ($cfg | ConvertTo-Json -Depth 5)
Write-Host "Saved $configPath"

# ---------------------------------------------------------------- certificates
Write-Step 'Certificates'
$rigPem = Join-Path $certDir 'rig.pem'
$rigKey = Join-Path $certDir 'rig.key'
$caPem = Join-Path $certDir 'golden_rig_ca.pem'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'

if (-not $NonInteractive -and -not $CertFile -and -not $KeyFile -and -not $CaCertFile -and -not $GenerateCsr) {
    Write-Host '  1) Import rig.pem / rig.key / golden_rig_ca.pem from files'
    Write-Host '  2) Generate a new private key + certificate request (CSR) on this PC'
    Write-Host '  3) Keep the current certificates'
    switch (Read-Default 'Choose' '3') {
        '1' {
            $CertFile = Read-Default 'Path to rig.pem (blank = skip)' ''
            $KeyFile = Read-Default 'Path to rig.key (blank = skip)' ''
            $CaCertFile = Read-Default 'Path to golden_rig_ca.pem (blank = skip)' ''
        }
        '2' { $GenerateCsr = $true }
    }
}

function Import-CertFile([string]$source, [string]$target) {
    if (-not $source) { return }
    if (-not (Test-Path $source)) { throw "File not found: $source" }
    if ((Resolve-Path $source).Path -eq $target) { return }
    if (Test-Path $target) { Copy-Item $target "$target.bak-$stamp" -Force }
    Copy-Item $source $target -Force
    Write-Host "Imported $source -> $target"
}
Import-CertFile $CertFile $rigPem
Import-CertFile $KeyFile $rigKey
Import-CertFile $CaCertFile $caPem

$certTool = Find-Tool 'rig_certtool.exe' 'rig_certtool.py'
if ($GenerateCsr) {
    if (-not $certTool) { throw 'rig_certtool not found; cannot generate a CSR.' }
    foreach ($f in @($rigKey, (Join-Path $certDir 'rig.csr'))) {
        if (Test-Path $f) { Move-Item $f "$f.bak-$stamp" -Force; Write-Host "Backed up $f" }
    }
    $code = Invoke-Tool $certTool @('request', '--rig-id', $RigId, '--out-dir', $certDir)
    if ($code -ne 0) { throw 'CSR generation failed.' }
    Write-Host ''
    Write-Host "Give $certDir\rig.csr to the Golden Rig CA operator and ask for:" -ForegroundColor Yellow
    Write-Host "    rig_certtool sign --csr rig.csr --ca-cert golden_rig_ca.pem --ca-key golden_rig_ca.key --dns $MdnsName.local" -ForegroundColor Yellow
    Write-Host 'Then import the returned rig.pem and golden_rig_ca.pem with "Configure Golden Rig" (option 1).' -ForegroundColor Yellow
}

# Restrict the certificate folder: SYSTEM + Administrators full, rig user read.
& icacls.exe $certDir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' "${RigUser}:(OI)(CI)RX" /T /Q | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Warn "Could not set permissions on $certDir for '$RigUser'." }
# Rig user may edit slots.json / config and write logs (certs\ no longer inherits this).
& icacls.exe $DataDir /grant "${RigUser}:(OI)(CI)M" /Q | Out-Null

if ((Test-Path $rigPem) -and $certTool) {
    $showArgs = @('show', '--cert', $rigPem, '--rig-id', $RigId, '--dns', "$MdnsName.local", '--fingerprint-out', (Join-Path $certDir 'rig.sha256.txt'))
    if (Test-Path $rigKey) { $showArgs += @('--key', $rigKey) }
    if (Test-Path $caPem) { $showArgs += @('--ca-cert', $caPem) }
    $null = Invoke-Tool $certTool $showArgs
}

# ---------------------------------------------------------------- firewall
$serverExe = Join-Path $InstallDir 'GoldenRigServer.exe'
if (-not $PSBoundParameters.ContainsKey('ConfigureFirewall') -and -not $NonInteractive) {
    $ConfigureFirewall = Read-YesNo "Allow inbound TCP $Port and mDNS (UDP 5353) in Windows Firewall?" $true
}
if ($ConfigureFirewall) {
    Write-Step 'Windows Firewall'
    Get-NetFirewallRule -Group $FirewallGroup -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    $prog = @{}
    if (Test-Path $serverExe) { $prog.Program = $serverExe }
    New-NetFirewallRule -Group $FirewallGroup -DisplayName "Golden Rig TLS (TCP $Port)" -Direction Inbound -Action Allow `
        -Protocol TCP -LocalPort $Port -RemoteAddress $FirewallRemoteAddress -Profile Any @prog | Out-Null
    New-NetFirewallRule -Group $FirewallGroup -DisplayName 'Golden Rig mDNS (UDP 5353)' -Direction Inbound -Action Allow `
        -Protocol UDP -LocalPort 5353 -RemoteAddress LocalSubnet -Profile Any @prog | Out-Null
    Write-Host "Inbound TCP $Port from $FirewallRemoteAddress and UDP 5353 from LocalSubnet allowed."
}

# ---------------------------------------------------------------- computer name
Write-Step 'Computer name'
$current = $env:COMPUTERNAME
$pending = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\ComputerName\ComputerName').ComputerName
if (-not $NewComputerName) { $NewComputerName = $MdnsName.ToUpper() }
Write-Host "Current computer name: $current$(if ($pending -ne $current) { "  (pending after restart: $pending)" })"
Write-Host "mDNS hostname        : $MdnsName.local"
$needsRename = ($pending.ToLower() -ne $NewComputerName.ToLower())
if ($needsRename -and -not $PSBoundParameters.ContainsKey('RenameComputer') -and -not $NonInteractive) {
    $RenameComputer = Read-YesNo "Rename this computer to '$NewComputerName' so Windows also answers $MdnsName.local? (restart required)" $true
}
$restartNeeded = $false
if ($RenameComputer -and $needsRename) {
    if (-not (Test-NetBiosName $NewComputerName)) {
        Write-Warn "'$NewComputerName' cannot be a Windows computer name (max 15 characters, letters/digits/hyphen). Not renamed; the rig still advertises $MdnsName.local itself."
    } else {
        Rename-Computer -NewName $NewComputerName -Force -WarningAction SilentlyContinue
        $restartNeeded = $true
        Write-Host "Computer will be renamed to '$NewComputerName' after restart." -ForegroundColor Yellow
    }
} elseif (-not $needsRename) {
    Write-Host 'Computer name already matches the mDNS hostname.'
}

# ---------------------------------------------------------------- check
Write-Step 'Configuration check'
$server = Find-Tool 'GoldenRigServer.exe' 'rig_launcher.py'
$checkCode = 1
if ($server) { $checkCode = Invoke-Tool $server @('--check', '--config', $configPath) }

Write-Step 'Summary'
Write-Host "Rig ID           : $RigId"
Write-Host "Station endpoint : $MdnsName.local : $Port"
$fpFile = Join-Path $certDir 'rig.sha256.txt'
if (Test-Path $fpFile) { Write-Host "Fingerprint      : $((Get-Content $fpFile -Raw).Trim())" }
Write-Host "Slots            : $slotsPath   (list COM ports: GoldenRigServer.exe --list-ports)"
if ($checkCode -ne 0) { Write-Warn 'The configuration check reported problems (see above). The server will not start until they are fixed.' }
if ($restartNeeded) { Write-Warn 'Restart Windows to apply the new computer name.' }
Write-Host "`nOn each station: Golden Rig binding -> host $MdnsName.local, port $Port -> Request identity -> compare the fingerprint -> approve."

if ($PauseAtEnd) { Read-Host "`nPress Enter to close" | Out-Null }
if ($restartNeeded) { exit 3010 }
exit 0
