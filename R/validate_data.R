# R/validate_data.R
#
# Run this before `quarto render`. Checks that white_creek_temp.csv and both
# APIs return data that isn't empty, isn't obviously wrong, and won't
# silently render a blank chart. Exits non-zero on any failure.
#
# Usage: Rscript R/validate_data.R

source("R/utils.R")

failures <- character()
warn <- function(msg) failures <<- c(failures, msg)
pass <- function(msg) cat("  OK:", msg, "\n")

cat("== White Creek temperature (HOBO) ==\n")
wc <- load_white_creek_temp("data/garden")
if (nrow(wc) == 0) {
  warn("white_creek_temp.csv has no rows -- run scripts/sync_garden_data.py first")
} else {
  pass(paste(nrow(wc), "rows"))
  if (any(is.na(wc$datetime))) warn("white_creek_temp.csv has unparsed datetimes")
  if (any(wc$temp_f < 32 | wc$temp_f > 100, na.rm = TRUE)) {
    warn("temp_f has values outside 32-100°F -- check for a unit or parsing error")
  } else {
    pass("temp_f within plausible range (32-100°F)")
  }
  gap_days <- as.numeric(difftime(Sys.time(), max(wc$datetime), units = "days"))
  # 21 days was an arbitrary guess and fired a false positive on every real
  # run so far -- actual upload cadence is 1-5 weeks between HOBO downloads.
  if (gap_days > 45) warn(paste0("latest reading is ", round(gap_days), " days old -- sync may be stale"))
}

cat("\n== Brazos River gage height (USGS) ==\n")
river <- fetch_usgs_gage_height("USGS-08108700", start = Sys.Date() - 7, end = Sys.Date())
if (nrow(river) == 0) {
  warn("USGS OGC API returned 0 rows for the last 7 days -- check site ID, parameter code, or endpoint status")
} else {
  pass(paste(nrow(river), "rows in the last 7 days"))
  if (any(river$gage_height_ft < 0 | river$gage_height_ft > 60, na.rm = TRUE)) {
    warn("gage_height_ft has an implausible value (<0 or >60 ft) -- check units")
  } else {
    pass("gage_height_ft within plausible range")
  }
}

cat("\n== Weather (NWS, KCLL) ==\n")
now <- fetch_nws_latest("KCLL")
if (is.na(now$temp_c[1])) {
  warn("NWS latest observation returned no temperature -- primary + fallback both failed or station is down")
} else {
  pass(paste0(round(now$temp_c, 1), "°C at ", now$datetime))
}
hx <- fetch_nws_observations("KCLL", n = 24)
if (nrow(hx) == 0) {
  warn("NWS observations history returned 0 rows -- weather trend chart will be empty")
} else {
  pass(paste(nrow(hx), "hourly observations returned"))
}

cat("\n====================\n")
if (length(failures) == 0) {
  cat("All checks passed. Safe to render.\n")
  quit(status = 0)
} else {
  cat(length(failures), "check(s) failed:\n")
  for (f in failures) cat("  - ", f, "\n", sep = "")
  quit(status = 1)
}
