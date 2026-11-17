# Tariff catalogue and load facts

Python 3.11, standard library only. Two modules under `materials/538/`:

- `tariff.py` — `TariffService`, an independent local-time catalogue that
  projects daily wall-clock bands onto real UTC.
- `load.py` — `LoadWarehouse`, which stores piecewise-constant load intervals
  and answers exact energy / cost / demand queries against *any* provider that
  exposes `segments(start, end)`.

Both deviations called out by the data team are fixed:

1. **Repeated local hour / second peak branch.** `TariffService` no longer
   attaches a `tzinfo` to a naive local endpoint. It converts each local
   day/segment boundary and each zone transition to UTC with an explicit
   fold round-trip check, keeps both branches of an ambiguous boundary and
   drops non-existent ones. A catalogue client therefore sees one seamless,
   gap-free UTC timeline even across spring-forward and fall-back.
2. **Irregular records treated as a uniform sample.** `LoadWarehouse.query`
   no longer infers a bucket size from a row length. Energy is integrated over
   real seconds, cost is additionally split at every tariff boundary, and the
   rolling demand is an exact search over the continuous start second with
   eligibility checks for observation coverage and peak status.

Run the demonstration and the full verification:

```sh
cd materials/538
python3.11 probe.py      # ordinary UTC example + the two fixed deviations
python3.11 verify.py     # 21 assertion groups, includes randomized differentials
```

## Public entries

`TariffService(spec)` — `spec` has `timezone` (IANA name), `weekdays`
(ISO 1..7, each a complete left-closed/right-open `HH:MM` day table) and an
optional `holidays` mapping of `YYYY-MM-DD` to a whole-day replacement table.
Validation is strict: complete contiguous coverage of `00:00..24:00`,
non-empty `band`, non-negative rational `rate`, boolean `peak`.

`segments(start, end)` — integer UTC seconds, returns ordered, seamless,
non-overlapping segments covering `[start, end)`. Adjacent segments with equal
`band`/`rate`/`peak` are merged; `rate` is a reduced rational string.

`LoadWarehouse.put(series, rows)` — atomically replaces one series. Rows carry
a unique non-empty `id`, integer `start`/`end` and a non-negative rational
`kw`. Rows may be unsorted and may contain gaps; an invalid row or duplicate id
rejects the whole replacement and preserves the previous series.

`query(series, start, end, provider, window_seconds=900)` — takes exactly one
deep copy of `provider.segments(start, end)` and uses it for the entire
response (energy, cost, by-band, demand). It never consults a local catalogue
or an earlier snapshot, and accepts a third-party provider that only exposes
`segments`. Returns `energy_kwh`, `cost`, `by_band` (sorted by band name, equal
names merged, including zero-power rows with positive overlap), `coverage_seconds`,
`missing_seconds`, `demand_kw`, `window`, `window_sources`. All numeric results
are reduced rational strings; zero is `"0"`.

## Representation choice: interval breakpoints, not integer prefixes

The implementation uses **interval breakpoints with cumulative curves**, not a
per-second array or an integer-prefix table.

- The tariff layer enumerates only the local day/segment boundaries plus zone
  transitions that fall inside the 7-day request (at most a few hundred
  points), classifies the interior of each resulting interval once, and merges
  neighbours. Cost is then a two-pointer sweep over load rows and tariff cells.
- Demand uses cumulative energy/observed-seconds and cumulative peak-seconds
  curves keyed on those breakpoints. The window average is piecewise linear in
  the start second, so its extreme lies in the finite candidate set
  `{b, b-W}` (plus integer neighbours and the domain ends) for every breakpoint
  `b` and the window length `W`. This is exact for every integer `s`, finds
  windows that end mid-row, and is independent of bucket alignment.

**Time / space impact.** With `R` load rows, `B` tariff boundaries and `K`
zone transitions, storage is `O(R + B + K)` and a query is `O((R+B) log(R+B))`
for the sweeps plus `O(R+B)` candidates. A 7-day query at the 10,000-row limit
runs in roughly 0.2 s and stores a few tens of thousands of rationals, *not*
604,800 per-second entries. The distance from integer-prefix alternatives is
asymptotic: a per-second table would need `86400 * days` cells per query (up to
604,800 for 7 days), and a sparse long-span load (a few rows spread over days)
would allocate and scan mostly-empty prefix space. Interval breakpoints scale
with the number of *actual* rows and tariff changes, so sparse long spans stay
small while dense spans stay linear in rows; the rolling-extreme candidate set
grows only with distinct breakpoints, never with elapsed wall-clock seconds.

The cost of this choice is arithmetic exactness: all values are
`fractions.Fraction`, so no floating-point rounding is introduced at any step.
