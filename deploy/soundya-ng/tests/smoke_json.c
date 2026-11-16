/* Локальная проверка тестов json без взятия зависимостей: только stdio. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "../src/json.h"

static int failures;
#define CHECK(cond, msg) do { if (!(cond)) { printf("FAIL %s\n", msg); failures++; } } while (0)

int main(void) {
    JDoc doc;
    char buf[256];
    const char *ok = "{\"name\":\"панель\",\"n\":7,\"ok\":true,\"arr\":[1,2,3],\"nested\":{\"x\":\"y\"}}";
    CHECK(j_parse(&doc, ok) == 0, "parse ok");
    int root = 0;
    int name = j_find(&doc, root, "name");
    CHECK(name >= 0, "find name");
    CHECK(j_string(&doc, name, buf, sizeof buf) == 12, "name len");
    CHECK(strcmp(buf, "панель") == 0, "name value");
    int n = j_find(&doc, root, "n");
    CHECK(j_int(&doc, n, -1) == 7, "int");
    CHECK(j_bool(&doc, j_find(&doc, root, "ok"), 0) == 1, "bool");
    int arr = j_find(&doc, root, "arr");
    CHECK(j_count(&doc, arr) == 3, "count");
    CHECK(j_int(&doc, j_at(&doc, arr, 2), -1) == 3, "at 2");
    CHECK(j_find(&doc, j_find(&doc, root, "nested"), "x") >= 0, "nested");
    const char *bad[] = {"{", "{\"a\":1,}", "{\"a\":}{", "[1,2", "\"\\ud800\"", "{\"a\":1}x", "01", "{\"a\":\"\\q\"}", ""};
    for (size_t i = 0; i < sizeof bad / sizeof bad[0]; i++) {
        JDoc d;
        CHECK(j_parse(&d, bad[i]) == -1, "reject");
        CHECK(d.count == 0, "count zero");
    }
    char quoted[64];
    CHECK(j_quote("панель «наша»", quoted, sizeof quoted) > 0, "quote");
    CHECK(strstr(quoted, "панель") != NULL, "quoted value");
    printf(failures ? "FAILURES: %d\n" : "OK\n", failures);
    return failures ? 1 : 0;
}
