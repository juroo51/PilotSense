# PilotSense input data format — v2 proposal

A future-proof replacement for the raw NDJSON logs that devices currently push to
`POST /api/flights/{flight_id}/data`. Goals, in the order the request framed them:
**scalability, encryption, device→app transport, variable entry types, and human
readability for debugging.**

Nothing here is wired into the app yet — these are example files plus the reasoning:

- [`sample_session.ndjson`](sample_session.ndjson) — the new per-line record format.
- [`sample_batch_envelope.json`](sample_batch_envelope.json) — how a device wraps and sends a batch.

---

## 1. What the current format does and where it hurts

Each per-flight file is heterogeneous NDJSON — a line carries GPS **or** ADS-B **or**
IMU fields, and the loader infers the type from *which keys are present*.

```json
{"Gps_data": "ok", "Gps_time": "16:38:40", "Gps_datum": "13-07-26", "Gps_lat": 4810.02255, "Gps_latsign": "N", "Gps_lon": 1711.71541, "Gps_lonsign": "E", "Gps_speed": 0, "Gps_tr": 0.000, "Gps_alt": 128}
{"Adsb_HexId": 505861, "Adsb_Fs": "no alert, no SPI, aircraft is airborne", "Adsb_alt": 325, "Adsb_alt_type": "feet", "Adsb_msg": 60, "Adsb_indic_air_speed": 91}
```

| # | Problem today | Consequence |
|---|---------------|-------------|
| 1 | **Only GPS lines have a timestamp.** ADS-B/IMU lines carry no time. | ADS-B is placed by *interpolating* between bracketing GPS fixes (`extract_adsb_message_points()`). Time is approximate and breaks if GPS drops. |
| 2 | **Type is implicit** (guessed from keys). | Adding a new record kind means teaching the parser new key-sets; ambiguous lines are possible. |
| 3 | **`Adsb_HexId: 505861` is emitted unquoted** — invalid JSON. | A regex repairs it before every parse. Fragile. |
| 4 | **NMEA `DDMM.MMMM` coords + separate `*sign` fields.** | Every reader must run `_nmea_to_decimal()`; not human-obvious. |
| 5 | **Split date/time, 2-digit year, no explicit zone.** | `13-07-26` + `16:38:40` must be reassembled; ambiguous and locale-ish. |
| 6 | **No schema version.** | Can't evolve the format without guessing which shape a file is. |
| 7 | **No sequence numbers.** | Can't detect dropped/reordered lines; **re-sending a batch duplicates data** (append is not idempotent). |
| 8 | **No integrity or payload auth.** Key travels in `X-Api-Key` header only; body is plaintext, uncompressed. | No tamper detection, no replay protection, and verbose repeated keys waste bandwidth. |

## 2. Design in one line

**Keep NDJSON (streamable, greppable, one self-describing JSON record per line), but
make every record carry `v` / `t` / `ts` / `seq`, normalize units, and wrap batches in
a signed, compressible envelope for transport.**

## 3. The record (line) format

```json
{"v":1,"t":"gps","seq":1,"ts":"2026-07-13T16:38:40.000Z","d":{"fix":"3d","lat":48.167043,"lon":17.195257,"alt_m":128,"speed_kt":0,"track_deg":0.0,"sats":9,"hdop":0.9}}
```

| Field | Meaning | Why it's there |
|-------|---------|----------------|
| `v`   | schema version (int) | evolve safely — bump on breaking change (problem #6) |
| `t`   | **source/record type**: `meta` `gps` `adsb` `mods` `imu` `baro` `event` … | **explicit** discriminator — you can tell GPS vs ADS-B vs Mode-S at a glance; unknown types are skipped, not fatal (problems #2, "entries vary") |
| `ts`  | ISO-8601 UTC, **millisecond** precision, on **every** record | no more interpolation; every message is self-timed and orderable (problem #1, #5) — see §3.2 |
| `seq` | monotonic per-session counter | gap/dup/reorder detection + idempotent append (problem #7) |
| `d`   | the typed payload | normalized, valid JSON, human units (problems #3, #4) |

Design choices baked into `d`:

- **Decimal degrees, signed** (`lat`, `lon`) — no NMEA, no sign fields, no repair regex.
- **Units in the key** (`alt_m`, `speed_kt`, `alt_ft`, `ias_kt`, `gs_kt`, `tas_kt`) so a
  value is unambiguous on sight; the `meta` record also declares defaults.
- **`icao24` is always a quoted string** — kills problem #3 permanently.
- Nested groups where it reads better (`acc.{x,y,z}`, `att.{pitch,roll,yaw}`).

### 3.1 Telling GPS / ADS-B / Mode-S apart

The `t` field is the answer to "what kind of message is this?" — one look, no
key-sniffing. The three telemetry sources and how they differ:

| `t` | Source | Carries its own position/time? | Typical `d` fields |
|-----|--------|-------------------------------|--------------------|
| `gps`  | onboard GPS receiver | **yes** — this is the position truth | `fix`, `lat`, `lon`, `alt_m`, `speed_kt`, `track_deg`, `sats`, `hdop` |
| `adsb` | 1090 MHz **extended squitter** (DF17/18) — the aircraft *broadcasts* its own state | **yes** — position is in the message | `df`, `tc` (type code), `lat`, `lon`, `alt_ft`, `gs_kt`, `nic` |
| `mods` | **Mode-S** replies (DF4/5/11/20/21), incl. Comm-B **BDS** registers | **no** — altitude/speed/ident only, no position | `df`, `bds`, `squawk`, `alt_ft`, `ias_kt`, `tas_kt`, `hdg_deg`, `callsign`, `status` |

> **Why `mods` is a real category, not a rename.** In your current logs
> `Adsb_msg: 20 / 50 / 60` are **BDS registers 2,0 / 5,0 / 6,0** (aircraft
> identification / track-and-turn / heading-and-speed) — those are Mode-S Comm-B
> Enhanced-Surveillance replies, **not** ADS-B extended squitter. Tagging them `mods`
> (with `bds`) separates "aircraft self-broadcast its GPS position" (`adsb`) from
> "we interrogated/overheard a Mode-S reply that has no position" (`mods`). Consumers
> that only want position-bearing messages filter on `t in {gps, adsb}`.

For Mode-S, keep both `df` (downlink format, the frame type on the wire) and, for Comm-B,
`bds` (the register that says what the 56-bit message body means). That's enough to decode
or re-derive any field without guessing.

### 3.2 Millisecond timestamps on every message

The core fix for problem #1: **every message carries its own time, at millisecond
resolution** — not just GPS lines, and not the old 1-second GPS resolution.

The device keeps one millisecond clock and stamps each message with UTC `ts` the moment
it's captured. To make that UTC *correct* rather than drifting, it syncs the clock to GPS:
the whole-second UTC comes from the GPS `$GxRMC` sentence, and the sub-second part is the
device's own millisecond counter since that second began. No monotonic counter is exposed
in the record — `ts` is the single source of time.

The `meta` record advertises the clock's trustworthiness:

```json
"clock": { "source": "gps", "state": "synced", "precision": "ms" }
```

- `state: "free"` — no GPS lock yet; `ts` is best-effort (device millis since boot), still
  fine for **ordering** because `ts` is monotonic within a session.
- `state: "synced"` — GPS-locked; `ts` is accurate wall-clock UTC to ~ms.
- `state: "holdover"` — GPS lost; coasting on the last sync, sub-second drifts slowly.

Millisecond resolution is enough to order and interval-measure ADS-B/Mode-S bursts (a few
messages per second) without the interpolation the old format relied on. If two records
share the same `ts`, `seq` still gives a total order.

**First line is a `meta` record** describing the session — device, firmware, aircraft
identity, clock source, unit defaults — so a file/stream is self-describing without the
filename or out-of-band knowledge:

```json
{"v":1,"t":"meta","seq":0,"ts":"2026-07-13T16:38:39.500Z","d":{"session_id":"2026-07-13T16-35-00Z_OMCAT","device_id":"ps-esp32-0007","fw":"pilotsense-fw 2.3.1","aircraft":{"reg":"OM-CAT","icao24":"505861","callsign":"OMCAT"},"clock":{"source":"gps","state":"synced","precision":"ms"},"units":{"alt":"m","speed":"kt"}}}
```

(Clock discipline and the no-RTC case are covered in §3.2.)

### Legacy → v2 field map (for the ingest shim)

Note the **source split**: current `Adsb_*` lines actually mix two sources — extended
squitter (→ `t:"adsb"`) and Mode-S Comm-B / identity replies (→ `t:"mods"`). The shim
routes by `Adsb_msg`: BDS registers `20/50/60` and identity/altitude replies become `mods`;
DF17/18 position/velocity squitters become `adsb`.

| Legacy | v2 |
|--------|----|
| `Gps_lat`+`Gps_latsign` / `Gps_lon`+`Gps_lonsign` | `gps` → `d.lat` / `d.lon` (signed decimal deg) |
| `Gps_datum`+`Gps_time` | `ts` (ms, GPS-synced) |
| `Gps_alt` / `Gps_speed` / `Gps_tr` / `Gps_data` | `d.alt_m` / `d.speed_kt` / `d.track_deg` / `d.fix` |
| `Adsb_HexId` | `d.icao24` (string) |
| `Adsb_msg` = `20`/`50`/`60` | `mods` → `d.bds` = `"2,0"`/`"5,0"`/`"6,0"` |
| `Adsb_alt`+`Adsb_alt_type` | `d.alt_ft` / `d.alt_m` |
| `Adsb_gnd_speed` / `Adsb_indic_air_speed` / `Adsb_true_air_speed` | `d.gs_kt` / `d.ias_kt` / `d.tas_kt` |
| `Adsb_Fs` / `Adsb_squawk` / `Adsb_callsign` | `d.status` / `d.squawk` / `d.callsign` |

## 4. Transport: device → app

Device buffers records and POSTs a **batch envelope** (see
[`sample_batch_envelope.json`](sample_batch_envelope.json)) to `POST /api/flights/{id}/data`:

```json
{
  "schema": "pilotsense.batch/1",
  "device_id": "ps-esp32-0007",
  "session_id": "2026-07-13T16-35-00Z_OMCAT",
  "seq_start": 1, "seq_end": 10, "count": 10,
  "sent_at": "2026-07-13T16:40:00.120Z",
  "encoding": "ndjson+gzip+base64",
  "encryption": "none",
  "payload": "<base64(gzip(NDJSON lines))>",
  "sig": "hmac-sha256=<hex>"
}
```

**Scalability**

- **Batching + gzip.** NDJSON's repeated keys compress ~5–10×; envelope declares the
  `encoding` so the server knows how to unpack.
- **Idempotent append via `seq`.** The server tracks the highest stored `seq` per
  `session_id` and drops anything ≤ that. Retries and overlapping batches are safe → no
  duplicate points. The response **acks the highest stored seq** so a device knows exactly
  where to resume after a dropped connection.
- **Resumable / shardable.** `session_id` is the unit of ordering and storage; batches can
  arrive out of order and still merge correctly by `seq`.
- Small, fixed per-batch metadata; NDJSON stays the interchange, so a columnar/binary
  encoding can be added later behind a new `encoding` value without touching the record schema.

**Encryption & integrity** (defense in depth)

1. **Confidentiality in transit — TLS 1.2+** on the HTTP endpoint. Baseline, non-optional.
2. **Authenticity + integrity — HMAC-SHA256.** `sig` is computed over the canonical bytes
   `device_id | session_id | seq_start | seq_end | sent_at | payload` using the device's
   shared secret (the existing `PILOTSENSE_DEVICE_KEYS` value). The server recomputes and
   `secrets.compare_digest`s it. Detects tampering and proves the batch came from the device
   — stronger than a bare key echoed in a header.
3. **Replay protection.** `sent_at` must be within a freshness window (e.g. ±5 min), and any
   `seq` ≤ last-stored is rejected. A captured batch can't be replayed to inject or duplicate.
4. **Optional end-to-end payload encryption** for untrusted relays/queues: set
   `encryption:"aes-256-gcm"`, encrypt the gzip blob with a per-device key, and carry the
   `nonce` in the envelope. Off by default — TLS + HMAC is the sensible baseline, and
   encrypting the payload makes it un-greppable, which fights goal #5 below.

**Human readability for debugging** (goal #5)

- The record format is plain NDJSON — one self-describing line — so `tail -f`, `grep t":"adsb"`,
  and `jq` all work on a stored session.
- A **debug transport variant**: `encoding:"ndjson"` puts the raw NDJSON straight in `payload`
  (no gzip/base64) and omits `sig`. End-to-end readable during bring-up; the server accepts it
  only from devices/keys flagged as non-production.

## 5. "Entries can vary" — extensibility rules

- New record kinds = a new `t` value + a `d` shape. **Unknown `t` is logged and skipped, never
  fatal** — old servers tolerate new devices.
- Additive `d` fields don't need a version bump; removing/renaming a field or changing units does
  (bump `v`).
- `event` records let the device report its own state inline (`gps_reacquired`, `sensor_fault`,
  `low_battery`) on the same timeline as telemetry.

## 6. Suggested rollout

1. Add a v2 parser branch: if the first line is `t:"meta"` (or the envelope has `schema`), parse
   v2; otherwise fall back to the legacy `_parse_log_line` path. Both feed the same internal
   DataFrame, so `analysis.py` / caching are unchanged.
2. Teach `ingest_flight_data` to unwrap the envelope (verify HMAC, check freshness, dedupe by
   `seq`) and to return the ack seq.
3. Convert existing `data/parsed/*.json` with a one-shot legacy→v2 script (the §3 map) so all
   storage is uniform; keep the shim for in-flight legacy devices until firmware ships v2.
