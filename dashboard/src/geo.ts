import { KOREA, KOREA_BBOX } from "./korea";

export interface OsmChangeset {
  id: number;
  created_at: string;
  changes_count?: number;
  min_lat?: number;
  min_lon?: number;
  max_lat?: number;
  max_lon?: number;
}

function inRing(x: number, y: number, ring: number[][]): boolean {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

export function inKorea(lon: number, lat: number): boolean {
  return KOREA.some(
    (polygon) => inRing(lon, lat, polygon[0]) && !polygon.slice(1).some((hole) => inRing(lon, lat, hole)),
  );
}

/** An edit counts when the centre of a non-empty changeset lies in South Korea. */
export function changesetInKorea(c: OsmChangeset): boolean {
  const values = [c.min_lat, c.min_lon, c.max_lat, c.max_lon];
  if (!((c.changes_count ?? 0) > 0) || values.some((v) => typeof v !== "number" || !Number.isFinite(v))) return false;
  return inKorea((c.min_lon! + c.max_lon!) / 2, (c.min_lat! + c.max_lat!) / 2);
}

/**
 * Counts closed changesets in South Korea, stopping as soon as `need` is reached.
 * Below `need`, the result is a lower bound limited to `maxPages` x 100 changesets.
 */
export async function countKoreaChangesets(
  api: string,
  uid: number,
  need: number,
  fetcher: typeof fetch = fetch,
  maxPages = 5,
): Promise<number> {
  const seen = new Set<number>();
  let count = 0;
  let before: string | undefined;
  for (let page = 0; page < maxPages; page++) {
    const url = new URL("/api/0.6/changesets.json", api);
    url.searchParams.set("user", String(uid));
    url.searchParams.set("bbox", KOREA_BBOX);
    url.searchParams.set("closed", "true");
    url.searchParams.set("limit", "100");
    if (before) url.searchParams.set("time", `2004-01-01T00:00:00Z,${before}`);
    const response = await fetcher(url.toString(), {
      headers: { Accept: "application/json", "User-Agent": "osm-korea-bus-sync-dashboard" },
    });
    if (!response.ok) throw new Error(`OSM changeset query failed: ${response.status}`);
    const list = ((await response.json()) as { changesets?: OsmChangeset[] }).changesets ?? [];
    for (const changeset of list) {
      if (seen.has(changeset.id)) continue;
      seen.add(changeset.id);
      if (changesetInKorea(changeset)) count++;
    }
    if (count >= need || list.length < 100) break;
    before = list[list.length - 1].created_at;
  }
  return count;
}
