# sekai-apphash-updater

Scan and extract apphash of Project Sekai: Colorful Stage feat. Hatsune Miku

## How it works

Every five minutes (and once at startup) the updater checks the latest store
version of each region: QooApp for JP, EN, TW and KR, TapTap for CN. When a
region's version changes, it downloads the XAPK and reads `clientAppHash` from
the `production_android` player settings inside the APK.

XAPK sources are tried in order until one yields the app hash:

- JP, EN, TW, KR: APKCombo (only when it lists the exact new version), then
  APKPure, which does not always carry the latest version promptly.
- CN: the configured CN mirror.

## Published app identity

With `PUBLISH_ENABLED = True`, each run mirrors `cache/apphash_json/{REGION}.json`
to the `data` branch of this repository and pushes when anything changed.
Consumers read, for example:

```
https://raw.githubusercontent.com/Sekai-World/sekai-apphash-updater/refs/heads/data/KR.json
```

```json
{
  "appVersion": "6.4.0",
  "appHash": "d67a688b-ff78-4300-bfc5-2120c000d75d",
  "updatedAt": "2026-09-24T00:00:00Z"
}
```

`updatedAt` records when the updater extracted that version. The updater is the
branch's only writer: its clone in `PUBLISH_REPO_DIR` is reset to the remote
branch before each sync. Pushing uses the host's Git credentials for
`PUBLISH_REMOTE_URL`.

## Running

```bash
cp config.example.py config.py  # then adjust
uv sync
uv run python updater.py
```

Tests: `uv run --group dev pytest`
