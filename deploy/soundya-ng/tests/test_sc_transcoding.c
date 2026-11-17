#include "../src/sc.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Сборка с SC_FIXTURE_ONLY: только JSON-декодер, без сети и curl. */
#define API "https://api-v2.soundcloud.com"
#define TRANSCODING(preset, protocol, mime) \
    "{\"url\":\"" API "/media/" preset "\",\"preset\":\"" preset "\"," \
    "\"duration\":180000,\"format\":{\"protocol\":\"" protocol "\",\"mime_type\":\"" mime "\"}}"
#define AAC TRANSCODING("aac_96k", "hls", "audio/mp4")
#define MPEGURL TRANSCODING("abr_sq", "hls", "audio/mpegurl")
#define MPEG TRANSCODING("mp3_128k", "hls", "audio/mpeg")
#define PROGRESSIVE TRANSCODING("mp3_progressive", "progressive", "audio/mpeg")
#define TRACK(streams) \
    "{\"id\":1,\"kind\":\"track\",\"streamable\":true,\"duration\":180000," \
    "\"media\":{\"transcodings\":[" streams "]}}"

/* Очередь большая: не размещаем её в ограниченном стеке Windows. */
static ScQueue queue;
static unsigned checks;
static int failures;

#define CHECK(expr) do { \
    ++checks; \
    if (!(expr)) { \
        fprintf(stderr, "Ошибка: строка %d, %s\n", __LINE__, #expr); \
        ++failures; \
    } \
} while (0)

static void expect(const char *name, const char *json, const char *url,
                   const char *protocol, const char *mime)
{
    char error[256] = "Старая ошибка";
    int previous = failures;
    int rc;
    queue.count = 7;
    rc = sc_decode_queue(json, &queue, error, sizeof error);
    CHECK(rc == 0);
    CHECK(error[0] == '\0');
    CHECK(queue.count == (url ? 1U : 0U));
    if (url && rc == 0 && queue.count == 1) {
        CHECK(strcmp(queue.tracks[0].transcoding, url) == 0);
        CHECK(strcmp(queue.tracks[0].protocol, protocol) == 0);
        CHECK(strcmp(queue.tracks[0].mime, mime) == 0);
    }
    printf("%s: %s\n", name, failures == previous ? "пройдено" : "ОШИБКА");
}

static void test_aac_before_mpegurl(void)
{
    /* Регрессия 404: второй abr_sq не должен вытеснять первый aac_96k. */
    expect("AAC HLS перед audio/mpegurl", TRACK(AAC "," MPEGURL),
           API "/media/aac_96k", "hls", "audio/mp4");
}

static void test_mpegurl_only(void)
{
    expect("Только audio/mpegurl пропускается", TRACK(MPEGURL), NULL, NULL, NULL);
}

static void test_mpeg_preferred(void)
{
    expect("Настоящий MPEG HLS предпочтительнее AAC", TRACK(AAC "," MPEG "," MPEGURL),
           API "/media/mp3_128k", "hls", "audio/mpeg");
    /* Приоритет не зависит от положения в массиве перекодировок. */
    expect("MPEG HLS сохраняет приоритет перед поздним AAC", TRACK(MPEG "," AAC "," MPEGURL),
           API "/media/mp3_128k", "hls", "audio/mpeg");
}

static void test_progressive_preferred(void)
{
    expect("Progressive предпочтительнее HLS", TRACK(AAC "," MPEG "," PROGRESSIVE "," MPEGURL),
           API "/media/mp3_progressive", "progressive", "audio/mpeg");
    expect("Progressive сохраняет приоритет перед поздним HLS", TRACK(PROGRESSIVE "," MPEG "," AAC "," MPEGURL),
           API "/media/mp3_progressive", "progressive", "audio/mpeg");
}

int main(void)
{
    test_aac_before_mpegurl();
    test_mpegurl_only();
    test_mpeg_preferred();
    test_progressive_preferred();
    printf("SoundCloud: %u проверок, ошибок: %d\n", checks, failures);
    return failures ? EXIT_FAILURE : EXIT_SUCCESS;
}
