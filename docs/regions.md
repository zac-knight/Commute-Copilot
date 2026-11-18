# Other states (researched, then removed)

The app is South East Queensland only. Other states were connected for a while and removed; this keeps
what was learned in case it is ever wanted again. All were checked against the publishers' own pages.

| State | Finding |
|---|---|
| NSW | The open-data portal's own file download (not the keyed API) is free, no registration, Creative Commons Attribution. |
| Victoria | Public GTFS Schedule download, Creative Commons Attribution (a zip of per-mode zips). Live data needs a key. |
| South Australia | Adelaide Metro GTFS and live feeds are public, Creative Commons Attribution. |
| Northern Territory | Darwin and Alice Springs bus feeds, public, Creative Commons Attribution. |
| ACT | Light rail feed open and current (CC BY); the bus feed needs developer credentials. |
| WA | A click-through licence from the Public Transport Authority: free to use, no use of its trademarks with the data, credit "available free of charge from www.transperth.wa.gov.au". |
| Tasmania | No confirmed open licence; the official page blocks automated access. |

Real-world feed quirks found along the way (the ingest still handles them): a blank line before the CSV
header (NT), padded header names such as `stop_id, stop_name` (WA), extended route type codes and trams/ferries
(NSW/VIC), and the same station defined in two feeds under different ids.
