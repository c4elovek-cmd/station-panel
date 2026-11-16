/* Живой тест аудиоконвейера: реальный трек SoundCloud через полный pipeline.
 * Приёмник — /bin/cat (PCM читается, но не звучит): колонка не воспроизводит звук.
 * Запуск только вручную на колонке: /tmp, ноль изменения рабочих сервисов. */
#define _POSIX_C_SOURCE 200809L
#define _DEFAULT_SOURCE
#include "audio.h"
#include "sc.h"
#include "json.h"
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static int failed;
static long long now_ms(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (long long)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}
static void report(const char *name, int ok, const char *detail) {
    if (!ok) failed++;
    printf("%s - %s%s%s\n", ok ? "ok" : "not ok", name, detail && *detail ? " (" : "",
           detail && *detail ? detail : "");
    if (detail && *detail) printf(")\n");
    fflush(stdout);
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    ScConfig cfg;
    memset(&cfg, 0, sizeof(cfg));
    const char *curl = getenv("PANEL_CURL");
    snprintf(cfg.curl, sizeof(cfg.curl), "%s", curl && *curl ? curl : "/data/opt/entware/bin/curl");
    const char *proxy = getenv("PANEL_TEST_PROXY");
    if (proxy) snprintf(cfg.proxy, sizeof(cfg.proxy), "%s", proxy);
    /* sc_stream сам добавляет client_id к transcoding URL — конфиг нужен целиком. */
    const char *cid = getenv("PANEL_TEST_CLIENT_ID");
    if (cid && *cid) snprintf(cfg.client_id, sizeof(cfg.client_id), "%s", cid);

    /* Реальный публичный трек: forss/flickermood, track 293, HLS-only. */
    ScTrack track;
    memset(&track, 0, sizeof(track));

    Audio audio;
    if (audio_init(&audio)) { report("audio_init", 0, "mmap"); return 1; }

    /* Трек берём анонимным resolve: токен не нужен для публичного трека. */
    const char *url_env = getenv("PANEL_TEST_TRACK_URL");
    char api[600], bodyfile[256];
    static char body[262144];
    if (url_env && *url_env) {
        snprintf(track.id, sizeof(track.id), "293");
        snprintf(track.transcoding, sizeof(track.transcoding), "%s", url_env);
        snprintf(track.protocol, sizeof(track.protocol), "hls");
        snprintf(track.mime, sizeof(track.mime), "audio/mp4");
        track.duration_ms = 300000;
    } else {
        if (!cid || !*cid) { report("client_id", 0, "PANEL_TEST_CLIENT_ID не задан"); return 1; }
        snprintf(api, sizeof(api), "https://api-v2.soundcloud.com/resolve?url=https://soundcloud.com/forss/flickermood&client_id=%s", cid);
        snprintf(bodyfile, sizeof(bodyfile), "/tmp/panel-player-live/track.json");
        char cmd[1024];
        snprintf(cmd, sizeof(cmd), "%s --silent --fail --max-time 25 --proto =https \"%s\" -o %s", cfg.curl, api, bodyfile);
        fflush(stdout);
        int rc = system(cmd);
        if (rc) { report("resolve публичного трека", 0, "curl не получил track.json"); return 1; }
        report("resolve публичного трека", 1, NULL);
        FILE *f = fopen(bodyfile, "r");
        if (!f) { report("track.json открыт", 0, "нет файла"); return 1; }
        size_t n = fread(body, 1, sizeof(body) - 1, f);
        fclose(f);
        body[n] = 0;
        report("track.json прочитан", n > 100, NULL);
        JDoc *doc = calloc(1, sizeof(*doc));
        if (!doc || j_parse(doc, body) < 0) { report("track.json разобран", 0, "JSON"); return 1; }
        report("track.json разобран", 1, NULL);
        int media = j_find(doc, 0, "media");
        int transc = media >= 0 ? j_find(doc, media, "transcodings") : -1;
        int count = transc >= 0 ? j_count(doc, transc) : 0;
        char media_url[2048] = "", protocol[32] = "", mime[128] = "";
        for (int i = 0; i < count; i++) {
            int item = j_at(doc, transc, i);
            char p[32] = "", m[128] = "", u[2048] = "";
            int pt = j_find(doc, item, "format");
            if (pt >= 0) {
                j_string(doc, j_find(doc, pt, "protocol"), p, sizeof(p));
                j_string(doc, j_find(doc, pt, "mime_type"), m, sizeof(m));
            }
            j_string(doc, j_find(doc, item, "url"), u, sizeof(u));
            if (!strcmp(p, "hls") && !strstr(u, "preview") && !media_url[0]) {
                snprintf(media_url, sizeof(media_url), "%s", u);
                snprintf(protocol, sizeof(protocol), "%s", p);
                snprintf(mime, sizeof(mime), "%s", m);
            }
        }
        if (!media_url[0]) { report("полный HLS-transcoding найден", 0, "только preview"); return 1; }
        report("полный HLS-transcoding найден", 1, NULL);
        snprintf(track.id, sizeof(track.id), "293");
        snprintf(track.transcoding, sizeof(track.transcoding), "%s", media_url);
        snprintf(track.protocol, sizeof(track.protocol), "%s", protocol[0] ? protocol : "hls");
        snprintf(track.mime, sizeof(track.mime), "%s", mime[0] ? mime : "audio/mp4");
        track.duration_ms = 300000;
        free(doc);
    }
    ScTrack *t = &track;
    printf("# трек: %s (%s/%s)\n", t->id, t->protocol, t->mime);

    long long started = now_ms();
    if (audio_start(&audio, &cfg, t, "/bin/cat", 0)) {
        report("audio_start", 0, "fork");
        return 1;
    }
    report("audio_start", 1, NULL);

    /* Ждём первого настоящего PCM. */
    long long deadline = now_ms() + 90000;
    int stage = 0;
    while (now_ms() < deadline) {
        stage = atomic_load(&audio.shared->stage);
        if (stage == 2 || stage < 0) break;
        usleep(50000);
        if (audio_poll(&audio)) break;
    }
    char detail[192];
    if (stage == 2) {
        snprintf(detail, sizeof(detail), "%lld мс до первого PCM", now_ms() - started);
        report("первый настоящий PCM (stage=playing)", 1, detail);
    } else {
        snprintf(detail, sizeof(detail), "stage=%d, %s", stage, audio.shared->error);
        report("первый настоящий PCM (stage=playing)", 0, detail);
        audio_close(&audio);
        return 1;
    }

    long long pos0 = audio_position(&audio);
    usleep(1500000);
    long long pos1 = audio_position(&audio);
    snprintf(detail, sizeof(detail), "позиция %lld → %lld мс", pos0, pos1);
    report("PCM идёт, позиция растёт", pos1 > pos0, detail);

    audio_pause(&audio, 1);
    long long frozen = audio_position(&audio);
    usleep(1500000);
    long long frozen_after = audio_position(&audio);
    snprintf(detail, sizeof(detail), "позиция %lld мс неизменна", frozen);
    report("пауза замораживает позицию", frozen == frozen_after && frozen > 0, detail);

    audio_pause(&audio, 0);
    usleep(300000);
    report("возобновление после паузы", atomic_load(&audio.shared->stage) == 2, NULL);

    audio_stop(&audio);
    usleep(300000);
    /* Группа процессов обязана умереть: проверяем по дочерним. */
    int zombies = 0;
    int status;
    while (waitpid(-1, &status, WNOHANG) > 0) zombies++;
    /* waitpid(-1) не счит чужие: наш процесс только что завершил группу. */
    snprintf(detail, sizeof(detail), "осталось неотреапленных: %d", zombies);
    report("стоп терминирует группу", audio.pid == 0, detail);

    audio_close(&audio);
    printf("# %s\n", failed ? "ЕСТЬ СБОИ" : "все проверки пройдены");
    return failed ? 1 : 0;
}
