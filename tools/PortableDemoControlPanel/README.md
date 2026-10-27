# Portable Demo Control Panel

This is a separate Windows x64 control panel for the beta.15 portable demo. It expects this layout:

```text
<demo folder>/
  GeWuPortableDemoControl.exe
  app/Workbench.exe
  app/build-manifest.json
  app/runtime/python.exe
  Data/
  Cache/
  relocate-demo-data.py   (optional)
```

The executable is bound to build `0.5.0-beta.15+ddc2b996e650` and commit
`ddc2b996e650b96b7bbbe69b57085fa18d116d38`. Start passes explicit sibling `Data` and
`Cache` paths. When the optional relocation helper exists, each start invokes it with
`app/runtime/python.exe relocate-demo-data.py --data-dir <absolute Data path>` (the helper is idempotent
so a copied folder can be relocated on another PC); a non-zero
exit leaves the workbench stopped and reports failure.

The demo runs as DEV with `GEWU_INSTANCE_ID=DEMO` and
`GEWU_DEV_EXTERNAL_EFFECTS=online` (reported by the app as `live_validation`). Opening, stopping, and restarting are enabled only
while the launcher endpoint file points to loopback and both `/api/health` and
`/api/runtime/status` report the expected build, commit, data path, and runtime identity.
An occupied or mismatched endpoint is shown as an identity conflict. The panel does not
scan ports, adopt a process, or terminate a PID; it uses only the bundled `Workbench.exe`
start/stop interface.

The status card displays only the model, SellerSprite, and Sorftime credential-presence booleans
from the runtime endpoint; absent or false entries direct the operator to the workbench's 总设置.
It never reads or displays credential values.

Build and publish:

```powershell
dotnet run --project SelfTest/PortableDemoControlPanel.SelfTest.csproj
dotnet publish PortableDemoControlPanel.csproj -c Release -r win-x64 --self-contained true -o publish
```
