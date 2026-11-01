$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$relativeTargets = @('build', 'build_release', '.ruff_cache', '__pycache__',
    'src/synthrecall/__pycache__', 'src/synthrecall/ui/__pycache__',
    'tests/__pycache__', 'tools/__pycache__')
$targets = @($relativeTargets | ForEach-Object {
    $target = [IO.Path]::GetFullPath((Join-Path $workspace $_))
    if (-not $target.StartsWith($workspace + [IO.Path]::DirectorySeparatorChar,
        [StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe target: $target" }
    if (Test-Path -LiteralPath $target) { Get-Item -LiteralPath $target }
})
$files = @($targets | ForEach-Object { Get-ChildItem -LiteralPath $_.FullName -Recurse -File -Force })
if (-not $files.Count) { Write-Output 'No generated files to clean.'; exit 0 }
$backupDir = Join-Path $workspace '.cleanup-backups'
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
$archivePath = Join-Path $backupDir ('generated-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.zip')
Add-Type -AssemblyName System.IO.Compression
$zip = [IO.Compression.ZipFile]::Open($archivePath, [IO.Compression.ZipArchiveMode]::Create)
try {
    foreach ($file in $files) {
        $relative = [IO.Path]::GetRelativePath($workspace, $file.FullName).Replace('\', '/')
        [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $file.FullName,
            $relative, [IO.Compression.CompressionLevel]::Optimal) | Out-Null
    }
} finally { $zip.Dispose() }
$zip = [IO.Compression.ZipFile]::OpenRead($archivePath)
try {
    if ($zip.Entries.Count -ne $files.Count) { throw 'Archive entry count mismatch' }
    foreach ($file in $files) {
        $relative = [IO.Path]::GetRelativePath($workspace, $file.FullName).Replace('\', '/')
        $entry = $zip.GetEntry($relative)
        if ($null -eq $entry -or $entry.Length -ne $file.Length) { throw "Archive mismatch: $relative" }
        $stream = $entry.Open()
        try { $archivedHash = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($stream)) }
        finally { $stream.Dispose() }
        if ($archivedHash -ne (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash) {
            throw "Archive hash mismatch: $relative"
        }
    }
} finally { $zip.Dispose() }
$total = ($files | Measure-Object Length -Sum).Sum
foreach ($target in $targets) {
    $resolved = (Resolve-Path -LiteralPath $target.FullName).Path
    if (-not $resolved.StartsWith($workspace + [IO.Path]::DirectorySeparatorChar,
        [StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe resolved target: $resolved" }
    Remove-Item -LiteralPath $resolved -Recurse -Force
    Write-Output "REMOVED $resolved"
}
$backupSize = (Get-Item -LiteralPath $archivePath).Length
Write-Output "BACKUP $archivePath"
Write-Output "FILES $($files.Count) ORIGINAL_BYTES $total BACKUP_BYTES $backupSize SAVED_BYTES $($total - $backupSize)"
