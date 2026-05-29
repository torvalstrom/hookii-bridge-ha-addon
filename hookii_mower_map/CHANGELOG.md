# Changelog

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
