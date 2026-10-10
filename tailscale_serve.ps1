# Put Ten Forward on your tailnet with a real HTTPS address, so it works away from home
# (and so the phone app's "away" address has something to talk to).
#
#   1. Install Tailscale on this computer and on your phone, both signed into the same account.
#   2. Run:  powershell -ExecutionPolicy Bypass -File tailscale_serve.ps1
#   3. Put the address it prints into Settings -> Addresses, and into the phone app.
#
# This is `serve`, which is tailnet-only: nothing is published to the internet.
tailscale serve --bg --https=8443 http://127.0.0.1:8410
tailscale serve status
$name = (tailscale status --json | ConvertFrom-Json).Self.DNSName.TrimEnd('.')
Write-Host ""
Write-Host "Ten Forward away from home: https://$($name):8443"
