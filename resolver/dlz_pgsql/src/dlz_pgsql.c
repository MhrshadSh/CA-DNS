/*
 * dlz_pgsql: BIND 9 DLZ dlopen module that answers from PostgreSQL (CA-DNS).
 *
 * Design (docs/architecture.md ADR-1, ADR-8):
 *   - Only the client's exact query name is served. BIND probes findzonedb
 *     longest name first, synchronously on one thread, so a probe that is a
 *     parent of the previous probe on this thread is answered NOTFOUND without
 *     touching the database.
 *   - findzonedb does the single database round trip per query
 *     (cadns.dlz_lookup(name, '@')) and keeps the rows in thread-local storage
 *     for the dlz_lookup callbacks that follow.
 *   - Database errors return ISC_R_FAILURE from findzonedb, which makes BIND
 *     answer from its cache or by recursion (fail-open).
 *
 * Connection settings come from libpq's PG* environment variables plus any
 * key=value arguments in named.conf (see util.h).
 */
#include <errno.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <time.h>

#include <libpq-fe.h>

#include "dlz_minimal.h"
#include "util.h"

#define LOOKUP_STMT "cadns_dlz_lookup"
#define LOOKUP_SQL "SELECT ttl, type, data FROM cadns.dlz_lookup($1, '@')"

#define NAME_MAX_LEN 1025 /* presentation format, including escapes */
#define MAX_ROWS 64
#define ROW_BUF_LEN 8192
#define CACHE_MAX_AGE_NS (2 * 1000000000LL)
#define ERROR_LOG_NS (10 * 1000000000LL)

enum db_state { DB_UNKNOWN, DB_UP, DB_DOWN };

struct conn {
	PGconn *pg;
	int64_t retry_at_ns;
};

struct instance {
	log_t *log;
	dns_sdlz_putrr_t *putrr;
	struct cadns_options opts;

	pthread_mutex_t mu;
	pthread_cond_t cv;
	struct conn *conns;
	unsigned *free_idx;
	unsigned nfree;

	atomic_int state;
	atomic_int_least64_t last_error_log_ns;
};

struct row {
	dns_ttl_t ttl;
	const char *type;
	const char *data;
};

/* Per-thread state; see the design notes above. */
struct thread_state {
	char last_probe[NAME_MAX_LEN];

	const struct instance *owner;
	char zone[NAME_MAX_LEN];
	int64_t filled_ns;
	unsigned nrows;
	struct row rows[MAX_ROWS];
	char buf[ROW_BUF_LEN];
};

static _Thread_local struct thread_state tls;

static int64_t
now_ns(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

/* libpq error messages span lines; flatten them into one log line. */
static void
one_line(char *dst, size_t len, const char *src)
{
	size_t n = 0;
	bool space = false;

	for (const char *p = src != NULL ? src : ""; *p != '\0' && n + 1 < len; p++) {
		if (*p == '\n' || *p == '\r' || *p == '\t' || *p == ' ') {
			space = n > 0;
			continue;
		}
		if (space && n + 2 < len) {
			dst[n++] = ' ';
		}
		space = false;
		dst[n++] = *p;
	}
	dst[n] = '\0';
}

static void
set_up(struct instance *inst)
{
	if (atomic_exchange(&inst->state, DB_UP) != DB_UP) {
		inst->log(ISC_LOG_INFO, "dlz_pgsql: database available");
	}
}

/*
 * Log a database problem: always on the transition to DOWN, then at most once
 * every ERROR_LOG_NS so an outage does not flood the log.
 */
static void
set_down(struct instance *inst, const char *what, const char *msg)
{
	char text[512];
	int64_t now = now_ns();
	int64_t last = atomic_load(&inst->last_error_log_ns);
	bool transition = atomic_exchange(&inst->state, DB_DOWN) != DB_DOWN;

	if (!transition && now - last < ERROR_LOG_NS) {
		return;
	}
	atomic_store(&inst->last_error_log_ns, now);
	one_line(text, sizeof(text), msg);
	inst->log(ISC_LOG_ERROR, "dlz_pgsql: %s failed: %s%s", what, text,
	          transition ? " (answering by recursion until the database is back)" : "");
}

static struct conn *
acquire(struct instance *inst)
{
	struct timespec deadline;
	unsigned idx;

	clock_gettime(CLOCK_MONOTONIC, &deadline);
	deadline.tv_nsec += (long)inst->opts.pool_wait_ms * 1000000L;
	deadline.tv_sec += deadline.tv_nsec / 1000000000L;
	deadline.tv_nsec %= 1000000000L;

	pthread_mutex_lock(&inst->mu);
	while (inst->nfree == 0) {
		if (pthread_cond_timedwait(&inst->cv, &inst->mu, &deadline) == ETIMEDOUT &&
		    inst->nfree == 0) {
			pthread_mutex_unlock(&inst->mu);
			return NULL;
		}
	}
	idx = inst->free_idx[--inst->nfree];
	pthread_mutex_unlock(&inst->mu);
	return &inst->conns[idx];
}

static void
release(struct instance *inst, struct conn *c)
{
	pthread_mutex_lock(&inst->mu);
	inst->free_idx[inst->nfree++] = (unsigned)(c - inst->conns);
	pthread_cond_signal(&inst->cv);
	pthread_mutex_unlock(&inst->mu);
}

static void
disconnect(struct conn *c)
{
	if (c->pg != NULL) {
		PQfinish(c->pg);
		c->pg = NULL;
	}
}

/* Connect (with backoff) and prepare the lookup statement. */
static bool
ensure_connected(struct instance *inst, struct conn *c)
{
	PGresult *res;
	int64_t now;

	if (c->pg != NULL && PQstatus(c->pg) == CONNECTION_OK) {
		return true;
	}
	now = now_ns();
	if (now < c->retry_at_ns) {
		return false;
	}
	disconnect(c);

	c->pg = PQconnectdbParams((const char *const *)inst->opts.keywords,
	                          (const char *const *)inst->opts.values, 0);
	if (c->pg == NULL || PQstatus(c->pg) != CONNECTION_OK) {
		set_down(inst, "connect", c->pg != NULL ? PQerrorMessage(c->pg) : "out of memory");
		goto backoff;
	}

	res = PQprepare(c->pg, LOOKUP_STMT, LOOKUP_SQL, 1, NULL);
	if (PQresultStatus(res) != PGRES_COMMAND_OK) {
		set_down(inst, "prepare", PQresultErrorMessage(res));
		PQclear(res);
		goto backoff;
	}
	PQclear(res);
	return true;

backoff:
	disconnect(c);
	c->retry_at_ns = now + (int64_t)inst->opts.reconnect_ms * 1000000LL;
	return false;
}

/* Copy a result into the thread-local row cache. */
static isc_result_t
store_rows(struct instance *inst, const char *name, PGresult *res)
{
	int n = PQntuples(res);
	size_t off = 0;

	tls.owner = NULL;
	tls.nrows = 0;
	if (n == 0) {
		return ISC_R_NOTFOUND;
	}
	if (n > MAX_ROWS || PQnfields(res) != 3) {
		set_down(inst, "lookup", "unexpected result shape");
		return ISC_R_FAILURE;
	}

	for (int i = 0; i < n; i++) {
		const char *ttl = PQgetvalue(res, i, 0);
		const char *type = PQgetvalue(res, i, 1);
		const char *data = PQgetvalue(res, i, 2);
		size_t tlen = strlen(type) + 1;
		size_t dlen = strlen(data) + 1;

		if (PQgetisnull(res, i, 0) || off + tlen + dlen > sizeof(tls.buf)) {
			set_down(inst, "lookup", "result row is NULL or too large");
			return ISC_R_FAILURE;
		}
		tls.rows[i].ttl = (dns_ttl_t)strtoul(ttl, NULL, 10);
		tls.rows[i].type = memcpy(tls.buf + off, type, tlen);
		off += tlen;
		tls.rows[i].data = memcpy(tls.buf + off, data, dlen);
		off += dlen;
	}

	snprintf(tls.zone, sizeof(tls.zone), "%s", name);
	tls.nrows = (unsigned)n;
	tls.filled_ns = now_ns();
	tls.owner = inst;
	return ISC_R_SUCCESS;
}

/*
 * Run cadns.dlz_lookup(name, '@'): ISC_R_SUCCESS with rows cached,
 * ISC_R_NOTFOUND if the name is not served, ISC_R_FAILURE on errors.
 */
static isc_result_t
fetch(struct instance *inst, const char *name)
{
	const char *params[1] = {name};
	isc_result_t result = ISC_R_FAILURE;
	struct conn *c = acquire(inst);

	if (c == NULL) {
		set_down(inst, "pool", "no free connection");
		return ISC_R_FAILURE;
	}

	/* One retry: a connection dropped by a server restart reconnects at once. */
	for (int attempt = 0; attempt < 2; attempt++) {
		PGresult *res;

		if (!ensure_connected(inst, c)) {
			break;
		}
		res = PQexecPrepared(c->pg, LOOKUP_STMT, 1, params, NULL, NULL, 0);
		if (PQresultStatus(res) == PGRES_TUPLES_OK) {
			result = store_rows(inst, name, res);
			PQclear(res);
			if (result != ISC_R_FAILURE) {
				set_up(inst);
			}
			break;
		}
		set_down(inst, "lookup", PQresultErrorMessage(res));
		PQclear(res);
		if (PQstatus(c->pg) == CONNECTION_OK) {
			break; /* query error (e.g. timeout): don't retry */
		}
		disconnect(c);
		c->retry_at_ns = 0;
	}

	release(inst, c);
	return result;
}

int
dlz_version(unsigned int *flags)
{
	*flags |= DNS_SDLZFLAG_THREADSAFE;
	return DLZ_DLOPEN_VERSION;
}

isc_result_t
dlz_create(const char *dlzname, unsigned int argc, char *argv[], void **dbdata, ...)
{
	struct instance *inst;
	PQconninfoOption *defaults;
	const char *helper;
	char err[256];
	va_list ap;

	inst = calloc(1, sizeof(*inst));
	if (inst == NULL) {
		return ISC_R_NOMEMORY;
	}

	va_start(ap, dbdata);
	while ((helper = va_arg(ap, const char *)) != NULL) {
		void *ptr = va_arg(ap, void *);
		if (strcmp(helper, "log") == 0) {
			inst->log = ptr;
		} else if (strcmp(helper, "putrr") == 0) {
			inst->putrr = ptr;
		}
	}
	va_end(ap);
	if (inst->log == NULL || inst->putrr == NULL) {
		free(inst);
		return ISC_R_FAILURE;
	}

	/* argv[0] is the module path. */
	if (cadns_parse_options(argc > 0 ? argc - 1 : 0, argc > 0 ? argv + 1 : argv, &inst->opts,
	                        err, sizeof(err)) != 0) {
		inst->log(ISC_LOG_ERROR, "dlz_pgsql: %s: %s", dlzname, err);
		free(inst);
		return ISC_R_FAILURE;
	}

	/* Reject unknown libpq keywords now instead of on every connect. */
	defaults = PQconndefaults();
	for (unsigned i = 0; defaults != NULL && i < inst->opts.nparams; i++) {
		bool known = false;
		for (PQconninfoOption *o = defaults; o->keyword != NULL; o++) {
			if (strcmp(o->keyword, inst->opts.keywords[i]) == 0) {
				known = true;
				break;
			}
		}
		if (!known) {
			inst->log(ISC_LOG_ERROR, "dlz_pgsql: %s: unknown parameter '%s'", dlzname,
			          inst->opts.keywords[i]);
			PQconninfoFree(defaults);
			cadns_options_free(&inst->opts);
			free(inst);
			return ISC_R_FAILURE;
		}
	}
	PQconninfoFree(defaults);

	inst->conns = calloc(inst->opts.pool, sizeof(*inst->conns));
	inst->free_idx = calloc(inst->opts.pool, sizeof(*inst->free_idx));
	if (inst->conns == NULL || inst->free_idx == NULL) {
		free(inst->conns);
		free(inst->free_idx);
		cadns_options_free(&inst->opts);
		free(inst);
		return ISC_R_NOMEMORY;
	}
	for (unsigned i = 0; i < inst->opts.pool; i++) {
		inst->free_idx[i] = i;
	}
	inst->nfree = inst->opts.pool;

	{
		pthread_condattr_t attr;

		pthread_condattr_init(&attr);
		pthread_condattr_setclock(&attr, CLOCK_MONOTONIC);
		pthread_cond_init(&inst->cv, &attr);
		pthread_condattr_destroy(&attr);
	}
	pthread_mutex_init(&inst->mu, NULL);
	atomic_init(&inst->state, DB_UNKNOWN);
	atomic_init(&inst->last_error_log_ns, 0);

	inst->log(ISC_LOG_INFO,
	          "dlz_pgsql: %s: loaded (pool=%u, statement_timeout_ms=%u, "
	          "reconnect_ms=%u); connections open on first query",
	          dlzname, inst->opts.pool, inst->opts.statement_timeout_ms,
	          inst->opts.reconnect_ms);
	*dbdata = inst;
	return ISC_R_SUCCESS;
}

void
dlz_destroy(void *dbdata)
{
	struct instance *inst = dbdata;

	for (unsigned i = 0; i < inst->opts.pool; i++) {
		disconnect(&inst->conns[i]);
	}
	pthread_cond_destroy(&inst->cv);
	pthread_mutex_destroy(&inst->mu);
	free(inst->conns);
	free(inst->free_idx);
	cadns_options_free(&inst->opts);
	inst->log(ISC_LOG_INFO, "dlz_pgsql: unloaded");
	free(inst);
}

isc_result_t
dlz_findzonedb(void *dbdata, const char *name, dns_clientinfomethods_t *methods,
               dns_clientinfo_t *clientinfo)
{
	struct instance *inst = dbdata;
	bool parent_probe;
	isc_result_t result;

	UNUSED(methods);
	UNUSED(clientinfo);

	if (strlen(name) >= sizeof(tls.last_probe)) {
		tls.last_probe[0] = '\0';
		return ISC_R_NOTFOUND;
	}
	parent_probe = cadns_is_parent_name(name, tls.last_probe);
	memcpy(tls.last_probe, name, strlen(name) + 1);
	if (parent_probe) {
		return ISC_R_NOTFOUND;
	}

	result = fetch(inst, name);
	if (result != ISC_R_NOTFOUND) {
		tls.last_probe[0] = '\0'; /* BIND's search loop ends here */
	}
	return result;
}

isc_result_t
dlz_lookup(const char *zone, const char *name, void *dbdata, dns_sdlzlookup_t *lookup,
           dns_clientinfomethods_t *methods, dns_clientinfo_t *clientinfo)
{
	struct instance *inst = dbdata;

	UNUSED(methods);
	UNUSED(clientinfo);

	/* Exact names only (ADR-8): nothing below the apex, no wildcards. */
	if (strcmp(name, "@") != 0) {
		return ISC_R_NOTFOUND;
	}

	if (tls.owner != inst || tls.nrows == 0 || strcasecmp(tls.zone, zone) != 0 ||
	    now_ns() - tls.filled_ns > CACHE_MAX_AGE_NS) {
		/* Not directly preceded by findzonedb on this thread. A zone that
		 * vanished in between is a failure (SERVFAIL), not NXDOMAIN. */
		if (fetch(inst, zone) != ISC_R_SUCCESS) {
			return ISC_R_FAILURE;
		}
	}

	for (unsigned i = 0; i < tls.nrows; i++) {
		isc_result_t result =
		    inst->putrr(lookup, tls.rows[i].type, tls.rows[i].ttl, tls.rows[i].data);
		if (result != ISC_R_SUCCESS) {
			inst->log(ISC_LOG_ERROR, "dlz_pgsql: putrr(%s %s %s) failed: %u", zone,
			          tls.rows[i].type, tls.rows[i].data, result);
			return result;
		}
	}
	return ISC_R_SUCCESS;
}
