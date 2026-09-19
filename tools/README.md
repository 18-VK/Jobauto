Downloaded helper binaries live here. None are committed — fetch what you need.

**cloudflared** — only for tunnelling the *local* dashboard (`docs/HOSTING.md`).
Not needed if you deploy to the cloud.

```powershell
curl -L -o tools\cloudflared.exe https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe
```

**gh** — GitHub CLI, for creating and pushing the repo without an installer.

```powershell
curl -L -o tools\gh.zip https://github.com/cli/cli/releases/latest/download/gh_2.83.2_windows_amd64.zip
tar -xf tools\gh.zip -C tools
```
