# Changelog

## 1.0.8 (2026-06-07)

**Now ships as a prebuilt multi-arch image from GitHub Container Registry - downloaded, not built on your device.** Same delivery change as Hookii Bridge 1.2.7: a GitHub Action builds `amd64` / `aarch64` / `armv7` images and pushes them to `ghcr.io/torvalstrom/<arch>-hookii-mower-map`, and `config.yaml` now has an `image:` key so the Supervisor pulls the image instead of building it locally. Fixes slow / failing updates on ARM hardware. No configuration change; code unchanged from 1.0.7.

## 1.0.7 (2026-05-30)

**Fix: cut/transit overlay now also picks up `rotate_deg`.** v1.0.6 rotated the boundary polygons, the live trail and the robot marker but the cut/transit polyline overlay was re-extracted via a fresh `extract_path_points(s)` call that bypassed the rotation, so the rotated dashboard map looked correct EXCEPT the bright cut swath which still drew in the un-rotated frame, layered on top of the rotated boundary at an obvious angle. v1.0.7 reuses the already-rotated point list both for the bounding box AND for the cut/transit segment classifier.

## 1.0.6 (2026-05-30)

**Per-deployment map rotation.** The Hookii cloud delivers points in the mower's own local frame which has no fixed relation to compass north; in practice the in-app projection and the SVG sometimes look 90 degrees apart because the app is doing its own orientation. New `rotate_deg` config option (default 0 = identity) applies a counter-clockwise rotation to every point before bounding-box computation and SVG drawing - boundary, exclusion zones, path cut/transit segments, live trail, robot circle and heading arrow all rotate together. Typical values: 0, 90, 180, 270. Set whatever offset gets your SVG aligned with your in-app view. Also bumped run.sh's Supervisor probe to check `SUPERVISOR_TOKEN` env var directly (the same fix shipped in hookii-bridge v1.2.5) - cleaner detection of "am I hosted by HA Supervisor?".

## 1.0.5 (2026-05-30)

**Idle-watchdog: force-exit when the MQTT stream goes silent.** Observed today on a real broker hiccup: the bridge's EMQX bounced, paho's `loop_forever(retry_first_connection=True)` re-entered its TCP retry path but never produced another `on_message` callback for 4 hours afterwards. The SVG endpoint kept serving 200's from the in-memory cache, the dashboard looked alive, but every frame was the last good frame from before the disconnect - users saw the map frozen. A daemon thread now tracks the wall-clock of the most recent `on_message` callback and force-exits via `os._exit(1)` after `WATCHDOG_IDLE_SECONDS` (default 300) of silence. The supervisor (k3s, Docker, HA Supervisor) respawns us with a fresh paho client. Same recover-by-restart pattern shipped in `hookii-bridge` v1.2.4 - both addons now share the same robustness floor.

## 1.0.4 (2026-05-30)

**Cut/transit segmentation now matches what the Hookii mobile app shows.** Users comparing the SVG side-by-side with the official app reported that the SVG showed almost the whole boundary as "mowed" while the app correctly highlighted only the swept rows. Root cause: the May 2026 cloud schema moved the cut/transit `info` field OFF the individual `pathPointList` entries and onto SEGMENT ranges in `ALL_PATH_INDEX_V2.indexInfoList` (`{startIndex, endIndex, info}`). Every per-point `info` is now silently `0`, so the old `point.info == 1 → cut, else transit` classification put 100% of the path into the thin transit channel - which combined with the boundary fill made it look like everything was covered uniformly. Fix: `extract_path_points` now projects the segment-level `info` down to per-point `info` by walking the segment ranges, with a fall-back to legacy per-point `info` when no segment index has arrived yet. Cut rows now render as the bright wide swath the rest of the rendering pipeline was already designed for. Verified against a real Greenhouse capture - 31260 path points + 39 segments in the index, 18 cut + 21 transit alternating cleanly.

## 1.0.3 (2026-05-30)

**Default host port mapping changed to `null` (ingress-only) to avoid port-8000 clashes.** A community user reported that `ports: 8000/tcp: 8000` collided with their Portainer install which already binds host port 8000. Because the add-on already supports HA's built-in ingress (`ingress: true` / `ingress_port: 8000`) for both the sidebar panel and the iframe-card URL pattern `/hassio/ingress/hookii_mower_map/page/<label>`, there's no functional need to also force-bind a host port. Setting the default to `null` means the add-on works out of the box on hosts where port 8000 is already in use. Users who specifically need direct host:port access (e.g. for an iframe served from outside HA) can still set a host port in the add-on's Configuration tab without re-installing.

## 1.0.2 (2026-05-29)

**Yard boundary now actually renders.** Discovered by inspecting a real captured `DEVICE_MAP_V2` payload from a live mower that v1.0.1's `extract_boundary` was looking for the wrong field names entirely - `boundary` / `boundaryPoints` / `regionPoints` / `borderPoints` / `points` are NOT what the May 2026 cloud schema actually emits. The real shape is `DEVICE_MAP_V2.mapDataList[0].mowingAreaElementList[].elementPointList[].{x,y,attr}` for the territory and the parallel `exclusionAreaElementList` for no-go zones. The "translucent fill" was correctly wired into the SVG; it was just always called with an empty point list, hence the missing yard background.

`extract_boundary` now returns BOTH categories: every mowing-area polygon (a Neomow can have multiple disjoint zones, e.g. front yard + back yard) and every exclusion polygon (flower beds, ponds). Mowing areas render as the translucent light-green fill from v1.0.1; exclusions punch back to a dark fill on top, so users can see where the mower will deliberately never reach. The legacy flat-key lookup is kept as a fallback for any older firmware that still uses it.

## 1.0.1 (2026-05-29)

Three visual refinements after the first round of dogfood feedback comparing the SVG output to the Hookii mobile app's polished map view.

- **Cut paths now render as a continuous swept area instead of stripes-with-holes.** The previous version used a stroke-width sized in pixel-equivalents, which made adjacent parallel mowing rows appear as separate stripes with visible gaps between them. The new stroke-width is sized in DATA units (cm) and matches the mower's actual cutting width - so adjacent rows physically overlap and visually merge into a single filled coverage polygon, the same way Hookii's app renders coverage. The width is derived from `REGION_TASK.mowingWidth` when available (the bridge republishes it as part of its standard payload set) with a 25cm default for Neomow X Pro. Override via `MOWING_WIDTH_CM` env var if your mower model has a different cutting width.

- **Yard-boundary polygon is now a translucent fill instead of a dashed outline.** The boundary represents the full mapped yard territory; rendering it as a filled translucent light-green polygon makes the cut-coverage layer on top of it the same way the Hookii app shows it ("light green = yet to mow, darker green = mowed").

- **No more blink on the auto-refresh cycle.** The previous `/page/<label>` HTML used a `<meta http-equiv="refresh" content="10">` page-reload, which caused a visible flash every 10 seconds. The new version uses a JS fetch that swaps the SVG into the existing container's innerHTML - the previous frame stays visible until the new one has arrived, with no white flash in between. The `/all` grid uses the same pattern per-mower so big multi-mower dashboards no longer strobe.

## 1.0.0 (2026-05-29)

- Initial public release.
- Subscribes directly to the local MQTT broker (does not require a Home Assistant token or WebSocket).
- Auto-renders a per-mower SVG yard view with boundary polygon, cut/transit path segments, live trail and current robot position + heading arrow.
- Captures `STATUS` / `DEVICE_MAP_V2` / `ALL_PATH_LIST_V2` / `ALL_PATH_INDEX_V2` and persists to `/data` so a container restart doesn't lose the last-known position or boundary.
- Per-mower colour configurable via the `mowers` option; defaults to a curated palette.
- HTTP API: `/svg/<label>`, `/page/<label>` (10-second auto-refresh iframe-ready HTML), `/state/<label>`, `/all` (grid of every configured mower).
