#ifndef SOUNDYA_NG_SC_H
#define SOUNDYA_NG_SC_H
#include <stddef.h>

typedef struct { char curl[512],proxy[512],client_id[128],token[2048]; } ScConfig;
typedef struct {
    char id[64],title[512],artist[256],artwork[1024],permalink[1024];
    char transcoding[2048],protocol[32],mime[128];
    long duration_ms;
} ScTrack;
typedef struct { ScTrack tracks[256]; size_t count; } ScQueue;

/* Успех 0; ошибка -1. Ошибки русские, без секретов и содержимого ответа.
 * play: поиск или HTTPS-ссылка SoundCloud; playlist: ссылка/ID;
 * likes/recs: личный аккаунт; similar: ID текущего трека.
 * Очередь ограничена 256 треками; выбираются только полные перекодировки.
 */
int sc_queue(const ScConfig*,const char *kind,const char *query,const ScTrack *current,
             ScQueue *out,char *error,size_t cap);
int sc_stream(const ScConfig*,const ScTrack*,char *out,size_t cap,char *error,size_t errcap);
int sc_like(const ScConfig*,const ScTrack*,int enabled,char *error,size_t cap);
int sc_check_login(const ScConfig*,char *error,size_t cap);
/* secure.soundcloud.com/oauth/token; секрет приложения, если необходим, из
 * SOUNDYA_SC_CLIENT_SECRET. Обмен одноразовый, без автоматических повторов.
 * Буферы очищаются при ошибке; expires — expires_in в секундах.
 */
int sc_refresh(const ScConfig*,const char *refresh,char *at,size_t atcap,
               char *rt,size_t rtcap,long *expires,char *error,size_t cap);
/* Декодер без сети: объект трека, массив, collection/track, tracks плейлиста. */
int sc_decode_queue(const char *json,ScQueue *out,char *error,size_t cap);
#endif
