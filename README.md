

# TAMU Gardens in Focus

Environmental monitoring dashboard for the Hu Lab TGIF site. Data includes: White Creek water temperature (HOBO logger), Brazos River gage height (USGS), and weather (NWS station KCLL).

Site: https://shu251.github.io/tamu-gardens-in-focus/

## Protocol

### When new HOBO data lands in #tgif

Set up a `SLACK_BOT_TOKEN` to watch Slack #tgif channel.

1.  When someone posts a `.xlsx` export to `#tgif`. Filename should be standard from HOBO loggers: `C - 35 YYYY-MM-DD HH_MM_SS TZ (Data TZ).xlsx`.
2.  Fridays at 11:30pm, the Action runs `sync_garden_data.py`, finds all every `.xlsx` in the channel, skips anything already marked `"synced"` in `manifest.json`, downloads and parses the rest, appends new rows to `white_creek_temp.csv`, commits both files.
3.  The dashboard itself does not auto-rebuild.
4.  Following a TGIF sampling event, open R project, run quarto render. this will update `docs/`, this needs to be committed to github before it shows on the website.

Git Actions tab \> "Sync garden data" \> Run workflow. Or run `python3 scripts/sync_garden_data.py` locally with `SLACK_BOT_TOKEN` set.

## Structure

```         
garden-dashboard.qmd          dashboard source (Quarto)
_quarto.yml                   project config, renders to docs/
styles.scss                   Custom theme
R/utils.R                     pulls & parses public data: USGS OGC API, NWS API, HOBO xlsx parsing
scripts/sync_garden_data.py   links to Slack, run by GitHub Actions
data/garden/
  white_creek_temp.csv        parsed HOBO data (tracked in git)
  manifest.json               tracks #tgif file syncing
  raw/                        downloaded .xlsx files (gitignored)
.github/workflows/
  sync-garden-data.yml        
docs/                         rendered site, html
```

## Setup

``` bash
# R packages
Rscript -e 'install.packages(c("tidyverse","httr2","plotly","readxl"))'

# Python (for the sync script, if running locally)
pip3 install slack_sdk openpyxl requests
```

See https://quarto.org/docs/get-started/

## Log notes on website structure:

*last updated July 2026*

1.  Created a Slack bot with `channels:history` and `files:read` scopes, install it to the Hu Lab workspace, invite bot to `#tgif`.

2.  Set `SLACK_BOT_TOKEN` locally and run:

    ``` bash
    python3 scripts/sync_garden_data.py
    ```

3.  Check `data/garden/manifest.json`. In manifest, files will change to "synced"

4.  Add `SLACK_BOT_TOKEN` as a repo secret (GitHub \> Settings \> Secrets and variables \> Actions) so the GitHub Actions workflow can run the same sync weekly.

## Data validation

With automation, determined that data validation functions were needed.

1.  Run the validator:

    ``` bash
    Rscript R/validate_data.R
    ```

    Checks: CSV has rows, datetimes parse, temp_f is in a plausible range (32-100°F), USGS returns rows with gage height in a plausible range (0-60 ft), NWS returns a current temperature and 24h of history. Exits non-zero and lists every failure if something's wrong.

2.  Render and open the result:

    ``` bash
    quarto render
    open docs/garden-dashboard.html
    ```

    Confirm all three pages load, each chart has data (not a blank axis), and zoom/pan works on at least one chart.

3.  Visually confirm things worked!

## Known gaps

- HOBO xlsx column layout (datetime in column B, temp in column C) matches a synthetic file built from the documented HOBO export structure and actual `#tgif` filenames -- not a real file, since binary `.xlsx` content isn't readable from a chat session. Worth a spot-check against your first real sync, but the parsing logic itself -- including the midnight-drop fix -- has been verified.
- USGS/NWS calls are correct against their published API docs and fail gracefully when unreachable, but have never returned real data in any session so far -- first real confirmation happens on your machine or in Actions.
- Added `flag_air_exposure()` (R/utils.R): filters readings outside a plausible 32-100°F range from the White Creek chart and valueboxes, without ever modifying the source CSV. Confirmed 2026-07-28 against real data: 2 of 2403 readings hit 102-106°F, both mid-afternoon on different dates -- the signature of the logger sitting above a low waterline in direct sun, not a parsing error. The dashboard now shows a visible note ("N readings excluded...") rather than silently dropping them or plotting a misleading spike. Verified end-to-end with injected synthetic spikes: filter removes exactly the injected points, raw CSV keeps them intact, rendered HTML shows the correct count and wording.

### Troubleshooting

**Manifest shows a file as `"failed"`.** Check `manifest.json -> synced_files -> <file_id> -> error`. Usually a column mismatch -- `parse_hobo_xlsx()` assumes datetime in column B, temp in column C. Open the file, confirm the layout, adjust the column indices in both `scripts/sync_garden_data.py` and `R/utils.R::read_hobo_xlsx()` if HOBO changed the export format.

**Chart is blank on one page.** Run `Rscript R/validate_data.R` first -- it isolates which of the three sources (HOBO CSV, USGS, NWS) is empty before you go looking in the `.qmd`.

**River page is empty or gage height looks wrong.** USGS decommissioned the legacy NWIS endpoint; confirm `fetch_usgs_gage_height()` in `R/utils.R` is still pointed at `api.waterdata.usgs.gov/ogcapi/v0/collections/continuous`, not the old `waterservices.usgs.gov` URL. Site ID must include the `USGS-` prefix (`USGS-08108700`, not `08108700`) -- the OGC API rejects the bare number.

Also: the site is `USGS-08108700` ("Brazos Rv at SH 21 nr Bryan, TX"), **not** `USGS-08109500` ("Brazos Rv nr College Station, TX" -- the name that actually matches this project). Confirmed 2026-07-28 via `time-series-metadata`: 08109500 has zero indexed time series for any parameter right now, so it returns nothing no matter how the query is built. 08108700 is close by on the same river and does have live gage height data. If you'd rather use 08109500 for the name match, check whether USGS has restored it before switching back (`https://waterdata.usgs.gov/monitoring-location/USGS-08109500/`).

**Weather page shows "not reported" for precip.** Not a bug. NWS returns `null` for precipitation more often than it returns `0` -- `null` means the station didn't report, not that it didn't rain. Do not change this to display `0`; that was the original bug in the HTML version.

**Action runs but nothing commits.** Either no new files were in `#tgif` (expected, most weeks) or `SLACK_BOT_TOKEN` is missing/expired -- check the Action's log for `SLACK_BOT_TOKEN not set` or a Slack API auth error.

**Sync succeeds locally but fails in Actions.** Almost always the secret. Confirm it's set at Settings \> Secrets and variables \> Actions, not just exported in your local shell.

**Bug found and fixed:** `read_hobo_xlsx()` was silently dropping any reading logged at exactly midnight. `readxl` returns Excel datetime cells as `POSIXct` already; routing that through `as.character()` before `parse_date_time()` strips the time component when it's `00:00:00`, so the date-only string then fails to match any of the datetime formats. With 15-minute logging intervals, that's one dropped reading every single day. Fixed by skipping the string round-trip when the column is already `POSIXct`/`Date`. The Python parser never had this bug; it keeps `openpyxl`'s native `datetime` object throughout.

If a degree symbol ever renders as `<U+00B0>` instead of °, that's a locale issue, not a code bug. confirmed in a sandbox defaulting to a POSIX/C locale. Try Render with `LC_ALL=en_US.UTF-8 quarto render` if it happens. macOS defaults to UTF-8 already. Likely PC to mac issue
