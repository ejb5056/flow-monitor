# DAS Link: tells the flow monitor which symbol is in each DAS montage, so a panel can follow it.
#
# It reads window titles only. It never clicks, types, or sends anything to DAS.
# A DAS montage title looks like "AAPL     348.05 -- 348.15     Apple Inc. - Common Stock". Montages are numbered by position
# on screen: top-to-bottom, then left-to-right (DAS 1 = the top-left one).
#
#   DAS Link - test.cmd   shows what it detects, sends nothing
#   DAS Link.cmd          runs the link (asks for the app address and password the first time)

param([switch]$Test, [switch]$Reset)
$ErrorActionPreference = 'Stop'
$cfgPath = Join-Path $PSScriptRoot 'das-link.json'

# Which montages count: 'left' = only those whose window centre is in the left half of the whole
# desktop (all monitors together), 'right' = right half, 'all' = every montage.
$Side = 'all'

Add-Type -AssemblyName System.Windows.Forms
$desk = [System.Windows.Forms.SystemInformation]::VirtualScreen
$midX = $desk.Left + $desk.Width / 2

Add-Type -TypeDefinition @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public static class DasWindows {
    delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc cb, IntPtr lParam);
    [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr parent, EnumProc cb, IntPtr lParam);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr h, out RECT r);
    [StructLayout(LayoutKind.Sequential)] struct RECT { public int L, T, R, B; }

    public class Win { public long Handle; public string Title; public int X; public int Y; public int W; public bool Minimized; public bool Top; }

    public static List<Win> ForProcesses(int[] pids) {
        var want = new HashSet<uint>();
        foreach (var p in pids) want.Add((uint)p);
        var found = new List<Win>();
        var seen = new HashSet<long>();
        EnumProc take = (h, l) => {
            if (!IsWindowVisible(h) || seen.Contains(h.ToInt64())) return true;
            var sb = new StringBuilder(512);
            GetWindowText(h, sb, 512);
            if (sb.Length == 0) return true;
            RECT r; GetWindowRect(h, out r);
            seen.Add(h.ToInt64());
            found.Add(new Win { Handle = h.ToInt64(), Title = sb.ToString(), X = r.L, Y = r.T, W = r.R - r.L, Minimized = IsIconic(h), Top = l.ToInt64() == 1 });
            return true;
        };
        EnumWindows((h, l) => {
            uint pid; GetWindowThreadProcessId(h, out pid);
            if (want.Contains(pid)) { take(h, new IntPtr(1)); EnumChildWindows(h, take, IntPtr.Zero); }
            return true;
        }, IntPtr.Zero);
        return found;
    }
}
"@

# Symbol, whitespace, then a "low - high" price range: the DAS montage title. Chart titles ("AAPL--1 Minute--") don't match.
$montageRx = '^\s*([A-Z][A-Z0-9.\-]{0,7})\s+\d+(\.\d+)?\s*-+\s*\d+(\.\d+)?'

function Get-Montages {
    $procs = @(Get-Process -Name 'dastrader' -ErrorAction SilentlyContinue)
    if ($procs.Count -eq 0) { return $null }
    $wins = [DasWindows]::ForProcesses([int[]]($procs | ForEach-Object { $_.Id }))
    $m = foreach ($w in $wins) {
        if ($w.Minimized) { continue }
        $hit = [regex]::Match($w.Title, $montageRx)
        if (-not $hit.Success) { continue }
        $cx = $w.X + $w.W / 2
        $inSide = ($Side -eq 'all') -or ($Side -eq 'left' -and $cx -lt $midX) -or ($Side -eq 'right' -and $cx -ge $midX)
        if ($inSide) { [pscustomobject]@{ Sym = $hit.Groups[1].Value; X = $w.X; Y = $w.Y; Title = $w.Title } }
    }
    # Rows 60px apart count as one row, so slight misalignment doesn't swap numbering.
    $sorted = @($m | Sort-Object @{ e = { [math]::Floor($_.Y / 60) } }, X)
    $i = 0
    foreach ($s in $sorted) { $i++; $s | Add-Member -NotePropertyName Slot -NotePropertyValue $i }
    return , $sorted
}

if ($Test) {
    Write-Host "DAS Link test: reading window titles every second. Change a montage symbol to see it update. Ctrl+C to stop.`n"
    while ($true) {
        $procs = @(Get-Process -Name 'dastrader' -ErrorAction SilentlyContinue)
        Clear-Host
        Write-Host ("DAS Link test  {0:HH:mm:ss}" -f (Get-Date))
        if ($procs.Count -eq 0) { Write-Host "`nDAS (dastrader.exe) is not running." }
        else {
            $wins = [DasWindows]::ForProcesses([int[]]($procs | ForEach-Object { $_.Id }))
            Write-Host ("`nMontages linked ({0} side of the desktop):" -f $Side)
            $ms = Get-Montages
            if (-not $ms -or $ms.Count -eq 0) { Write-Host "  none" }
            foreach ($x in $ms) { Write-Host ("  DAS {0}: {1,-6}  at ({2},{3})" -f $x.Slot, $x.Sym, $x.X, $x.Y) }
            $skipped = foreach ($w in $wins) {
                $hit = [regex]::Match($w.Title, $montageRx)
                if ($hit.Success -and -not ($ms | Where-Object { $_.Title -eq $w.Title })) { "{0} at ({1},{2})" -f $hit.Groups[1].Value, $w.X, $w.Y }
            }
            Write-Host "`nMontages ignored (other side or minimized):"
            if (-not $skipped) { Write-Host "  none" } else { foreach ($k in $skipped) { Write-Host "  $k" } }
            $tops = @($wins | Where-Object { $_.Top })
            Write-Host ("`nDAS windows ({0} total, first 20):" -f $tops.Count)
            foreach ($w in ($tops | Select-Object -First 20)) {
                $t = $w.Title; if ($t.Length -gt 60) { $t = $t.Substring(0, 60) }
                Write-Host ("  {0}{1}" -f $t, $(if ($w.Minimized) { '  [minimized]' } else { '' }))
            }
        }
        Start-Sleep -Seconds 1
    }
}

if ($Reset -and (Test-Path $cfgPath)) { Remove-Item $cfgPath }
if (-not (Test-Path $cfgPath)) {
    $url = (Read-Host 'Flow monitor address (e.g. https://flow-monitor-production.up.railway.app)').Trim().TrimEnd('/')
    $sec = Read-Host 'App password' -AsSecureString
    $pw = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
    @{ url = $url; password = $pw } | ConvertTo-Json | Set-Content -Path $cfgPath -Encoding UTF8
    Write-Host "Saved to $cfgPath"
}
$cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
$headers = @{ 'X-App-Key' = $cfg.password }
$endpoint = "$($cfg.url)/api/das"

Write-Host "DAS Link running -> $($cfg.url)   (close this window to stop)"
$last = ''
$lastSent = [datetime]::MinValue
while ($true) {
    try {
        $ms = Get-Montages
        $list = @()
        if ($ms) { $list = @($ms | ForEach-Object { @{ slot = $_.Slot; sym = $_.Sym } }) }
        $key = ($list | ForEach-Object { "$($_.slot)=$($_.sym)" }) -join ','
        # Send on change, and every 15s as a heartbeat so the panel knows the link is alive.
        if ($key -ne $last -or ((Get-Date) - $lastSent).TotalSeconds -ge 15) {
            $body = @{ montages = $list } | ConvertTo-Json -Depth 4 -Compress
            Invoke-RestMethod -Method Post -Uri $endpoint -Headers $headers -ContentType 'application/json' -Body $body -TimeoutSec 5 | Out-Null
            if ($key -ne $last) { Write-Host ("{0:HH:mm:ss}  {1}" -f (Get-Date), $(if ($key) { $key } else { 'no montages' })) }
            $last = $key
            $lastSent = Get-Date
        }
        Start-Sleep -Milliseconds 250
    } catch {
        Write-Host ("{0:HH:mm:ss}  send failed: {1}" -f (Get-Date), $_.Exception.Message)
        $last = ''
        Start-Sleep -Seconds 3
    }
}
