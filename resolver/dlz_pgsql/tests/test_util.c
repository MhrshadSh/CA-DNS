/*
 * Unit tests for util.c (run at image build time: make test).
 */
#include <stdio.h>
#include <string.h>

#include "util.h"

static int failures;

#define CHECK(cond)                                                                                \
	do {                                                                                       \
		if (!(cond)) {                                                                     \
			fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #cond);   \
			failures++;                                                                \
		}                                                                                  \
	} while (0)

static void
test_parent_names(void)
{
	CHECK(cadns_is_parent_name("example.com", "www.example.com"));
	CHECK(cadns_is_parent_name("com", "www.example.com"));
	CHECK(cadns_is_parent_name("Example.COM", "a.b.example.com"));
	CHECK(!cadns_is_parent_name("example.com", "example.com"));   /* same name */
	CHECK(!cadns_is_parent_name("ample.com", "www.example.com")); /* not a label boundary */
	CHECK(!cadns_is_parent_name("www.example.com", "example.com"));
	CHECK(!cadns_is_parent_name("example.com", ""));
	CHECK(!cadns_is_parent_name("", "example.com"));
	CHECK(!cadns_is_parent_name("example.com", ".example.com")); /* empty label */
}

static int
parse(const char *args[], unsigned n, struct cadns_options *o, char *err)
{
	return cadns_parse_options(n, (char **)args, o, err, 256);
}

static void
test_defaults(void)
{
	struct cadns_options o;
	char err[256];

	CHECK(parse(NULL, 0, &o, err) == 0);
	CHECK(o.pool == 4);
	CHECK(o.pool_wait_ms == 100);
	CHECK(o.reconnect_ms == 2000);
	CHECK(o.statement_timeout_ms == 250);
	CHECK(strcmp(cadns_option_value(&o, "application_name"), "cadns-dlz") == 0);
	CHECK(strcmp(cadns_option_value(&o, "connect_timeout"), "2") == 0);
	CHECK(strcmp(cadns_option_value(&o, "options"), "-c statement_timeout=250") == 0);
	CHECK(o.keywords[o.nparams] == NULL && o.values[o.nparams] == NULL);
	cadns_options_free(&o);
}

static void
test_module_and_libpq_options(void)
{
	const char *args[] = {"pool=8", "statement_timeout_ms=0", "dbname=cadns", "host=postgres",
	                      "connect_timeout=5"};
	struct cadns_options o;
	char err[256];

	CHECK(parse(args, 5, &o, err) == 0);
	CHECK(o.pool == 8);
	CHECK(o.statement_timeout_ms == 0);
	CHECK(cadns_option_value(&o, "options") == NULL);
	CHECK(strcmp(cadns_option_value(&o, "dbname"), "cadns") == 0);
	CHECK(strcmp(cadns_option_value(&o, "host"), "postgres") == 0);
	CHECK(strcmp(cadns_option_value(&o, "connect_timeout"), "5") == 0);
	cadns_options_free(&o);
}

static void
test_rejections(void)
{
	const char *cases[][1] = {
	    {"pool=0"},   {"pool=65"},
	    {"pool=abc"}, {"pool=-1"},
	    {"pool="},    {"novalue"},
	    {"=value"},   {"password=secret"},
	    {"pool=4x"},  {"statement_timeout_ms=99999999"},
	};
	struct cadns_options o;
	char err[256];

	for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
		err[0] = '\0';
		CHECK(parse(cases[i], 1, &o, err) == -1);
		CHECK(err[0] != '\0');
		CHECK(o.nparams == 0);
	}

	const char *dup[] = {"host=a", "host=b"};
	CHECK(parse(dup, 2, &o, err) == -1);

	const char *both[] = {"options=-c work_mem=1MB"};
	CHECK(parse(both, 1, &o, err) == -1);

	const char *both_ok[] = {"statement_timeout_ms=0", "options=-c work_mem=1MB"};
	CHECK(parse(both_ok, 2, &o, err) == 0);
	cadns_options_free(&o);
}

int
main(void)
{
	test_parent_names();
	test_defaults();
	test_module_and_libpq_options();
	test_rejections();
	if (failures > 0) {
		fprintf(stderr, "%d check(s) failed\n", failures);
		return 1;
	}
	printf("test_util: all checks passed\n");
	return 0;
}
