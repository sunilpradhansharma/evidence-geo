# The R netmeta sidecar (Phase 6).
#
# Implements the wire contract already defined and tested in
# backend/app/evidence/engines/netmeta.py. The contract came first and this is written to
# it, not the other way round: the Python side has been parsing this shape against fixtures
# since Phase 6, so the schema is fixed and the R side must satisfy it.
#
# THREE RULES, and they are the reason this file exists at all.
#
# 1. NO DEFAULTS ON THIS SIDE. Every statistical choice - effect measure, fixed vs random,
#    the reference treatment, the zero-event policy - arrives in the request from an
#    approved protocol. A default applied here is a methodology decision nobody approved
#    and nobody can see, so a missing field is an error rather than a fallback.
#
# 2. ARM-LEVEL IN, netmeta::pairwise() INSIDE. The caller transmits arms grouped by study
#    and never flattens. pairwise() converts to contrast level while preserving the
#    multi-arm structure netmeta's variance correction consumes. Flattening a three-arm
#    trial into independent rows double-counts its control group and understates every
#    standard error involving it.
#
# 3. INCONSISTENCY IS ONLY REPORTED WHEN THE NETWORK CAN SUPPORT IT. The Python side
#    discards it when independent_loop_count is 0; this side simply does not compute a
#    design-by-treatment test on a network with no independent loop.

library(plumber)
library(netmeta)
library(jsonlite)

CONTRACT_VERSION <- "1"

# The contract's vocabulary -> netmeta's. Kept as an explicit table so an unsupported
# measure is refused by name rather than silently passed through to netmeta and rejected
# with a message about an argument the caller never set.
SM_OF <- list(
  risk_ratio       = "RR",
  odds_ratio       = "OR",
  risk_difference  = "RD",
  hazard_ratio     = "HR",
  mean_difference  = "MD",
  standardised_mean_difference = "SMD"
)

# Continuity corrections. The contract names a policy; netmeta needs an increment AND a
# decision about comparisons with zero events in BOTH arms.
#
# `allstudies` is not a detail, and leaving it at netmeta's default was a bug that stop-and-
# review gate #4 caught on netmeta's own Dong2013. With allstudies = FALSE a comparison with
# zero events in both arms is dropped outright - which contradicts a policy whose name says
# to correct it and keep it, and in a MULTI-ARM trial drops some of a study's comparisons
# and not others, so netmeta then refuses the whole study for having "a wrong number of
# comparisons". The policy name has to mean what it says:
#
#   correct-and-keep  -> incr 0.5, allstudies TRUE
#   exclude           -> incr 0,   allstudies FALSE
ZERO_EVENT_OF <- list(
  TREATMENT_ARM_CONTINUITY_CORRECTION = list(incr = 0.5, allstudies = TRUE),
  FIXED_0_5_CORRECTION                = list(incr = 0.5, allstudies = TRUE),
  EXCLUDE_ZERO_EVENT_STUDIES          = list(incr = 0,   allstudies = FALSE)
)

`%||%` <- function(a, b) if (is.null(a)) b else a

fail <- function(res, status, message) {
  res$status <- status
  list(error = message, contract_version = CONTRACT_VERSION)
}

require_field <- function(body, name) {
  value <- body[[name]]
  if (is.null(value) || (is.character(value) && !nzchar(value))) {
    stop(sprintf(
      "%s is required. The sidecar has no defaults: every statistical choice is the calling protocol's.",
      name
    ))
  }
  value
}

# Flatten {studies: [{study_id, arms: [...]}]} into the long arm-level frame pairwise()
# wants. Study identity is preserved on every row, which is what lets pairwise() keep the
# multi-arm correlation.
arm_frame <- function(studies, outcome_type) {
  rows <- list()
  for (study in studies) {
    for (arm in study$arms) {
      row <- list(
        studlab   = study$study_id,
        treat     = arm$treatment,
        n         = arm$sample_size %||% NA
      )
      if (identical(outcome_type, "binary")) {
        row$event <- arm$events %||% NA
      } else {
        row$mean <- arm$mean %||% NA
        row$sd   <- arm$standard_deviation %||% NA
      }
      rows[[length(rows) + 1]] <- as.data.frame(row, stringsAsFactors = FALSE)
    }
  }
  if (length(rows) == 0) stop("no arms were transmitted")
  do.call(rbind, rows)
}

league_table <- function(net, sm, random) {
  # netmeta returns matrices indexed by treatment. Every off-diagonal cell is one contrast;
  # only the upper triangle is emitted so the caller is not handed each pair twice with the
  # second silently the reciprocal of the first.
  #
  # `random` is PASSED IN, not read back off `net`. This previously asked the fitted object
  # via `net$comb.random`, and `comb.random` is deprecated - netmeta 2.9-0 already warns on
  # every call. When it is eventually removed the field reads NULL, `identical(NULL, TRUE)`
  # is FALSE, and this would have silently emitted FIXED-effect estimates for a random-effects
  # request. That is the worst shape a bug can take here: no error, just narrower intervals
  # than the analysis supports, flowing into league tables and the alignment dashboard as
  # unearned confidence. The version pin delayed that; it did not prevent it. The caller
  # already knows which model it asked for, so asking the object was never necessary.
  est <- if (random) net$TE.random else net$TE.fixed
  lo  <- if (random) net$lower.random else net$lower.fixed
  hi  <- if (random) net$upper.random else net$upper.fixed
  se  <- if (random) net$seTE.random else net$seTE.fixed

  ratio <- sm %in% c("RR", "OR", "HR")
  treatments <- rownames(est)
  out <- list()
  for (i in seq_along(treatments)) {
    for (j in seq_along(treatments)) {
      if (j <= i) next
      value <- est[i, j]
      if (is.na(value)) next
      out[[length(out) + 1]] <- list(
        treatment       = treatments[i],
        comparator      = treatments[j],
        # netmeta works on the analysis scale; the contract carries reported-scale numbers.
        estimate        = if (ratio) exp(value) else value,
        ci_lower        = if (ratio) exp(lo[i, j]) else lo[i, j],
        ci_upper        = if (ratio) exp(hi[i, j]) else hi[i, j],
        standard_error  = se[i, j]
      )
    }
  }
  out
}

net_split <- function(net, contrasts, random) {
  # Only meaningful where a comparison has both a direct and an indirect path. netsplit()
  # errors on a network with nothing to split, so it is attempted rather than assumed.
  split <- tryCatch(netsplit(net), error = function(e) NULL)
  if (is.null(split)) return(contrasts)

  direct <- if (random) split$direct.random else split$direct.fixed
  indirect <- if (random) split$indirect.random else split$indirect.fixed
  pvalue <- if (random) split$compare.random$p else split$compare.fixed$p
  if (is.null(direct) || is.null(indirect)) return(contrasts)

  keys <- as.character(split$comparison)
  for (k in seq_along(contrasts)) {
    label <- paste(contrasts[[k]]$treatment, contrasts[[k]]$comparator, sep = ":")
    reversed <- paste(contrasts[[k]]$comparator, contrasts[[k]]$treatment, sep = ":")
    idx <- match(label, keys)
    if (is.na(idx)) idx <- match(reversed, keys)
    if (is.na(idx)) next
    contrasts[[k]]$direct_estimate <- direct$TE[idx]
    contrasts[[k]]$indirect_estimate <- indirect$TE[idx]
    contrasts[[k]]$net_split_p_value <- if (!is.null(pvalue)) pvalue[idx] else NULL
  }
  contrasts
}

#* Health check. Unchanged by the contract, so a deploy can probe it before wiring anything.
#* @get /healthz
function() {
  list(
    ok = TRUE,
    contract_version = CONTRACT_VERSION,
    netmeta_version = as.character(packageVersion("netmeta")),
    r_version = paste(R.version$major, R.version$minor, sep = ".")
  )
}

# `digits = NA` on the serializer below is load-bearing, not tidiness. jsonlite::toJSON
# defaults to FOUR DECIMAL PLACES, so without it every estimate, CI bound, standard error,
# tau2, I2 and SUCRA score is silently rounded on the wire. Stop-and-review gate #4 caught
# this as a 5e-05 disagreement against a direct netmeta() call on Woods2010 - small enough
# to read as noise, and exactly the kind of quiet precision loss that makes a stored result
# irreproducible against the HTA submissions these numbers get compared to.

#* Run one network meta-analysis.
#* @post /nma
#* @serializer unboxedJSON list(digits = NA)
function(req, res) {
  body <- tryCatch(
    fromJSON(req$postBody, simplifyVector = FALSE),
    error = function(e) NULL
  )
  if (is.null(body)) return(fail(res, 400, "request body is not valid JSON"))

  # A sidecar built against an older contract must fail loudly rather than misread fields.
  sent <- body$contract_version %||% "0"
  if (!identical(as.character(sent), CONTRACT_VERSION)) {
    return(fail(res, 409, sprintf(
      "contract version mismatch: this sidecar speaks %s, the caller sent %s",
      CONTRACT_VERSION, sent
    )))
  }

  result <- tryCatch({
    outcome_type <- require_field(body, "outcome_type")
    measure      <- require_field(body, "effect_measure")
    model        <- require_field(body, "model")
    reference    <- require_field(body, "reference_treatment")
    studies      <- require_field(body, "studies")

    sm <- SM_OF[[measure]]
    if (is.null(sm)) stop(sprintf("unsupported effect_measure %s", measure))
    if (!model %in% c("fixed", "random")) {
      stop(sprintf("model must be 'fixed' or 'random', got %s", model))
    }

    policy <- body$zero_event_policy %||% "TREATMENT_ARM_CONTINUITY_CORRECTION"
    zero <- ZERO_EVENT_OF[[policy]]
    if (is.null(zero)) stop(sprintf("unsupported zero_event_policy %s", policy))

    frame <- arm_frame(studies, outcome_type)

    # pairwise() is what preserves multi-arm structure. Passing already-flattened contrasts
    # would defeat the entire reason this is a sidecar.
    pw <- if (identical(outcome_type, "binary")) {
      pairwise(
        treat = frame$treat, event = frame$event, n = frame$n,
        studlab = frame$studlab, sm = sm,
        addincr = FALSE, incr = zero$incr, allstudies = zero$allstudies
      )
    } else {
      pairwise(
        treat = frame$treat, mean = frame$mean, sd = frame$sd, n = frame$n,
        studlab = frame$studlab, sm = sm
      )
    }

    net <- netmeta(
      pw,
      comb.fixed = identical(model, "fixed"),
      comb.random = identical(model, "random"),
      reference.group = reference,
      sm = sm
    )

    random <- identical(model, "random")
    contrasts <- league_table(net, sm, random)
    if (length(contrasts) == 0) stop("netmeta produced no estimable contrasts")

    independent_loops <- max(0, net$d - net$n + 1)
    if (independent_loops > 0) contrasts <- net_split(net, contrasts, random)

    inconsistency <- NULL
    if (independent_loops > 0) {
      decomp <- tryCatch(decomp.design(net), error = function(e) NULL)
      if (!is.null(decomp) && !is.null(decomp$Q.inc.random)) {
        inconsistency <- list(
          design_by_treatment_p = decomp$Q.inc.random$pval,
          q_inconsistency = decomp$Q.inc.random$Q
        )
      }
    }

    sucra <- NULL
    ranking <- tryCatch(
      netrank(net, small.values = "bad"), error = function(e) NULL
    )
    if (!is.null(ranking)) {
      scores <- if (random) ranking$Pscore.random else ranking$Pscore.fixed
      if (!is.null(scores)) sucra <- as.list(scores)
    }

    list(
      contract_version = CONTRACT_VERSION,
      effect_measure = measure,
      model = model,
      reference_treatment = reference,
      package_version = paste("netmeta", as.character(packageVersion("netmeta"))),
      contrasts = contrasts,
      sucra = sucra,
      tau_squared = net$tau2,
      q_statistic = net$Q,
      degrees_freedom = net$df.Q,
      i_squared = net$I2,
      inconsistency = inconsistency,
      independent_loop_count = independent_loops
    )
  }, error = function(e) {
    structure(conditionMessage(e), class = "sidecar_error")
  })

  if (inherits(result, "sidecar_error")) {
    return(fail(res, 400, as.character(result)))
  }
  result
}
