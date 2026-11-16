#define _POSIX_C_SOURCE 200809L
#define _DEFAULT_SOURCE
#include "audio.h"
#include "hls.h"
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define PLAYLIST_CAP (512u * 1024u)
#define INIT_CAP (1024u * 1024u)
#define MASTER_HOPS 3

static void message(char *out, size_t cap, const char *text) {
    if (out && cap) snprintf(out, cap, "%s", text);
}

static const char *tool(const char *name, const char *fallback) {
    const char *value = getenv(name);
    return value && *value ? value : fallback;
}

static void defaults(void) {
    signal(SIGTERM, SIG_DFL);
    signal(SIGINT, SIG_DFL);
    signal(SIGCHLD, SIG_DFL);
    sigset_t empty;
    sigemptyset(&empty);
    sigprocmask(SIG_SETMASK, &empty, NULL);
}

static void close_extra(int keep1, int keep2) {
    long maxfd = sysconf(_SC_OPEN_MAX);
    if (maxfd < 256) maxfd = 256;
    if (maxfd > 65536) maxfd = 65536;
    for (int fd = 3; fd < maxfd; ++fd)
        if (fd != keep1 && fd != keep2) close(fd);
}

static int allwrite(int fd, const void *data, size_t size) {
    const unsigned char *next = data;
    while (size) {
        ssize_t written = write(fd, next, size);
        if (written < 0 && errno == EINTR) continue;
        if (written <= 0) return -1;
        next += written;
        size -= (size_t)written;
    }
    return 0;
}

static pid_t spawn(char *const argv[], int input, int output) {
    pid_t pid = fork();
    if (pid != 0) return pid;
    defaults();
    signal(SIGPIPE, SIG_DFL);
    if ((input >= 0 && dup2(input, STDIN_FILENO) < 0) ||
        (output >= 0 && dup2(output, STDOUT_FILENO) < 0)) _exit(126);
    int nullfd = open("/dev/null", O_WRONLY);
    if (nullfd >= 0) dup2(nullfd, STDERR_FILENO);
    close_extra(-1, -1);
    execv(argv[0], argv);
    _exit(127);
}

static int child_result(pid_t pid) {
    int status;
    pid_t result;
    do { result = waitpid(pid, &status, 0); } while (result < 0 && errno == EINTR);
    return result == pid && WIFEXITED(status) && WEXITSTATUS(status) == 0 ? 0 : -1;
}

/* Сеть получает только публичный media URL: OAuth и client_id не добавляются.
 * В памяти хранятся ограниченные манифесты/сегменты; весь трек — только в pipe. */
static int fetch(const ScConfig *cfg, const char *url, FILE *output,
                 unsigned char **memory, size_t *length, size_t limit,
                 char *err, size_t cap) {
    if (strncmp(url, "https://", 8)) {
        message(err, cap, "Медиа: разрешён только HTTPS");
        return -1;
    }
    int fds[2];
    if (pipe(fds)) { message(err, cap, "Медиа: не удалось создать pipe"); return -1; }
    char *args[] = {(char *)cfg->curl, "--silent", "--fail", "--location",
        "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "5",
        "--max-time", limit ? "45" : "7200", "--speed-limit", "1", "--speed-time", "30",
        "--proxy", (char *)cfg->proxy, "--noproxy", cfg->proxy[0] ? "" : "*",
        "--", (char *)url, NULL};
    pid_t child = spawn(args, -1, fds[1]);
    close(fds[1]);
    unsigned char buffer[16384], *data = NULL;
    size_t used = 0, allocated = 0;
    int failed = child < 0;
    while (!failed) {
        ssize_t got = read(fds[0], buffer, sizeof(buffer));
        if (got < 0 && errno == EINTR) continue;
        if (got <= 0) { if (got < 0) failed = 1; break; }
        size_t count = (size_t)got;
        if (limit && (used > limit || count > limit - used)) {
            message(err, cap, "Медиа: превышен лимит размера"); failed = 1; break;
        }
        if (memory) {
            if (used + count + 1 > allocated) {
                size_t next = allocated ? allocated * 2 : sizeof(buffer) + 1;
                if (next < used + count + 1) next = used + count + 1;
                if (next > limit + 1) next = limit + 1;
                unsigned char *grown = realloc(data, next);
                if (!grown) { message(err, cap, "Медиа: недостаточно памяти"); failed = 1; break; }
                data = grown; allocated = next;
            }
            memcpy(data + used, buffer, count);
        } else if (fwrite(buffer, 1, count, output) != count) {
            message(err, cap, "Медиа: декодер закрыл вход"); failed = 1; break;
        }
        used += count;
    }
    close(fds[0]);
    if (child > 0) {
        if (failed) kill(child, SIGTERM);
        if (child_result(child)) {
            if (!failed) message(err, cap, "Медиа: загрузка завершилась с ошибкой");
            failed = 1;
        }
    }
    if (failed || (memory && !used)) {
        if (!failed) message(err, cap, "Медиа: пустой ответ");
        free(data); return -1;
    }
    if (memory) { data[used] = 0; *memory = data; *length = used; }
    return 0;
}

static unsigned char *download(const ScConfig *cfg, const char *url, size_t *size,
                               size_t limit, char *err, size_t cap) {
    unsigned char *data = NULL;
    if (fetch(cfg, url, NULL, &data, size, limit, err, cap)) return NULL;
    return data;
}

/* Единственная процедура разрешения master: init и producer используют один
 * итоговый media playlist. Последний master нельзя принять за медиа-сегмент. */
static int media_playlist(const ScConfig *cfg, const char *url, HlsList *list,
                          char *err, size_t cap) {
    char base[HLS_URL_CAP];
    if (strlen(url) >= sizeof(base)) { message(err, cap, "HLS: слишком длинный URL"); return -1; }
    snprintf(base, sizeof(base), "%s", url);
    for (int depth = 0; depth <= MASTER_HOPS; ++depth) {
        size_t size;
        unsigned char *text = download(cfg, base, &size, PLAYLIST_CAP, err, cap);
        if (!text) return -1;
        int rc = hls_parse((const char *)text, base, list, err, cap);
        free(text);
        if (rc) return -1;
        int master = list->count == 1 && list->duration == 0 && !list->init_url[0];
        if (!master) {
            if (!list->count) { message(err, cap, "HLS: пустой media playlist"); return -1; }
            return 0;
        }
        if (depth == MASTER_HOPS) {
            message(err, cap, "HLS: превышена глубина master playlist"); return -1;
        }
        snprintf(base, sizeof(base), "%s", list->segments[0]);
    }
    return -1;
}

static int produce(const ScConfig *cfg, const HlsList *list, const char *url,
                   const unsigned char asc[2], int fd, char *err, size_t cap) {
    FILE *output = fdopen(fd, "wb");
    if (!output) { close(fd); message(err, cap, "Producer: не удалось открыть pipe"); return -1; }
    int rc = 0;
    if (!list) rc = fetch(cfg, url, output, NULL, NULL, 0, err, cap);
    else for (size_t index = 0; !rc && index < list->count; ++index) {
        size_t size;
        unsigned char *segment = download(cfg, list->segments[index], &size,
                                         HLS_MAX_MEDIA_BYTES, err, cap);
        if (!segment) { rc = -1; break; }
        if (list->init_url[0]) rc = hls_aac_segment(segment, size, asc, output, err, cap);
        else if (size >= 188 && segment[0] == 0x47)
            rc = hls_ts_audio(segment, size, output, err, cap);
        else if (fwrite(segment, 1, size, output) != size) {
            message(err, cap, "Producer: декодер закрыл вход"); rc = -1;
        }
        free(segment);
        if (!rc && fflush(output)) {
            message(err, cap, "Producer: не удалось передать сегмент"); rc = -1;
        }
    }
    if (fclose(output) && !rc) { message(err, cap, "Producer: неполная запись потока"); rc = -1; }
    return rc;
}

static int pipe_pair(int fds[2]) {
    if (pipe(fds)) return -1;
    fcntl(fds[0], F_SETFD, FD_CLOEXEC);
    fcntl(fds[1], F_SETFD, FD_CLOEXEC);
    return 0;
}

static void close_pair(int fds[2]) {
    for (int i = 0; i < 2; ++i) if (fds[i] >= 0) { close(fds[i]); fds[i] = -1; }
}

static void close_end(int *fd) { if (*fd >= 0) { close(*fd); *fd = -1; } }

static void run_pipeline(Audio *audio, const ScConfig *cfg, const ScTrack *track,
                         const char *device, long seek_ms) {
    char url[4096], err[256] = "Ошибка аудиопотока";
    int encoded[2] = {-1, -1}, pcm[2] = {-1, -1}, speaker[2] = {-1, -1}, report[2] = {-1, -1};
    HlsList *list = NULL;
    pid_t decoder = -1, sink = -1, producer = -1;
    if (sc_stream(cfg, track, url, sizeof(url), err, sizeof(err))) goto fail;
    int aac = strstr(track->mime, "mp4") != NULL || strstr(track->mime, "aac") != NULL;
    unsigned char asc[2] = {0, 0};
    int rate = 44100, channels = 2;
    if (!strcmp(track->protocol, "hls")) {
        list = calloc(1, sizeof(*list));
        if (!list) { message(err, sizeof(err), "HLS: недостаточно памяти"); goto fail; }
        if (media_playlist(cfg, url, list, err, sizeof(err))) goto fail;
        if (list->init_url[0]) {
            size_t size;
            unsigned char *init = download(cfg, list->init_url, &size, INIT_CAP, err, sizeof(err));
            if (!init) goto fail;
            int rc = hls_aac_config(init, size, asc, err, sizeof(err));
            free(init);
            if (rc) goto fail;
            aac = 1;
            static const int rates[] = {96000, 88200, 64000, 48000, 44100, 32000,
                                       24000, 22050, 16000, 12000, 11025, 8000, 7350};
            int index = ((asc[0] & 7) << 1) | (asc[1] >> 7);
            channels = (asc[1] >> 3) & 15;
            if ((asc[0] >> 3) != 2 || index > 12 || channels < 1 || channels > 2) {
                message(err, sizeof(err), "AAC: поддержан только AAC-LC mono/stereo"); goto fail;
            }
            rate = rates[index];
        } else if (aac) {
            message(err, sizeof(err), "AAC: требуется fMP4 с init-сегментом"); goto fail;
        }
    } else if (strcmp(track->protocol, "progressive") || aac) {
        message(err, sizeof(err), "Медиа: поддержаны progressive MP3 и HLS MP3/fMP4 AAC"); goto fail;
    }
    if (pipe_pair(encoded) || pipe_pair(pcm) || pipe_pair(speaker) || pipe_pair(report)) {
        message(err, sizeof(err), "Аудио: не удалось создать pipe"); goto fail;
    }
    const char *program = aac ? tool("PANEL_FAAD", "/data/opt/entware/bin/faad")
                              : tool("PANEL_MPG123", "/data/opt/entware/bin/mpg123");
    /* mpg123 --rate действительно ресэмплирует; --stereo дублирует mono.
     * faad читает '-' только как ADTS stdin: MP4 на pipe ему не передаётся.
     * -f 2 — RAW, -b 1 — signed 16-bit; -s не является ресэмплером. */
    char *mpg[] = {(char *)program, "-q", "-s", "--stereo", "--rate", "44100",
                   "--encoding", "s16", "-", NULL};
    char *faad[] = {(char *)program, "-q", "-f", "2", "-b", "1", "-w", "-", NULL};
    decoder = spawn(aac ? faad : mpg, encoded[0], pcm[1]);
    char rate_arg[16], channels_arg[8];
    snprintf(rate_arg, sizeof(rate_arg), "%d", rate);
    snprintf(channels_arg, sizeof(channels_arg), "%d", channels);
    char *aplay[] = {(char *)tool("PANEL_APLAY", "/usr/bin/aplay"), "-q", "-D", (char *)device,
                     "-t", "raw", "-f", "S16_LE", "-r", rate_arg, "-c", channels_arg,
                     "--buffer-time=100000", NULL};
    if (decoder < 0) { message(err, sizeof(err), "Аудио: не удалось запустить декодер"); goto fail; }
    sink = spawn(aplay, speaker[0], -1);
    if (sink < 0) { message(err, sizeof(err), "Аудио: не удалось запустить приёмник PCM"); goto fail; }
    producer = fork();
    if (!producer) {
        defaults();
        signal(SIGPIPE, SIG_IGN);
        close_extra(encoded[1], report[1]);
        char detail[256] = "Producer: ошибка медиа-потока";
        int rc = produce(cfg, list, url, asc, encoded[1], detail, sizeof(detail));
        if (rc) allwrite(report[1], detail, strlen(detail));
        close(report[1]);
        free(list);
        _exit(rc ? 1 : 0);
    }
    if (producer < 0) { message(err, sizeof(err), "Аудио: не удалось запустить producer"); goto fail; }
    free(list); list = NULL;
    close_pair(encoded); close_end(&pcm[1]); close_end(&speaker[0]); close_end(&report[1]);

    size_t frame = (size_t)channels * sizeof(int16_t), pending = 0;
    unsigned char buffer[8192 + 4];
    uint64_t discard = (uint64_t)seek_ms * (unsigned)rate / 1000;
    uint64_t played = 0;
    int failed = 0;
    for (;;) {
        ssize_t got = read(pcm[0], buffer + pending, sizeof(buffer) - pending);
        if (got < 0 && errno == EINTR) continue;
        if (got <= 0) {
            if (got < 0 || pending) {
                message(err, sizeof(err), pending ? "PCM: неполный аудиофрейм" : "PCM: ошибка чтения");
                failed = 1;
            }
            break;
        }
        size_t used = pending + (size_t)got, aligned = used - used % frame;
        size_t frames = aligned / frame;
        size_t skip_frames = discard > frames ? frames : (size_t)discard;
        discard -= skip_frames;
        size_t skip = skip_frames * frame;
        int volume = atomic_load(&audio->shared->volume);
        if (volume < 0) volume = 0;
        if (volume > 100) volume = 100;
        for (size_t offset = skip; offset < aligned; offset += sizeof(int16_t)) {
            int16_t value;
            memcpy(&value, buffer + offset, sizeof(value));
            value = (int16_t)((int)value * volume / 100);
            memcpy(buffer + offset, &value, sizeof(value));
        }
        if (aligned > skip) {
            if (allwrite(speaker[1], buffer + skip, aligned - skip)) {
                message(err, sizeof(err), "PCM: приёмник закрыл pipe"); failed = 1; break;
            }
            played += (aligned - skip) / frame;
            /* Деление — от общего числа фреймов, без потери дробей на каждом read. */
            atomic_store(&audio->shared->bytes, (long long)seek_ms * 1000 +
                         (long long)(played * 1000000 / (unsigned)rate));
            atomic_store(&audio->shared->stage, 2);
        }
        pending = used - aligned;
        if (pending) memmove(buffer, buffer + aligned, pending);
    }
    close_pair(pcm); close_pair(speaker);
    if (failed) goto fail;
    int producer_failed = child_result(producer); producer = -1;
    char detail[256];
    ssize_t count;
    do { count = read(report[0], detail, sizeof(detail) - 1); } while (count < 0 && errno == EINTR);
    close_pair(report);
    int decoder_failed = child_result(decoder); decoder = -1;
    int sink_failed = child_result(sink); sink = -1;
    if (producer_failed) {
        if (count > 0) { detail[count] = 0; message(err, sizeof(err), detail); }
        else message(err, sizeof(err), "Producer: процесс завершился с ошибкой");
        goto fail;
    }
    if (decoder_failed) {
        message(err, sizeof(err), aac ? "AAC: faad не декодировал ADTS stdin (нужна совместимая версия)"
                                    : "MP3: декодер завершился с ошибкой"); goto fail;
    }
    if (sink_failed) { message(err, sizeof(err), "PCM: приёмник завершился с ошибкой"); goto fail; }
    if (!played) { message(err, sizeof(err), "PCM: декодер не выдал аудио после перемотки"); goto fail; }
    atomic_store(&audio->shared->stage, 3);
    _exit(0);
fail:
    free(list);
    close_pair(encoded); close_pair(pcm); close_pair(speaker); close_pair(report);
    message(audio->shared->error, sizeof(audio->shared->error), err);
    atomic_store(&audio->shared->stage, -1);
    /* audio_poll/stop снимает всю группу, в том числе дочерний curl producer. */
    _exit(1);
}

int audio_init(Audio *audio) {
    memset(audio, 0, sizeof(*audio));
    audio->shared = mmap(NULL, sizeof(*audio->shared), PROT_READ | PROT_WRITE,
                         MAP_SHARED | MAP_ANONYMOUS, -1, 0);
    if (audio->shared == MAP_FAILED) { audio->shared = NULL; return -1; }
    memset(audio->shared, 0, sizeof(*audio->shared));
    atomic_store(&audio->shared->volume, 100);
    return 0;
}

void audio_stop(Audio *audio) {
    if (audio->pid > 0) {
        kill(-audio->pid, SIGCONT);
        kill(-audio->pid, SIGTERM);
        struct timespec delay = {0, 20000000};
        nanosleep(&delay, NULL);
        kill(-audio->pid, SIGKILL);
        while (waitpid(audio->pid, NULL, 0) < 0 && errno == EINTR) {}
        audio->pid = 0;
    }
    if (audio->shared) atomic_store(&audio->shared->stage, 0);
}

int audio_start(Audio *audio, const ScConfig *cfg, const ScTrack *track,
                const char *device, long seek_ms) {
    if (!audio || !audio->shared || !cfg || !track || !device || seek_ms < 0 ||
        (unsigned long long)seek_ms > (unsigned long long)LLONG_MAX / 1000) return -1;
    audio_stop(audio);
    atomic_store(&audio->shared->bytes, (long long)seek_ms * 1000);
    audio->shared->error[0] = 0;
    atomic_store(&audio->shared->stage, 1);
    audio->pid = fork();
    if (!audio->pid) {
        setpgid(0, 0);
        /* Нельзя наследовать handler main: SIGTERM обязан остановить supervisor. */
        defaults();
        close_extra(-1, -1);
        signal(SIGPIPE, SIG_IGN);
        run_pipeline(audio, cfg, track, device, seek_ms);
        _exit(1);
    }
    if (audio->pid < 0) {
        audio->pid = 0;
        message(audio->shared->error, sizeof(audio->shared->error), "Аудио: fork завершился с ошибкой");
        atomic_store(&audio->shared->stage, -1);
        return -1;
    }
    setpgid(audio->pid, audio->pid);
    return 0;
}

void audio_pause(Audio *audio, int paused) {
    if (audio->pid > 0) kill(-audio->pid, paused ? SIGSTOP : SIGCONT);
}

int audio_poll(Audio *audio) {
    if (audio->pid <= 0) return 0;
    int status;
    pid_t done = waitpid(audio->pid, &status, WNOHANG);
    if (!done || (done < 0 && errno == EINTR)) return 0;
    kill(-audio->pid, SIGKILL);
    audio->pid = 0;
    return done > 0 && WIFEXITED(status) && WEXITSTATUS(status) == 0 ? 1 : -1;
}

long audio_position(const Audio *audio) {
    return audio->shared ? (long)(atomic_load(&audio->shared->bytes) / 1000) : 0;
}

void audio_close(Audio *audio) {
    audio_stop(audio);
    if (audio->shared) { munmap(audio->shared, sizeof(*audio->shared)); audio->shared = NULL; }
}
