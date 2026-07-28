# R/utils.R
# Shared data-access functions for the TAMU Gardens in Focus dashboard.
# All functions return tidy tibbles. Written for tidyverse + httr2.
#
# NOTE: I have not been able to run/render this against live data in this
# session (no R runtime available here, and no Slack bot token to pull real
# HOBO files). Endpoints and response shapes are taken from USGS/NWS API docs
# as of July 2026 -- test against a real render before trusting the output.

library(tidyverse)
library(httr2)
library(lubridate)

# ---- Brazos River gage height (USGS OGC API) --------------------------------

#' Pull gage height (parameter 00065, ft) for a USGS site from the OGC API.
#'
#' Uses the "continuous" collection, which replaced the legacy NWIS
#' instantaneous-values service (decommissioned 2026-2027). See:
#' https://api.waterdata.usgs.gov/docs/ogcapi/
fetch_usgs_gage_height <- function(site_id = "USGS-08109500",
                                    start = Sys.Date() - 30,
                                    end = Sys.Date(),
                                    parameter_code = "00065") {

  base_url <- "https://api.waterdata.usgs.gov/ogcapi/v0/collections/continuous/items"

  datetime_range <- paste0(
    format(as.POSIXct(start), "%Y-%m-%dT%H:%M:%SZ"), "/",
    format(as.POSIXct(end), "%Y-%m-%dT%H:%M:%SZ")
  )

  req <- request(base_url) |>
    req_url_query(
      monitoring_location_id = site_id,
      parameter_code = parameter_code,
      datetime = datetime_range,
      f = "json",
      limit = 10000
    ) |>
    req_timeout(20) |>
    req_retry(max_tries = 3)

  resp <- tryCatch(req_perform(req), error = function(e) NULL)

  if (is.null(resp) || resp_status(resp) != 200) {
    warning("USGS OGC API request failed for ", site_id)
    return(tibble(datetime = as.POSIXct(character()), gage_height_ft = double()))
  }

  body <- resp_body_json(resp)

  if (length(body$features) == 0) {
    return(tibble(datetime = as.POSIXct(character()), gage_height_ft = double()))
  }

  map_dfr(body$features, function(f) {
    tibble(
      datetime = ymd_hms(f$properties$time, quiet = TRUE),
      gage_height_ft = suppressWarnings(as.numeric(f$properties$value)),
      approval_status = f$properties$approval_status %||% NA_character_
    )
  }) |>
    arrange(datetime)
}

# ---- Weather (NWS, station KCLL / Easterwood Field) --------------------------

#' Pull the last N hourly observations for a station (for the weather trend
#' chart). Same fallback logic as fetch_nws_latest.
fetch_nws_observations <- function(station = "KCLL", n = 168) {
  url <- paste0("https://api.weather.gov/stations/", station, "/observations?limit=", n)
  ua <- "(TAMU Gardens in Focus dashboard, contact: hu-lab@tamu.edu)"

  req <- request(url) |>
    req_headers(`User-Agent` = ua, Accept = "application/geo+json") |>
    req_timeout(12) |>
    req_retry(max_tries = 2)

  resp <- tryCatch(req_perform(req), error = function(e) NULL)
  if (is.null(resp) || resp_status(resp) != 200) {
    warning("NWS observations request failed for station ", station)
    return(tibble(datetime = as.POSIXct(character()), temp_c = double(), precip_mm = double()))
  }

  feats <- resp_body_json(resp)$features
  map_dfr(feats, function(feat) {
    p <- feat$properties
    tibble(
      datetime = ymd_hms(p$timestamp, quiet = TRUE),
      temp_c = p$temperature$value %||% NA_real_,
      precip_mm = p$precipitationLastHour$value %||% NA_real_,
      description = p$textDescription %||% NA_character_
    )
  }) |>
    arrange(datetime)
}

#' Pull the latest observation for a station, with a fallback endpoint and
#' a hard timeout, per prior findings on NWS API reliability.
fetch_nws_latest <- function(station = "KCLL") {

  primary <- paste0("https://api.weather.gov/stations/", station, "/observations/latest")
  fallback <- paste0("https://api.weather.gov/stations/", station, "/observations")

  ua <- "(TAMU Gardens in Focus dashboard, contact: hu-lab@tamu.edu)"

  get_obs <- function(url) {
    req <- request(url) |>
      req_headers(`User-Agent` = ua, Accept = "application/geo+json") |>
      req_timeout(12) |>
      req_retry(max_tries = 2)
    tryCatch(req_perform(req), error = function(e) NULL)
  }

  resp <- get_obs(primary)

  # fallback: most recent item from the observations list
  props <- NULL
  if (!is.null(resp) && resp_status(resp) == 200) {
    props <- resp_body_json(resp)$properties
  } else {
    resp2 <- get_obs(fallback)
    if (!is.null(resp2) && resp_status(resp2) == 200) {
      feats <- resp_body_json(resp2)$features
      if (length(feats) > 0) props <- feats[[1]]$properties
    }
  }

  if (is.null(props)) {
    warning("NWS API request failed for station ", station, " (primary + fallback)")
    return(tibble(
      datetime = as.POSIXct(NA), temp_c = NA_real_, wind_speed_kmh = NA_real_,
      precip_mm = NA_real_, description = NA_character_
    ))
  }

  tibble(
    datetime = ymd_hms(props$timestamp, quiet = TRUE),
    temp_c = props$temperature$value %||% NA_real_,
    wind_speed_kmh = props$windSpeed$value %||% NA_real_,
    # precipitation is frequently null from this endpoint -- do not coerce to 0,
    # a null here means "not reported," not "no rain." keep it NA and label
    # it as such downstream (this was one of the two bugs fixed in the prior
    # HTML build; the same trap applies here).
    precip_mm = props$precipitationLastHour$value %||% NA_real_,
    description = props$textDescription %||% NA_character_
  )
}

# ---- White Creek temperature (HOBO logger exports) ---------------------------

#' Parse a single HOBO logger .xlsx export into a tidy tibble.
#'
#' HOBO exports typically have a title row, a header row with units baked
#' into the column name (e.g. "Temp, °F (LGR S/N: ...)"), then data. Column
#' position is more stable than column name across exports, so this reads
#' positionally (2 = datetime, 3 = temp) and renames explicitly. Re-check
#' this against a real export before relying on it -- I have not been able
#' to open one of the actual `#tgif` files in this session.
read_hobo_xlsx <- function(path) {
  raw <- readxl::read_excel(path, skip = 1)

  raw |>
    rename(
      record = 1,
      datetime_raw = 2,
      temp_f = 3
    ) |>
    mutate(
      # readxl already parses Excel datetime cells into POSIXct/Date. Routing
      # those through as.character() -> parse_date_time() silently drops the
      # time component when it's exactly midnight (as.character(POSIXct)
      # prints date-only for 00:00:00), which parse_date_time then fails to
      # match -- quietly losing one reading per day, every day. Only fall
      # back to string parsing if the column truly came in as text.
      datetime = if (inherits(datetime_raw, "POSIXct") || inherits(datetime_raw, "Date")) {
        as.POSIXct(datetime_raw, tz = "America/Chicago")
      } else {
        parse_date_time(
          datetime_raw,
          orders = c("mdy HMS", "mdy HM", "ymd HMS"),
          tz = "America/Chicago"
        )
      },
      temp_f = as.numeric(temp_f),
      temp_c = (temp_f - 32) * 5 / 9
    ) |>
    filter(!is.na(datetime), !is.na(temp_f)) |>
    select(datetime, temp_f, temp_c) |>
    distinct(datetime, .keep_all = TRUE) |>
    arrange(datetime)
}

#' Read + combine every synced HOBO CSV chunk in data/garden/.
load_white_creek_temp <- function(dir = "data/garden") {
  csv_path <- file.path(dir, "white_creek_temp.csv")
  if (!file.exists(csv_path)) {
    return(tibble(datetime = as.POSIXct(character()), temp_f = double(), temp_c = double()))
  }
  read_csv(csv_path, col_types = cols(
    datetime = col_datetime(),
    temp_f = col_double(),
    temp_c = col_double()
  )) |>
    distinct(datetime, .keep_all = TRUE) |>
    arrange(datetime)
}
