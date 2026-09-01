# Aura OTA release flow

Aura uses two 2.5 MiB application slots and Bootloader rollback. The first
upgrade from the old single-app layout must be a complete wired flash. Later
application and resource updates can use **Menu > Update**.

## Safety model

- The manifest and every artifact are fetched over HTTPS.
- Application and resource payloads must match the manifest SHA-256 and size.
- An application is written to the inactive slot and selected only after image
  validation. It is marked valid after display, audio, and Wi-Fi stay healthy
  for ten seconds after reboot.
- Resources are downloaded to a temporary SPIFFS file, verified, then swapped.
  NVS records an in-progress swap so an interrupted replacement is restored on
  the next boot.
- The manifest contains public file metadata only. Do not place credentials,
  runtime JSON, Soul/persona data, or server configuration in this directory.

## Build a release directory

Run from `firmware/esp32` after `idf.py build`:

```bash
python tools/make_ota_release.py \
  --version 0.18.1 \
  --assets-version 0.18.1 \
  --base-url https://updates.example.com/aura/stable \
  --asset scenes/outside_park.bin \
  --asset scenes/outside_mall.bin \
  --output releases/ota/0.18.1
```

Omit `--asset` when a release changes only the application. Publish the
generated files under `/firmware/stable/`, uploading artifacts first and
replacing `manifest.json` last. Publishing the manifest last prevents devices
from seeing a release whose payloads are not yet available.

Configure the same host in `idf.py menuconfig` under **Aura Lily**:

```text
CONFIG_AURA_OTA_MANIFEST_URL="https://updates.example.com/aura/stable/manifest.json"
CONFIG_AURA_OTA_RESOURCES_MANIFEST_URL="https://updates.example.com/aura/stable/resources.json"
```

The repository defaults are empty deliberately. A public build never contacts
the project's private deployment.

Never overwrite an artifact URL after publication. Use a new version and file
name so caches cannot serve bytes that disagree with the manifest hash.
