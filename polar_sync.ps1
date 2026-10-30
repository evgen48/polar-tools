<#
.SYNOPSIS
  Dependency-free (no Ruby) PFTP client for newer Polar watches (Ignite, Vantage...) over a COM port.

.EXAMPLE
  .\polar_sync.ps1 -Port COM4 -List /            # list a directory
  .\polar_sync.ps1 -Port COM4 -Get /PRODCONF.TXT # download one file
  .\polar_sync.ps1 -Port COM4 -Sync              # download everything to -Out
#>
param(
    [string]$Port = 'COM4',
    [string]$Out = (Join-Path $env:USERPROFILE 'Polar\watch1'),
    [string]$List,
    [string]$Get,
    [switch]$Sync,
    [string]$Root = '/',
    [int]$TimeoutSec = 30,
    [string]$Trace
)

$ErrorActionPreference = 'Stop'
$script:sp = $null

function Open-Watch {
    $s = New-Object System.IO.Ports.SerialPort $Port, 115200, ([System.IO.Ports.Parity]::None), 8, ([System.IO.Ports.StopBits]::One)
    $s.Handshake = [System.IO.Ports.Handshake]::None
    $s.ReadTimeout = 2000
    $s.WriteTimeout = 5000
    $s.DtrEnable = $true
    $s.RtsEnable = $true
    $s.Open()
    $s.DiscardInBuffer()
    $s.DiscardOutBuffer()
    $script:sp = $s
}

function Get-ErrorText([int]$code) {
    switch ($code) {
        1 { 'rebooting' } 2 { 'try again' } 100 { 'unidentified host error' } 101 { 'invalid command' }
        102 { 'invalid parameter' } 103 { 'no such file or directory' } 104 { 'directory exists' }
        105 { 'file exists' } 106 { 'operation not permitted' } 107 { 'no such user' } 108 { 'timeout' }
        200 { 'unidentified device error' } 201 { 'not implemented' } 202 { 'system busy' }
        203 { 'invalid content' } 204 { 'checksum failure' } 205 { 'disk full' }
        206 { 'prerequisite not found' } 207 { 'insufficient buffer' } 208 { 'wait for idling' }
        default { "unknown ($code)" }
    }
}

function Write-Trace([string]$dir, [byte[]]$bytes) {
    if (-not $Trace) { return }
    $n = [Math]::Min($bytes.Length, 48)
    $hex = ($bytes[0..($n - 1)] | ForEach-Object { '{0:x2}' -f $_ }) -join ' '
    Add-Content -Path $Trace -Value ("{0:HH:mm:ss.fff} {1} len={2}: {3}" -f (Get-Date), $dir, $bytes.Length, $hex)
}

function Invoke-Request([byte[]]$data) {
    # Frame: [5, len_lo, len_hi] + data ; same as polar_usb_acm.rb
    $pkt = [byte[]](5, ($data.Length -band 255), ($data.Length -shr 8)) + $data
    Write-Trace 'TX' $pkt
    $script:sp.Write($pkt, 0, $pkt.Length)

    $buf = New-Object 'System.Collections.Generic.List[byte]'
    $response = New-Object 'System.Collections.Generic.List[byte]'
    $tmp = New-Object byte[] 65536
    $expectHeader = $true; $started = $false; $isNotif = $false; $hasMore = $false
    $expected = 0; $received = 0; $done = $false
    $lastRx = Get-Date

    while (-not $done -or $buf.Count -gt 0) {
        $avail = $script:sp.BytesToRead
        if ($avail -gt 0) {
            $n = $script:sp.Read($tmp, 0, [Math]::Min($avail, $tmp.Length))
            $chunk = [byte[]]($tmp[0..($n - 1)])
            Write-Trace 'RX' $chunk
            $buf.AddRange($chunk)
            $lastRx = Get-Date
        }
        if ($buf.Count -eq 0) {
            if (((Get-Date) - $lastRx).TotalSeconds -gt $TimeoutSec) { throw "TIMEOUT: no data from watch for $TimeoutSec s" }
            Start-Sleep -Milliseconds 2
            continue
        }

        if ($expectHeader) {
            if ($buf.Count -lt 3) { Start-Sleep -Milliseconds 2; continue }
            $flags = [int]$buf[0]
            $isNotif = ($flags -band 2) -ne 0
            $hasMore = ($flags -band 8) -ne 0
            $expected = [int]$buf[1] + ([int]$buf[2] -shl 8)
            $received = 0

            if ($flags -eq 1) {
                # Error/status packet
                $code = if ($buf.Count -gt 3) { $buf[3] } else { 0 }
                $buf.Clear()
                if ($code -ne 0) { throw "WATCH ERROR $code : $(Get-ErrorText $code)" }
                continue
            }
            if (-not $started -and ($flags -band 1) -eq 0) { throw "Protocol error: initial packet expected (flags=$flags)" }
            if ($started -and ($flags -band 1) -ne 0) { throw "Protocol error: unexpected initial packet (flags=$flags)" }
            if (($flags -band 4) -eq 0 -or ($flags -band 0xf0) -ne 0) { throw "Protocol error: bad flags $flags" }

            $payload = $buf.GetRange(3, $buf.Count - 3)
            $expectHeader = $false
            $started = $true
        } else {
            $payload = $buf.GetRange(0, $buf.Count)
        }

        $overflow = $received + $payload.Count - $expected
        $buf.Clear()
        if ($overflow -gt 0) {
            # Keep the surplus for the next header
            $buf.AddRange($payload.GetRange($payload.Count - $overflow, $overflow))
            $payload.RemoveRange($payload.Count - $overflow, $overflow)
        }
        $received += $payload.Count
        if (-not $isNotif) { $response.AddRange($payload) }

        if ($received -eq $expected) {
            $expectHeader = $true
            if ($hasMore) {
                $ack = [byte[]](8, 0, 0)
                $script:sp.Write($ack, 0, 3)
            } elseif ($isNotif) {
                $started = $false
            } else {
                $started = $false
                $done = $true
            }
        }
    }
    return , $response.ToArray()
}

function New-GetMessage([string]$path) {
    # PbPFtpOperation { command = GET(0) [field 1], path [field 2] }
    $pb = [System.Text.Encoding]::UTF8.GetBytes($path)
    $len = New-Object 'System.Collections.Generic.List[byte]'
    $v = $pb.Length
    while ($v -ge 128) { $len.Add([byte](($v -band 127) -bor 128)); $v = $v -shr 7 }
    $len.Add([byte]$v)
    $msg = [byte[]](0x08, 0x00, 0x12) + $len.ToArray() + $pb
    return , ([byte[]](($msg.Length -band 255), ($msg.Length -shr 8)) + $msg)
}

function Read-Varint([byte[]]$b, [ref]$i) {
    [uint64]$r = 0; $shift = 0
    while ($true) {
        $x = $b[$i.Value]; $i.Value++
        $r = $r -bor ([uint64]($x -band 127) -shl $shift)
        if (($x -band 128) -eq 0) { break }
        $shift += 7
    }
    return $r
}

function Skip-Field([byte[]]$b, [ref]$i, [int]$wt) {
    switch ($wt) {
        0 { [void](Read-Varint $b $i) }
        1 { $i.Value += 8 }
        2 { $l = [int](Read-Varint $b $i); $i.Value += $l }
        5 { $i.Value += 4 }
        default { throw "Unsupported protobuf wire type $wt" }
    }
}

function ConvertFrom-PbDirectory([byte[]]$b) {
    # PbPFtpDirectory { repeated PbPFtpEntry entries = 1 } ; entry { name = 1, size = 2, ... }
    $entries = @()
    $i = 0
    while ($i -lt $b.Length) {
        $tag = [int](Read-Varint $b ([ref]$i))
        $field = $tag -shr 3; $wt = $tag -band 7
        if ($field -eq 1 -and $wt -eq 2) {
            $l = [int](Read-Varint $b ([ref]$i))
            $e = $b[$i..($i + $l - 1)]; $i += $l
            $name = ''; $size = [uint64]0; $j = 0
            while ($j -lt $e.Length) {
                $t = [int](Read-Varint $e ([ref]$j))
                $f = $t -shr 3; $w = $t -band 7
                if ($f -eq 1 -and $w -eq 2) {
                    $nl = [int](Read-Varint $e ([ref]$j))
                    $name = [System.Text.Encoding]::UTF8.GetString($e, $j, $nl); $j += $nl
                } elseif ($f -eq 2 -and $w -eq 0) {
                    $size = Read-Varint $e ([ref]$j)
                } else { Skip-Field $e ([ref]$j) $w }
            }
            $entries += [pscustomobject]@{ Name = $name; Size = [int64]$size }
        } else { Skip-Field $b ([ref]$i) $wt }
    }
    return , $entries
}

function Get-RemoteDir([string]$dir) {
    if (-not $dir.EndsWith('/')) { $dir += '/' }
    $res = Invoke-Request (New-GetMessage $dir)
    if ($res.Length -gt 0 -and $res[0] -eq 0) { return $null }
    return , (ConvertFrom-PbDirectory $res)
}

function Get-RemoteFile([string]$remote, [string]$local) {
    $res = Invoke-Request (New-GetMessage $remote)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $local) | Out-Null
    $part = "$local.part"
    [System.IO.File]::WriteAllBytes($part, $res)
    Move-Item -Force $part $local
    return $res.Length
}

function ConvertTo-LocalPath([string]$remote) {
    return Join-Path $Out ($remote.TrimStart('/') -replace '/', '\')
}

function Sync-Dir([string]$remoteDir) {
    $entries = Get-RemoteDir $remoteDir
    if ($null -eq $entries) { Write-Warning "Cannot list $remoteDir"; return }
    foreach ($e in $entries) {
        if ($e.Name.EndsWith('/')) {
            Sync-Dir ($remoteDir + $e.Name)
            continue
        }
        $remote = $remoteDir + $e.Name
        $local = ConvertTo-LocalPath $remote
        if ($remoteDir -like '/SYS/FONT/*') { continue }   # huge, not needed
        $upToDate = (Test-Path $local) -and ((Get-Item $local).Length -eq $e.Size)
        # These change content without changing size
        if ($remote -eq '/SYNCINFO.BPB' -or $remoteDir -match '/(TL|DSUM)/$' -or $remoteDir -match '^/U/\d+/S/$') { $upToDate = $false }
        if ($upToDate) { continue }
        Write-Host "Downloading $remote ($($e.Size) bytes)"
        try {
            [void](Get-RemoteFile $remote $local)
        } catch {
            if ($_.Exception.Message -like 'TIMEOUT*') { throw }
            Write-Warning "Skipping ${remote}: $($_.Exception.Message)"
        }
    }
}

try {
    Open-Watch
    Write-Host "Opened $Port"
    if ($List) {
        $entries = Get-RemoteDir $List
        if ($null -eq $entries) { Write-Host "Directory doesn't exist" } else { $entries | ForEach-Object { '{0,-30} {1,10}' -f $_.Name, $_.Size } }
    } elseif ($Get) {
        $local = Join-Path $Out (Split-Path -Leaf $Get)
        $n = Get-RemoteFile $Get $local
        Write-Host "Saved $n bytes to $local"
    } elseif ($Sync) {
        Write-Host "Syncing '$Root' to $Out"
        Sync-Dir $Root
        Write-Host 'Sync complete'
    } else {
        Write-Host 'Specify -List <dir>, -Get <file> or -Sync'
    }
} finally {
    if ($script:sp -and $script:sp.IsOpen) { $script:sp.Close() }
}
