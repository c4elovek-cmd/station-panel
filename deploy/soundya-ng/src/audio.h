#ifndef PANEL_AUDIO_H
#define PANEL_AUDIO_H
#include <stdatomic.h>
#include <sys/types.h>
#include "sc.h"
/* Счётчик реально переданного PCM; процесс декодера не управляет статусом панели. */
typedef struct { _Atomic long long bytes; _Atomic int stage; _Atomic int volume; char error[256]; } AudioShared;
typedef struct { pid_t pid; AudioShared *shared; } Audio;
int audio_init(Audio *a);
int audio_start(Audio *a,const ScConfig *cfg,const ScTrack *track,const char *device,long seek_ms);
void audio_pause(Audio *a,int paused);
void audio_stop(Audio *a);
void audio_close(Audio *a);
int audio_poll(Audio *a);
long audio_position(const Audio *a);
#endif
