#include "../src/json.h"

#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static JDoc doc;
static unsigned long checks;
static int failures;

#define CHECK(expr) do { \
    ++checks; \
    if (!(expr)) { \
        fprintf(stderr, "Ошибка: строка %d, %s\n", __LINE__, #expr); \
        ++failures; \
    } \
} while (0)

static void valid(const char *json)
{
    int i;
    size_t length = strlen(json);
    CHECK(j_parse(&doc, json) == 0);
    CHECK(doc.count >= 1 && doc.count <= J_MAX_TOKENS);
    CHECK(doc.source == json);
    CHECK(doc.tokens[0].parent == -1);
    for (i = 0; i < doc.count; ++i) {
        const JToken *t = &doc.tokens[i];
        CHECK(t->source == json);
        CHECK(t->start <= t->end && t->end <= length);
        CHECK(t->kind >= J_OBJECT && t->kind <= J_NULL);
        if (i) CHECK(t->parent >= 0 && t->parent < i);
    }
}

static void invalid(const char *json)
{
    CHECK(j_parse(&doc, json) == -1);
    CHECK(doc.count == 0);
}

static void test_structure(void)
{
    const char *json = " \n{\"a\":[1,{\"x\":[false,null]},[2,3]],\"b\":true,\"empty\":[]}\t";
    int a, nested, x;
    char out[32];
    valid(json);
    CHECK(doc.count == 16);
    CHECK(doc.tokens[0].kind == J_OBJECT);
    CHECK(doc.tokens[0].start == 2);
    CHECK(doc.tokens[0].end == strlen(json) - 1);
    CHECK(j_string(&doc, 1, out, sizeof out) == 1 && strcmp(out, "a") == 0);
    a = j_find(&doc, 0, "a");
    CHECK(a == 2);
    CHECK(j_count(&doc, a) == 3);
    CHECK(j_int(&doc, j_at(&doc, a, 0), -1) == 1);
    nested = j_at(&doc, a, 1);
    x = j_find(&doc, nested, "x");
    CHECK(j_count(&doc, x) == 2);
    CHECK(j_bool(&doc, j_at(&doc, x, 0), 9) == 0);
    CHECK(doc.tokens[j_at(&doc, x, 1)].kind == J_NULL);
    CHECK(j_bool(&doc, j_find(&doc, 0, "b"), 9) == 1);
    CHECK(j_count(&doc, j_find(&doc, 0, "empty")) == 0);
    CHECK(j_at(&doc, a, 3) == -1);
    CHECK(j_at(&doc, a, -1) == -1);
    CHECK(j_at(&doc, 0, 0) == -1);
    CHECK(j_count(&doc, 0) == -1);
    CHECK(j_find(&doc, a, "x") == -1);
    CHECK(j_find(&doc, 0, "x") == -1);
    CHECK(j_find(&doc, nested, "b") == -1);
    CHECK(j_find(&doc, 0, NULL) == -1);
    CHECK(j_string(&doc, a, out, sizeof out) == -1 && out[0] == '\0');
    CHECK(j_int(&doc, a, 77) == 77);
    CHECK(j_bool(&doc, a, 77) == 77);

    valid("{\"name\":1,\"nested\":{\"name\":3},\"na\\u006de\":2}");
    CHECK(j_int(&doc, j_find(&doc, 0, "name"), -1) == 2);
    valid("{\"\":0,\"ключ\":1,\"\\u043a\\u043b\\u044e\\u0447\":2,\"\\u0000\":3}");
    CHECK(j_int(&doc, j_find(&doc, 0, "ключ"), -1) == 2);
    CHECK(j_int(&doc, j_find(&doc, 0, ""), -1) == 0);
}

static void test_unicode(void)
{
    char out[128];
    const char *expected = "Привет, 😀! \"\\/\b\f\n\r\t";
    const char *json = "\"\\u041fривет, \\uD83D\\uDE00! \\\"\\\\\\/\\b\\f\\n\\r\\t\"";
    size_t n = strlen(expected);
    valid(json);
    CHECK(j_string(&doc, 0, out, sizeof out) == (int)n);
    CHECK(strcmp(out, expected) == 0);
    CHECK(j_string(&doc, 0, out, n + 1) == (int)n);
    CHECK(j_string(&doc, 0, out, n) == -1 && out[0] == '\0');
    CHECK(j_string(&doc, 0, NULL, 4) == -1);
    out[0] = 'x';
    CHECK(j_string(&doc, 0, out, 0) == -1 && out[0] == 'x');
    valid("\"😀 Русский текст\"");
    CHECK(j_string(&doc, 0, out, sizeof out) == (int)strlen("😀 Русский текст"));
    CHECK(strcmp(out, "😀 Русский текст") == 0);
    valid("\"a\\u0000b\"");
    CHECK(j_string(&doc, 0, out, sizeof out) == 3);
    CHECK(out[0] == 'a' && out[1] == '\0' && out[2] == 'b' && out[3] == '\0');
    valid("\"\\uD800\\uDC00\\uDBFF\\uDFFF\"");
    CHECK(j_string(&doc, 0, out, sizeof out) == 8);
    CHECK(memcmp(out, "\xf0\x90\x80\x80\xf4\x8f\xbf\xbf", 8) == 0);
    valid("\"\\u007f\\u0080\\u07ff\\u0800\\uffff\"");
    CHECK(j_string(&doc, 0, out, sizeof out) == 11);
    valid("\"\"");
    CHECK(j_string(&doc, 0, out, 1) == 0 && out[0] == '\0');
}

static void test_invalid(void)
{
    static const char *bad[] = {
        "", " \t\r\n", "{", "[", "\"", "tru", "fals", "nul", "True", "NULL",
        "{}[]", "{} x", "truefalse", "null0", "1 2", "\xef\xbb\xbf{}",
        "{a:1}", "{1:2}", "{\"a\"}", "{\"a\":}", "{\"a\":1,}",
        "{\"a\":1 \"b\":2}", "{\"a\":1,,\"b\":2}", "{\"a\",1}",
        "[1,]", "[,1]", "[1,,2]", "[1 2]", "[}", "{]", "[[1]",
        "00", "01", "-01", "+1", "-", "--1", "1.", ".1", "-.1",
        "1e", "1e+", "1e-", "1e++2", "NaN", "Infinity", "0x1", "1_2",
        "\"\\x20\"", "\"\\v\"", "\"\\'\"", "\"abc\\", "\"\\u\"",
        "\"\\u123\"", "\"\\u12xz\"", "\"\\uD800\"", "\"\\uDC00\"",
        "\"\\uD800\\u0041\"", "\"\\uD800\\uD800\"", "\"\\uD800x\"",
        "\"\\uD800\\Udc00\"", "\"\\uD800\\uDC0\"", "\"\n\"", "\"\x01\"",
        "\"\x80\"", "\"\xc0\xaf\"", "\"\xc1\xbf\"", "\"\xc2\"",
        "\"\xe0\x80\x80\"", "\"\xed\xa0\x80\"", "\"\xf0\x80\x80\x80\"",
        "\"\xf4\x90\x80\x80\"", "\"\xf5\x80\x80\x80\"", "\"\xe2\x82\"",
        "\"\xc2x\"", "\"\xff\"", "[\v1]", "[\f1]"
    };
    size_t i;
    char prefix[128];
    const char *complete = "{\"строка\":\"\\uD83D\\uDE00\\n\",\"число\":-12.34e+5,\"массив\":[true,null,{}]}";
    for (i = 0; i < sizeof bad / sizeof bad[0]; ++i) invalid(bad[i]);
    for (i = 0; i < strlen(complete); ++i) {
        memcpy(prefix, complete, i);
        prefix[i] = '\0';
        invalid(prefix);
    }
    valid(complete);
    CHECK(j_parse(&doc, NULL) == -1 && doc.count == 0);
    CHECK(j_parse(NULL, "{}") == -1);
    CHECK(j_count(NULL, 0) == -1);
    CHECK(j_find(NULL, 0, "a") == -1);
    CHECK(j_at(NULL, 0, 0) == -1);
    CHECK(j_int(NULL, 0, 13) == 13);
    CHECK(j_bool(NULL, 0, 13) == 13);
    CHECK(j_string(NULL, 0, prefix, sizeof prefix) == -1 && prefix[0] == '\0');
    valid("0");
    CHECK(j_int(&doc, -1, 13) == 13);
    CHECK(j_int(&doc, doc.count, 13) == 13);
}

static void test_numbers(void)
{
    static const char *good[] = { "0", "-0", "123", "-123", "0.0", "-0.01", "1e0", "1E+2", "1e-2", "12.34e+56" };
    size_t i;
    char number[64];
    for (i = 0; i < sizeof good / sizeof good[0]; ++i) valid(good[i]);
    valid("-123");
    CHECK(j_int(&doc, 0, 9) == -123);
    valid("-0");
    CHECK(j_int(&doc, 0, 9) == 0);
    valid("1.0");
    CHECK(j_int(&doc, 0, 9) == 9);
    valid("1e0");
    CHECK(j_int(&doc, 0, 9) == 9);
    snprintf(number, sizeof number, "%lld", LLONG_MAX);
    valid(number);
    CHECK(j_int(&doc, 0, 9) == LLONG_MAX);
    snprintf(number, sizeof number, "%lld", LLONG_MIN);
    valid(number);
    CHECK(j_int(&doc, 0, 9) == LLONG_MIN);
    valid("9223372036854775808");
    CHECK(j_int(&doc, 0, 9) == 9);
    valid("-9223372036854775809");
    CHECK(j_int(&doc, 0, 9) == 9);
    valid("9999999999999999999999999999999999999999999999");
    CHECK(j_int(&doc, 0, 9) == 9);
    valid("true");
    CHECK(j_bool(&doc, 0, 9) == 1 && j_int(&doc, 0, 9) == 9);
    valid("false");
    CHECK(j_bool(&doc, 0, 9) == 0);
    valid("null");
    CHECK(j_bool(&doc, 0, 9) == 9);
}

static void test_bounds(void)
{
    char nested[2 * (J_MAX_DEPTH + 1) + 2];
    char many[2 * J_MAX_TOKENS + 4];
    int i, n;
    for (n = J_MAX_DEPTH; n <= J_MAX_DEPTH + 1; ++n) {
        for (i = 0; i < n; ++i) nested[i] = '[';
        nested[n] = '0';
        for (i = 0; i < n; ++i) nested[n + 1 + i] = ']';
        nested[2 * n + 1] = '\0';
        if (n == J_MAX_DEPTH) valid(nested); else invalid(nested);
    }
    for (n = J_MAX_DEPTH; n <= J_MAX_DEPTH + 1; ++n) {
        for (i = 0; i < n; ++i) nested[i] = '[';
        for (i = 0; i < n; ++i) nested[n + i] = ']';
        nested[2 * n] = '\0';
        if (n == J_MAX_DEPTH) valid(nested); else invalid(nested);
    }
    /* Корневой массив занимает один токен. */
    for (n = J_MAX_TOKENS - 1; n <= J_MAX_TOKENS; ++n) {
        size_t pos = 0;
        many[pos++] = '[';
        for (i = 0; i < n; ++i) {
            if (i) many[pos++] = ',';
            many[pos++] = '0';
        }
        many[pos++] = ']';
        many[pos] = '\0';
        if (n == J_MAX_TOKENS - 1) {
            valid(many);
            CHECK(doc.count == J_MAX_TOKENS);
            CHECK(j_count(&doc, 0) == n);
            CHECK(j_at(&doc, 0, n - 1) == n);
        } else invalid(many);
    }
    valid("{}");
    CHECK(doc.count == 1);
}

static void test_quote(void)
{
    static const char *texts[] = {
        "", "Привет 😀", "\"цитата\"\nновая строка\\путь", "\b\f\n\r\t", "\x01\x1f", "/"
    };
    char quoted[256], decoded[256];
    size_t i, cap;
    for (i = 0; i < sizeof texts / sizeof texts[0]; ++i) {
        int n = j_quote(texts[i], quoted, sizeof quoted);
        CHECK(n >= 2 && n == (int)strlen(quoted));
        valid(quoted);
        CHECK(j_string(&doc, 0, decoded, sizeof decoded) == (int)strlen(texts[i]));
        CHECK(strcmp(decoded, texts[i]) == 0);
        for (cap = 1; cap <= (size_t)n; ++cap) {
            quoted[0] = 'x';
            CHECK(j_quote(texts[i], quoted, cap) == -1 && quoted[0] == '\0');
        }
        CHECK(j_quote(texts[i], quoted, (size_t)n + 1) == n);
    }
    CHECK(j_quote("\x01\x1f", quoted, sizeof quoted) == 14);
    CHECK(strcmp(quoted, "\"\\u0001\\u001f\"") == 0);
    CHECK(j_quote("\xc0\xaf", quoted, sizeof quoted) == -1 && quoted[0] == '\0');
    CHECK(j_quote(NULL, quoted, sizeof quoted) == -1 && quoted[0] == '\0');
    CHECK(j_quote("x", NULL, 8) == -1);
    quoted[0] = 'x';
    CHECK(j_quote("x", quoted, 0) == -1 && quoted[0] == 'x');
}

static void test_large_string(void)
{
    /* Содержимое строки не расходует отдельный токен на каждый байт. */
    char input[20003], out[20001];
    int i;
    input[0] = '"';
    for (i = 1; i <= 20000; ++i) input[i] = 'a';
    input[20001] = '"';
    input[20002] = '\0';
    valid(input);
    CHECK(doc.count == 1);
    CHECK(j_string(&doc, 0, out, sizeof out) == 20000);
    CHECK(out[0] == 'a' && out[19999] == 'a' && out[20000] == '\0');
    CHECK(j_string(&doc, 0, out, sizeof out - 1) == -1 && out[0] == '\0');
    input[20001] = '\0';
    invalid(input);
}

static void test_mixed_depth(void)
{
    char input[8 * (J_MAX_DEPTH + 1) + 2];
    int n, i;
    for (n = J_MAX_DEPTH; n <= J_MAX_DEPTH + 1; ++n) {
        size_t pos = 0;
        for (i = 0; i < n; ++i) {
            if (i % 2) input[pos++] = '[';
            else {
                memcpy(input + pos, "{\"x\":", 5);
                pos += 5;
            }
        }
        input[pos++] = '0';
        for (i = n - 1; i >= 0; --i) input[pos++] = i % 2 ? ']' : '}';
        input[pos] = '\0';
        if (n == J_MAX_DEPTH) valid(input); else invalid(input);
    }
}

static void test_fuzz(void)
{
    /* Воспроизводимая проверка произвольных байтов и мутаций реального JSON. */
    static const char seed[] = "{\"x\":[1,true,false,null,\"\\uD83D\\uDE00\"],\"y\":{\"z\":-1.5e2}}";
    unsigned long state = 0x12345678UL;
    char input[96], out[96];
    int run;
    for (run = 0; run < 20000; ++run) {
        size_t length, i;
        state = (state * 1664525UL + 1013904223UL) & 0xffffffffUL;
        if (run % 2) {
            length = strlen(seed);
            memcpy(input, seed, length);
            i = (size_t)(state % length);
            state = (state * 1664525UL + 1013904223UL) & 0xffffffffUL;
            input[i] = (char)(1 + state % 255);
        } else {
            length = (size_t)(state % 95);
            for (i = 0; i < length; ++i) {
                state = (state * 1664525UL + 1013904223UL) & 0xffffffffUL;
                input[i] = (char)(1 + state % 255);
            }
        }
        input[length] = '\0';
        if (j_parse(&doc, input) == 0) {
            int token;
            CHECK(doc.count >= 1 && doc.count <= J_MAX_TOKENS);
            for (token = 0; token < doc.count; ++token) {
                CHECK(doc.tokens[token].end <= length);
                if (doc.tokens[token].kind == J_STRING) {
                    CHECK(j_string(&doc, token, out, sizeof out) >= 0);
                } else if (doc.tokens[token].kind == J_ARRAY) {
                    int count = j_count(&doc, token);
                    CHECK(count >= 0);
                    CHECK(j_at(&doc, token, count) == -1);
                    if (count) CHECK(j_at(&doc, token, count - 1) > token);
                } else if (doc.tokens[token].kind == J_OBJECT) {
                    (void)j_find(&doc, token, "x");
                }
                (void)j_int(&doc, token, 7);
                (void)j_bool(&doc, token, 7);
            }
        } else CHECK(doc.count == 0);
    }
}

static void test_bytes(void)
{
    char input[2], quoted[16], decoded[16];
    unsigned int byte;
    for (byte = 1; byte < 256; ++byte) {
        int n;
        input[0] = (char)byte;
        input[1] = '\0';
        n = j_quote(input, quoted, sizeof quoted);
        if (byte < 128) {
            CHECK(n >= 3);
            valid(quoted);
            CHECK(j_string(&doc, 0, decoded, sizeof decoded) == 1);
            CHECK((unsigned char)decoded[0] == byte && decoded[1] == '\0');
        } else CHECK(n == -1);
    }
}

int main(void)
{
    test_structure();
    test_unicode();
    test_invalid();
    test_numbers();
    test_bounds();
    test_quote();
    test_large_string();
    test_mixed_depth();
    test_fuzz();
    test_bytes();
    printf("JSON: %lu проверок, ошибок: %d\n", checks, failures);
    return failures ? EXIT_FAILURE : EXIT_SUCCESS;
}
