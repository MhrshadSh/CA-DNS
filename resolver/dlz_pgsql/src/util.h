/*
 * Pure helpers for dlz_pgsql (no BIND or libpq dependencies, unit-tested).
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>

/* Upper bound on libpq connection parameters passed from named.conf. */
#define CADNS_MAX_PARAMS 32

struct cadns_options {
	unsigned pool;         /* pool=N: connections, 1..64 (default 4) */
	unsigned pool_wait_ms; /* pool_wait_ms=N: wait for a free connection (default 100) */
	unsigned reconnect_ms; /* reconnect_ms=N: backoff after a failed connect (default 2000) */
	unsigned statement_timeout_ms; /* statement_timeout_ms=N: 0 disables (default 250) */

	/* libpq keywords/values for PQconnectdbParams, NULL-terminated, owned. */
	char *keywords[CADNS_MAX_PARAMS + 1];
	char *values[CADNS_MAX_PARAMS + 1];
	unsigned nparams;
};

/*
 * True if `name` is a proper parent domain of `child` (label boundary,
 * case-insensitive): "example.com" is a parent of "www.example.com".
 */
bool cadns_is_parent_name(const char *name, const char *child);

/*
 * Parse module arguments (argv excludes the module path). Module options are
 * listed in struct cadns_options; any other key=value token is a libpq
 * connection parameter. `password` is rejected: use PGPASSWORD or PGPASSFILE.
 * Defaults are added for application_name, connect_timeout, tcp_user_timeout
 * and (via `options`) statement_timeout.
 *
 * Returns 0 on success; otherwise -1 with a message in `err`. On failure the
 * options are freed.
 */
int cadns_parse_options(unsigned argc, char *argv[], struct cadns_options *opts, char *err,
                        size_t errlen);

/* Find a libpq parameter value by keyword, or NULL. */
const char *cadns_option_value(const struct cadns_options *opts, const char *keyword);

void cadns_options_free(struct cadns_options *opts);
