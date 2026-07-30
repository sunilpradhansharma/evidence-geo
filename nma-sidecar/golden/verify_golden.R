# Golden-dataset verification for the netmeta sidecar (Phase 6, stop-and-review gate #4).
#
# Run against a live sidecar:
#     docker build -f Dockerfile.nma -t evidence-nma-sidecar .
#     docker run -d -p 8100:8000 --name nma evidence-nma-sidecar
#     docker exec nma R -q -f /app/golden/verify_golden.R
#
# WHAT THIS PROVES, AND WHAT IT DOES NOT.
#
# It runs netmeta's own published datasets through TWO paths - a direct netmeta() call and
# the sidecar's HTTP endpoint - and asserts every league-table cell agrees. That tests the
# thing the sidecar actually adds risk to: the arm-frame construction, the pairwise()
# conversion, the matrix extraction, the log/exp scale handling and the JSON emission. Any
# of those could silently distort a correct netmeta result, and this is what would catch it.
#
# It does NOT independently validate netmeta itself. netmeta is the reference package -
# validated, cited in HTA submissions, and the reason this is a sidecar rather than a
# hand-rolled implementation - so re-deriving its arithmetic here would be asserting our
# arithmetic over its own.
#
# STILL OUTSTANDING, DELIBERATELY NOT FAKED: comparing these outputs against the values
# PRINTED in the netmeta JSS paper. That is a transcription a statistical reviewer should
# do from the paper, not something to generate from the same package under test - a
# generated "expected" file would agree with itself by construction and prove nothing.
# Senn2013 is also absent below for a concrete reason, recorded rather than skipped: it is
# a CONTRAST-level dataset, and neither the wire contract's Python side nor this sidecar
# implements contrast-level transport yet. Adding it is a contract change, not a fixture.

library(netmeta)
library(jsonlite)

BASE <- Sys.getenv("SIDECAR_URL", "http://127.0.0.1:8000")
TOLERANCE <- 1e-8

# DELIBERATELY DUPLICATED FROM plumber.R, NOT IMPORTED.
#
# The gate must not source the thing it is testing. If it read the sidecar's own policy
# table, a wrong table would agree with itself and the check would pass on both paths being
# wrong in the same way - the same "a clean diff proves reproducibility, not correctness"
# error this file's header warns about.
#
# The cost of duplicating is drift, so: THESE TWO TABLES MUST MATCH. If they ever disagree,
# the gate silently compares two different analyses and passes anyway, which is worse than
# failing. A mismatch is the one failure mode this file cannot detect about itself.
ZERO_EVENT_OF <- list(
  TREATMENT_ARM_CONTINUITY_CORRECTION = list(incr = 0.5, allstudies = TRUE),
  FIXED_0_5_CORRECTION                = list(incr = 0.5, allstudies = TRUE),
  EXCLUDE_ZERO_EVENT_STUDIES          = list(incr = 0,   allstudies = FALSE)
)
POLICY <- "TREATMENT_ARM_CONTINUITY_CORRECTION"

post_nma <- function(payload) {
  con <- curl::curl(paste0(BASE, "/nma"), handle = curl::new_handle(
    postfields = toJSON(payload, auto_unbox = TRUE, na = "null"),
    httpheader = c("Content-Type" = "application/json")
  ))
  on.exit(close(con))
  fromJSON(paste(readLines(con, warn = FALSE), collapse = ""), simplifyVector = FALSE)
}

# Turn an arm-level data frame into the contract's {studies: [{study_id, arms: [...]}]}.
as_payload <- function(frame, measure, model, policy = POLICY) {
  studies <- lapply(split(frame, frame$studlab), function(rows) {
    list(
      study_id = as.character(rows$studlab[1]),
      arms = lapply(seq_len(nrow(rows)), function(i) list(
        treatment = as.character(rows$treat[i]),
        events = as.integer(rows$event[i]),
        sample_size = as.integer(rows$n[i])
      ))
    )
  })
  list(
    contract_version = "1",
    outcome_type = "binary",
    effect_measure = measure,
    model = model,
    reference_treatment = as.character(sort(unique(frame$treat))[1]),
    zero_event_policy = policy,
    studies = unname(studies)
  )
}

check <- function(name, frame, measure = "odds_ratio", model = "random", policy = POLICY) {
  cat("\n==", name, "==\n")
  sm <- if (measure == "odds_ratio") "OR" else "RR"
  zero <- ZERO_EVENT_OF[[policy]]

  # The zero-event arguments must match what the sidecar applies for this policy. Calling
  # pairwise() with bare defaults here compares two DIFFERENT analyses and reports the
  # methodological gap between them as a parity failure - which is what this gate did on
  # its first run, erroring on Dong2013 before it reached the sidecar at all.
  pw <- pairwise(
    treat = frame$treat, event = frame$event, n = frame$n,
    studlab = frame$studlab, sm = sm,
    addincr = FALSE, incr = zero$incr, allstudies = zero$allstudies
  )
  direct <- netmeta(
    pw,
    comb.fixed = identical(model, "fixed"),
    comb.random = identical(model, "random"),
    reference.group = as.character(sort(unique(frame$treat))[1]),
    sm = sm
  )

  served <- post_nma(as_payload(frame, measure, model, policy))
  if (!is.null(served$error)) {
    cat("  FAIL sidecar returned an error:", served$error, "\n")
    return(FALSE)
  }

  est <- if (identical(model, "random")) direct$TE.random else direct$TE.fixed
  worst <- 0
  compared <- 0
  for (cell in served$contrasts) {
    reference <- est[cell$treatment, cell$comparator]
    if (is.na(reference)) next
    # The sidecar reports on the natural scale; netmeta holds the analysis scale.
    delta <- abs(log(cell$estimate) - reference)
    worst <- max(worst, delta)
    compared <- compared + 1
  }

  cat("  package        ", served$package_version, "\n")
  cat("  cells compared ", compared, "\n")
  cat("  worst log delta", format(worst, scientific = TRUE), "\n")

  if (compared == 0) {
    cat("  FAIL no comparable cells - the sidecar returned a league table netmeta did not\n")
    return(FALSE)
  }
  if (worst > TOLERANCE) {
    cat("  FAIL beyond tolerance", TOLERANCE, "\n")
    return(FALSE)
  }
  cat("  OK\n")
  TRUE
}

data(Woods2010)
woods <- data.frame(
  studlab = Woods2010$author,
  treat   = Woods2010$treatment,
  event   = Woods2010$r,
  n       = Woods2010$N,
  stringsAsFactors = FALSE
)

data(Dong2013)
dong <- data.frame(
  studlab = Dong2013$id,
  treat   = Dong2013$treatment,
  event   = Dong2013$death,
  n       = Dong2013$randomized,
  stringsAsFactors = FALSE
)

results <- c(
  check("Woods2010 (COPD exacerbations, arm-level binary)", woods),
  check("Dong2013 (COPD mortality, arm-level binary)", dong),

  # THE FIXED-EFFECT CASE EXISTS TO CATCH A MIX-UP, NOT TO RE-TEST THE ARITHMETIC.
  #
  # Every case above requests `random`, so for its first two runs this gate could not tell
  # the two models apart: a sidecar that ignored `model` entirely and always returned random
  # estimates would have passed clean. That is not hypothetical - `league_table` really did
  # decide fixed-vs-random by reading the deprecated `net$comb.random` back off the fitted
  # object, which reads NULL once netmeta drops it and would have silently served FIXED
  # estimates for every random request. Narrower intervals, no error, straight into the
  # league table.
  #
  # `check` compares against `TE.fixed` when asked for fixed, so if the sidecar serves the
  # wrong model the deltas blow past tolerance instead of agreeing by luck.
  check("Woods2010 (fixed effect - guards the model switch)", woods, model = "fixed")
)

cat("\nSenn2013 is NOT run here: it is contrast-level, and contrast transport is not\n")
cat("implemented on either side of the wire contract yet. That is an open contract item,\n")
cat("recorded rather than skipped silently.\n")

if (all(results)) {
  cat("\nGOLDEN PARITY: PASS\n")
  quit(status = 0)
}
cat("\nGOLDEN PARITY: FAIL\n")
quit(status = 1)
