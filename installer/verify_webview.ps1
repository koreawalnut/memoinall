param([Parameter(Mandatory = $true)][string]$Path)
$ErrorActionPreference = 'Stop'
$signature = Get-AuthenticodeSignature -LiteralPath $Path
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notlike '*O=Microsoft Corporation*') {
    Write-Error ('WebView2 signature verification failed: ' + $signature.Status + ' / ' + $signature.StatusMessage)
    exit 1
}
Write-Output 'Verified Microsoft WebView2 bootstrapper signature'
