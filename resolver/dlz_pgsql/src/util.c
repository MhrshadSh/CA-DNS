/*
 * Pure helpers for dlz_pgsql. See util.h.
 */
#include "util.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

bool
cadns_is_parent_name(const char *name, const char *child)
{
	size_t n = strlen(name);
	size_t c = strlen(child);

	if (n == 0 || c <= n + 1) {
		return false;
	}
	return child[c - n - 1] == '.' && strcasecmp(child + c - n, name) == 0;
}

const char *
cadns_option_value(const struct cadns_options *opts, const char *keyword)
{
	for (unsigned i = 0; i < opts->nparams; i++) {
		if (strcmp(opts->keywords[i], keyword) == 0) {
			return opts->values[i];
		}
	}
	return NULL;
}

void
cadns_options_free(struct cadns_options *opts)
{
	for (unsigned i = 0; i < opts->nparams; i++) {
		free(opts->keywords[i]);
		free(opts->values[i]);
		opts->keywords[i] = NULL;
		opts->values[i] = NULL;
	}
	opts->nparams = 0;
}

static int
add_param(struct cadns_options *opts, const char *key, size_t keylen, const char *value, char *err,
          size_t errlen)
{
	if (opts->nparams >= CADNS_MAX_PARAMS) {
		snprintf(err, errlen, "too many connection parameters (max %d)", CADNS_MAX_PARAMS);
		return -1;
	}
	opts->keywords[opts->nparams] = strndup(key, keylen);
	opts->values[opts->nparams] = strdup(value);
	if (opts->keywords[opts->nparams] == NULL || opts->values[opts->nparams] == NULL) {
		free(opts->keywords[opts->nparams]);
		free(opts->values[opts->nparams]);
		snprintf(err, errlen, "out of memory");
		return -1;
	}
	opts->nparams++;
	return 0;
}

static int
parse_uint(const char *key, const char *value, unsigned min, unsigned max, unsigned *out, char *err,
           size_t errlen)
{
	char *end = NULL;
	unsigned long v;

	errno = 0;
	v = strtoul(value, &end, 10);
	if (errno != 0 || end == value || *end != '\0' || value[0] == '-' || v < min || v > max) {
		snprintf(err, errlen, "%s must be an integer in %u..%u, got '%s'", key, min, max,
		         value);
		return -1;
	}
	*out = (unsigned)v;
	return 0;
}

struct uint_option {
	const char *key;
	unsigned min;
	unsigned max;
	size_t offset;
};

static const struct uint_option uint_options[] = {
    {"pool", 1, 64, offsetof(struct cadns_options, pool)},
    {"pool_wait_ms", 0, 10000, offsetof(struct cadns_options, pool_wait_ms)},
    {"reconnect_ms", 0, 600000, offsetof(struct cadns_options, reconnect_ms)},
    {"statement_timeout_ms", 0, 60000, offsetof(struct cadns_options, statement_timeout_ms)},
};

int
cadns_parse_options(unsigned argc, char *argv[], struct cadns_options *opts, char *err,
                    size_t errlen)
{
	memset(opts, 0, sizeof(*opts));
	opts->pool = 4;
	opts->pool_wait_ms = 100;
	opts->reconnect_ms = 2000;
	opts->statement_timeout_ms = 250;

	for (unsigned i = 0; i < argc; i++) {
		const char *arg = argv[i];
		const char *eq = strchr(arg, '=');
		size_t keylen;
		bool handled = false;

		if (eq == NULL || eq == arg) {
			snprintf(err, errlen, "expected key=value, got '%s'", arg);
			goto fail;
		}
		keylen = (size_t)(eq - arg);

		for (size_t j = 0; j < sizeof(uint_options) / sizeof(uint_options[0]); j++) {
			const struct uint_option *o = &uint_options[j];
			if (strlen(o->key) == keylen && strncmp(arg, o->key, keylen) == 0) {
				if (parse_uint(o->key, eq + 1, o->min, o->max,
				               (unsigned *)((char *)opts + o->offset), err,
				               errlen) != 0) {
					goto fail;
				}
				handled = true;
				break;
			}
		}
		if (handled) {
			continue;
		}

		if (keylen == strlen("password") && strncmp(arg, "password", keylen) == 0) {
			snprintf(
			    err, errlen,
			    "do not put the password in named.conf; use PGPASSWORD or PGPASSFILE");
			goto fail;
		}
		for (unsigned j = 0; j < opts->nparams; j++) {
			if (strlen(opts->keywords[j]) == keylen &&
			    strncmp(opts->keywords[j], arg, keylen) == 0) {
				snprintf(err, errlen, "duplicate parameter '%.*s'", (int)keylen,
				         arg);
				goto fail;
			}
		}
		if (add_param(opts, arg, keylen, eq + 1, err, errlen) != 0) {
			goto fail;
		}
	}

	if (cadns_option_value(opts, "application_name") == NULL &&
	    add_param(opts, "application_name", strlen("application_name"), "cadns-dlz", err,
	              errlen) != 0) {
		goto fail;
	}
	if (cadns_option_value(opts, "connect_timeout") == NULL &&
	    add_param(opts, "connect_timeout", strlen("connect_timeout"), "2", err, errlen) != 0) {
		goto fail;
	}
	if (cadns_option_value(opts, "tcp_user_timeout") == NULL &&
	    add_param(opts, "tcp_user_timeout", strlen("tcp_user_timeout"), "2000", err, errlen) !=
	        0) {
		goto fail;
	}
	if (opts->statement_timeout_ms > 0) {
		char buf[64];

		if (cadns_option_value(opts, "options") != NULL) {
			snprintf(err, errlen,
			         "use either statement_timeout_ms or options, not both "
			         "(statement_timeout_ms=0 disables it)");
			goto fail;
		}
		snprintf(buf, sizeof(buf), "-c statement_timeout=%u", opts->statement_timeout_ms);
		if (add_param(opts, "options", strlen("options"), buf, err, errlen) != 0) {
			goto fail;
		}
	}
	return 0;

fail:
	cadns_options_free(opts);
	return -1;
}
