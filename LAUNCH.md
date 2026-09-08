# Launch — Provision

From this folder:

```bash
cd /Users/cameroncohen/Developer/projects/Provision
python3 run.py
```

| | |
|---|---|
| Launcher | `~/Developer/projects/Launchers/DOWNLOWd.command` |
| Kind | runnable |

Note: the Finder launcher above still points at the pre-rename path
(`.../projects/DOWNLOWd`, now `.../projects/Provision`) and its own
`sync-projects` regeneration script no longer exists at the path its
header comment names. It needs to be regenerated or hand-fixed in the
`Launchers` project directly — that project is outside this repo, so
it wasn't touched as part of this rename.
