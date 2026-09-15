# dlz_pgsql: BIND DLZ dlopen module (built inside resolver/Dockerfile).
#   make          build dlz_pgsql.so
#   make test     build and run unit tests

CC       ?= cc
PKG_CONFIG ?= pkg-config
CFLAGS   ?= -O2 -g
CFLAGS   += -std=gnu17 -fPIC -pthread -Wall -Wextra -Werror -Wshadow -Wformat=2 \
            -D_FORTIFY_SOURCE=3 -fstack-protector-strong -Iinclude -Isrc
PQ_CFLAGS := $(shell $(PKG_CONFIG) --cflags libpq)
PQ_LIBS   := $(shell $(PKG_CONFIG) --libs libpq)
LDFLAGS  += -Wl,-z,relro,-z,now -Wl,--no-undefined

BUILD := build

.PHONY: all test clean
all: $(BUILD)/dlz_pgsql.so

$(BUILD):
	mkdir -p $@

$(BUILD)/dlz_pgsql.so: src/dlz_pgsql.c src/util.c src/util.h include/dlz_minimal.h | $(BUILD)
	$(CC) $(CFLAGS) $(PQ_CFLAGS) -shared -o $@ src/dlz_pgsql.c src/util.c $(LDFLAGS) $(PQ_LIBS)

$(BUILD)/test_util: tests/test_util.c src/util.c src/util.h | $(BUILD)
	$(CC) $(CFLAGS) -fsanitize=address,undefined -o $@ tests/test_util.c src/util.c

test: $(BUILD)/test_util
	$(BUILD)/test_util

clean:
	rm -rf $(BUILD)
