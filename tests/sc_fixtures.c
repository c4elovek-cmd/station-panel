/* Автономные тесты sc.c без сети: запускать на хосте сборки.
 * Проверяются декодирование очередей, выбор transcoding, разбор ID/URN,
 * отклонение preview-фрагментов и безопасность адресов transcoding.
 * Сборка автономного декодера, в том числе на Windows:
 *   cc -std=c11 -Wall -Wextra -Werror -UNDEBUG -DSCTEST_MAIN -DSC_FIXTURE_ONLY \
 *      deploy/soundya-ng/src/json.c deploy/soundya-ng/src/sc.c \
 *      tests/sc_fixtures.c -o tests/sc_fixtures
 * Для Linux SC_FIXTURE_ONLY можно не задавать.
 */
#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../deploy/soundya-ng/src/sc.h"
#include "sc_fixtures.h"

static int failures = 0;

static void expect_str(const char *name, const char *actual, const char *wanted) {
    if (!actual || strcmp(actual, wanted)) {
        printf("FAIL %s: ожидалось «%s», получено «%s»\n", name, wanted, actual ? actual : "(null)");
        ++failures;
    }
}

static void expect_long(const char *name, long actual, long wanted) {
    if (actual != wanted) {
        printf("FAIL %s: ожидалось %ld, получено %ld\n", name, wanted, actual);
        ++failures;
    }
}

static void expect_size(const char *name, size_t actual, size_t wanted) {
    if (actual != wanted) {
        printf("FAIL %s: ожидалось %zu, получено %zu\n", name, wanted, actual);
        ++failures;
    }
}

static void expect_empty(const char *name, const char *error) {
    if (!error || error[0]) {
        printf("FAIL %s: ожидалась пустая ошибка, получено «%s»\n", name, error ? error : "(null)");
        ++failures;
    }
}

void sc_fixture_run_search(void) {
    ScQueue q;
    char error[256] = {0};
    /* Поиск: числовой ID, progressive приоритет, пользователь в user.username. */
    const char *body =
        "{\"collection\":[{"
        "\"id\":308946187,\"kind\":\"track\",\"title\":\"Nightfall\",\"duration\":245000,"
        "\"streamable\":true,"
        "\"user\":{\"username\":\"Aurora Bore\",\"urn\":\"soundcloud:users:123\"},"
        "\"permalink_url\":\"https://soundcloud.com/aurora-b/nightfall\","
        "\"artwork_url\":\"https://i1.sndcdn.com/artworks-000123-large.jpg\","
        "\"media\":{\"transcodings\":["
        "{\"url\":\"https://api-v2.soundcloud.com/media/0/track/hls/playlist\","
        "\"preset\":\"mp3\",\"duration\":245000,"
        "\"format\":{\"protocol\":\"hls\",\"mime_type\":\"audio/mpeg\"}},"
        "{\"url\":\"https://api-v2.soundcloud.com/media/0/track/progressive\","
        "\"preset\":\"mp3\",\"duration\":245000,"
        "\"format\":{\"protocol\":\"progressive\",\"mime_type\":\"audio/mpeg\"}}"
        "]}}],\"next_href\":null}";
    if (sc_decode_queue(body, &q, error, sizeof(error))) {
        printf("FAIL search: ошибка декодирования: %s\n", error);
        ++failures;
        return;
    }
    expect_empty("search.error", error);
    expect_size("search.count", q.count, 1);
    expect_str("search.id", q.tracks[0].id, "308946187");
    expect_str("search.title", q.tracks[0].title, "Nightfall");
    expect_str("search.artist", q.tracks[0].artist, "Aurora Bore");
    expect_str("search.protocol", q.tracks[0].protocol, "progressive");
    expect_str("search.mime", q.tracks[0].mime, "audio/mpeg");
    expect_str("search.permalink", q.tracks[0].permalink, "https://soundcloud.com/aurora-b/nightfall");
    expect_long("search.duration", q.tracks[0].duration_ms, 245000);
    expect_str("search.transcoding", q.tracks[0].transcoding,
               "https://api-v2.soundcloud.com/media/0/track/progressive");
}

void sc_fixture_run_urn_and_likes(void) {
    ScQueue q;
    char error[256] = {0};
    /* Лайки: URN-формат ID, HLS audio/mpeg без progressive, обёртка {track:...}. */
    const char *body =
        "{\"collection\":[{\"created_at\":\"2026-01-01\",\"track\":{"
        "\"id\":\"soundcloud:tracks:56789\",\"kind\":\"track\",\"title\":\"Sta​​rt\","
        "\"duration\":180000,\"streamable\":true,"
        "\"user\":{\"username\":\"Who\"},"
        "\"publisher_metadata\":{\"artist\":\"Fallback Artist\"},"
        "\"media\":{\"transcodings\":[{"
        "\"url\":\"https://api-v2.soundcloud.com/media/1/hls\",\"preset\":\"mp3\","
        "\"format\":{\"protocol\":\"hls\",\"mime_type\":\"audio/mpeg\"}}]"
        "}}},"
        "{\"created_at\":\"2026-01-01\",\"track\":{\"id\":42,\"kind\":\"track\","
        "\"title\":\"Zero\",\"duration\":1000,\"streamable\":true,"
        "\"user\":{\"username\":\"X\"},\"media\":{}}}],"
        "\"next_href\":\"https://api-v2.soundcloud.com/users/1/likes?cursor=2\"}";
    if (sc_decode_queue(body, &q, error, sizeof(error))) {
        printf("FAIL likes: ошибка декодирования: %s\n", error);
        ++failures;
        return;
    }
    expect_size("likes.count", q.count, 1);
    expect_str("likes.id", q.tracks[0].id, "56789");
    expect_str("likes.artist", q.tracks[0].artist, "Who");
    expect_str("likes.protocol", q.tracks[0].protocol, "hls");
}

void sc_fixture_run_skip_and_stub(void) {
    ScQueue q;
    char error[256] = {0};
    /* preview и blocked пропускаются; заглушка без media остаётся 0-й, но
     * sc_decode_queue не имеет сети — трек с media:[] исключается тоже. */
    const char *body =
        "{\"tracks\":["
        "{\"id\":1,\"kind\":\"track\",\"title\":\"Blocked\",\"access\":\"blocked\","
        "\"streamable\":true,\"user\":{\"username\":\"a\"},\"media\":{\"transcodings\":[]}},"
        "{\"id\":2,\"kind\":\"track\",\"title\":\"Snip\",\"duration\":200000,"
        "\"streamable\":true,\"user\":{\"username\":\"a\"},"
        "\"media\":{\"transcodings\":[{\"url\":\"https://api-v2.soundcloud.com/media/x\","
        "\"preset\":\"preview\",\"duration\":30000,"
        "\"format\":{\"protocol\":\"progressive\",\"mime_type\":\"audio/mpeg\"}}]}},"
        "{\"id\":3,\"kind\":\"track\",\"title\":\"Stub\",\"duration\":100000,"
        "\"streamable\":true,\"user\":{\"username\":\"a\"},\"media\":{}},"
        "{\"id\":\"3\",\"kind\":\"track\",\"title\":\"Duplicate\",\"duration\":100000,"
        "\"streamable\":true,\"user\":{\"username\":\"b\"},\"media\":{}}]}";
    if (sc_decode_queue(body, &q, error, sizeof(error))) {
        printf("FAIL skip: ошибка декодирования: %s\n", error);
        ++failures;
        return;
    }
    expect_size("skip.count", q.count, 0);
}

void sc_fixture_run_broken(void) {
    ScQueue q;
    char error[256] = {0};
    if (!sc_decode_queue("{\"collection\":[", &q, error, sizeof(error))) {
        printf("FAIL broken: сломанный JSON должен давать ошибку\n");
        ++failures;
    }
    if (!error[0]) {
        printf("FAIL broken: текст ошибки пуст\n");
        ++failures;
    }
}

void sc_fixture_run_security(void) {
    ScQueue q;
    char error[256];
    const char *bad[] = {
        "https://api-v2.soundcloud.com.evil.test/media/a",
        "https://api-v2.soundcloud.com@evil.test/media/a",
        "https://evil.test@api-v2.soundcloud.com/media/a",
        "http://api-v2.soundcloud.com/media/a",
        "https://api-v2.soundcloud.com:8443/media/a",
        "https://api-v2.soundcloud.com/media/preview/a",
        "https://api-v2.soundcloud.com/media/a\\u0000tail"
    };
    for (size_t i = 0; i < sizeof(bad)/sizeof(bad[0]); ++i) {
        char json[2048];
        snprintf(json, sizeof(json),
                 "{\"id\":99,\"kind\":\"track\",\"media\":{\"transcodings\":[{"
                 "\"url\":\"%s\",\"format\":{\"protocol\":\"progressive\","
                 "\"mime_type\":\"audio/mpeg\"}}]}}", bad[i]);
        if (sc_decode_queue(json, &q, error, sizeof(error)) || q.count) {
            printf("FAIL security: небезопасная перекодировка %zu принята\n", i);
            ++failures;
        }
    }
    {
        const char *json = "{\"id\":\"22\\u0000x\",\"media\":{\"transcodings\":[{"
                           "\"url\":\"https://api-v2.soundcloud.com/media/a\","
                           "\"format\":{\"protocol\":\"progressive\",\"mime_type\":\"audio/mpeg\"}}]}}";
        if (sc_decode_queue(json, &q, error, sizeof(error)) || q.count) {
            printf("FAIL security: внутренний NUL в ID принят\n");
            ++failures;
        }
    }
}

void sc_fixture_run_limits(void) {
    ScQueue q;
    char error[256], item[384], json[131072] = "[";
    for (int i = 0; i < 260; ++i) {
        snprintf(item, sizeof(item),
                 "%s{\"id\":%d,\"media\":{\"transcodings\":[{"
                 "\"url\":\"https://api-v2.soundcloud.com/media/a\","
                 "\"format\":{\"protocol\":\"progressive\",\"mime_type\":\"audio/mpeg\"}}]}}",
                 i ? "," : "", i + 1);
        strcat(json, item);
    }
    strcat(json, "]");
    if (sc_decode_queue(json, &q, error, sizeof(error))) {
        printf("FAIL limits: ошибка декодирования: %s\n", error);
        ++failures;
    } else expect_size("limits.count", q.count, 256);
}

void sc_fixture_run_nested(void) {
    ScQueue q;
    char error[256] = {0};
    const char *body =
        "[{\"urn\":\"soundcloud:tracks:777\",\"kind\":\"track\",\"title\":\"Direct array\","
        "\"duration\":90000,\"streamable\":true,\"user\":{\"username\":\"U\"},"
        "\"media\":{\"transcodings\":["
        "{\"url\":\"https://api-v2.soundcloud.com/media/7/a\",\"preset\":\"opus\","
        "\"format\":{\"protocol\":\"hls\",\"mime_type\":\"audio/ogg; codecs=opus\"}},"
        "{\"url\":\"https://api-v2.soundcloud.com/media/7/b\",\"preset\":\"mp3\","
        "\"format\":{\"protocol\":\"hls\",\"mime_type\":\"audio/mpeg\"}}]}}]";
    if (sc_decode_queue(body, &q, error, sizeof(error))) {
        printf("FAIL nested: ошибка декодирования: %s\n", error);
        ++failures;
        return;
    }
    expect_size("nested.count", q.count, 1);
    expect_str("nested.id", q.tracks[0].id, "777");
    /* audio/mpeg HLS (200) выше audio/ogg HLS (100). */
    expect_str("nested.transcoding", q.tracks[0].transcoding, "https://api-v2.soundcloud.com/media/7/b");
    expect_str("nested.mime", q.tracks[0].mime, "audio/mpeg");
}

int sc_fixture_summary(void) { return failures; }

#ifdef SCTEST_MAIN
int main(void) {
    sc_fixture_run_search();
    sc_fixture_run_urn_and_likes();
    sc_fixture_run_skip_and_stub();
    sc_fixture_run_broken();
    sc_fixture_run_nested();
    sc_fixture_run_security();
    sc_fixture_run_limits();
    if (failures) {
        printf("sc_fixtures: %d неудачных проверок\n", failures);
        return 1;
    }
    printf("sc_fixtures: все проверки пройдены\n");
    return 0;
}
#endif

/* Сетевой интеграционный тест и runtime-проверка персональных рекомендаций
 * требуют отдельного раннера и разрешения на использование аккаунта.
 * Ни токенов, ни client_secret в репозитории не хранится.
 */
