#define _GNU_SOURCE
#define _POSIX_C_SOURCE 200809L
#define _DEFAULT_SOURCE
#include "audio.h"
#include "json.h"
#include "sc.h"
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define VERSION "0.1.0"
#define CLIENTS 16
#define REQUEST_MAX 16384
#define RESPONSE_MAX 1048576
static volatile sig_atomic_t shutdown_requested=0;
static void terminate(int sig){(void)sig;shutdown_requested=1;}
static const char *env(const char *key,const char *fallback){const char *v=getenv(key);return v&&*v?v:fallback;}
static long long monotime(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return (long long)t.tv_sec*1000+t.tv_nsec/1000000;}
static void copy(char *dst,size_t cap,const char *src){snprintf(dst,cap,"%s",src?src:"");}
static void note(const char *text){fprintf(stderr,"panel-player: %s\n",text);}
static int writeall(int fd,const void *data,size_t size){const char *p=data;while(size){ssize_t n=write(fd,p,size);if(n<0&&errno==EINTR)continue;if(n<=0)return -1;p+=n;size-=(size_t)n;}return 0;}

typedef struct { char token[2048],refresh[1024];long long expires,retry_after;char login[32]; } Account;
typedef struct {
    ScConfig sc;Account accounts[2];int active;char dir[512],device[128];
    ScQueue queue;size_t index;Audio audio;char state[32],error[256];int repeat,shuffle,paused;
    pid_t job;int jobfd;size_t jobread;long long jobstart,next_login[2];int jobaccount,jobkind;unsigned revisions[2],jobrevision;
} Player;
typedef struct { int kind,account,ok;ScQueue queue;char at[2048],rt[1024],error[256];long expires; } JobResult;
static Player player;static JobResult jobresult;

typedef struct {char *data;size_t used,cap;int bad;} Buffer;
static void add(Buffer *b,const char *fmt,...){if(b->bad)return;va_list ap;va_start(ap,fmt);int n=vsnprintf(b->data+b->used,b->cap-b->used,fmt,ap);va_end(ap);if(n<0||(size_t)n>=b->cap-b->used){b->bad=1;return;}b->used+=(size_t)n;}
static void quoted(Buffer *b,const char *s){if(b->bad)return;int n=j_quote(s,b->data+b->used,b->cap-b->used);if(n<0){b->bad=1;return;}b->used+=(size_t)n;}
static void track_json(Buffer *b,const ScTrack *t){add(b,"{\"id\":");quoted(b,t->id);add(b,",\"title\":");quoted(b,t->title);add(b,",\"artist\":");quoted(b,t->artist);add(b,",\"artwork_url\":");quoted(b,t->artwork);add(b,",\"permalink_url\":");quoted(b,t->permalink);add(b,",\"duration_ms\":%ld}",t->duration_ms);}
static void current_state(void){if(!player.audio.pid)return;int stage=atomic_load(&player.audio.shared->stage);copy(player.state,sizeof(player.state),player.paused?"paused":stage==2?"playing":stage<0?"error":"loading");}
static void status_json(Buffer *b){
    current_state();Account *a=&player.accounts[player.active];
    add(b,"{\"backend\":\"panel-player\",\"version\":\"" VERSION "\",\"state\":");quoted(b,player.state);
    add(b,",\"track\":");if(player.queue.count){char text[800];snprintf(text,sizeof(text),"%s — %s",player.queue.tracks[player.index].artist,player.queue.tracks[player.index].title);quoted(b,text);}else add(b,"null");
    add(b,",\"track_info\":");if(player.queue.count)track_json(b,&player.queue.tracks[player.index]);else add(b,"null");
    add(b,",\"position_ms\":%ld,\"duration_ms\":%ld,\"volume\":%d,\"login\":",audio_position(&player.audio),player.queue.count?player.queue.tracks[player.index].duration_ms:0,atomic_load(&player.audio.shared->volume));quoted(b,a->login);
    add(b,",\"active_account\":\"%s\",\"queue_index\":%zu,\"queue_len\":%zu,\"shuffle\":%s,\"repeat\":%s,\"pending\":%s,\"error\":",player.active?"other":"me",player.index,player.queue.count,player.shuffle?"true":"false",player.repeat?"true":"false",player.job?"true":"false");quoted(b,player.error);
    add(b,",\"queue\":[");for(size_t i=0;i<player.queue.count;i++){if(i)add(b,",");track_json(b,&player.queue.tracks[i]);}add(b,"],\"accounts\":{");
    for(int i=0;i<2;i++){if(i)add(b,",");add(b,"\"%s\":{\"login\":",i?"other":"me");quoted(b,player.accounts[i].login);add(b,",\"has_token\":%s,\"has_refresh\":%s,\"expires\":%lld}",player.accounts[i].token[0]?"true":"false",player.accounts[i].refresh[0]?"true":"false",player.accounts[i].expires);}add(b,"}}");
}
static int path(char *out,size_t cap,const char *name){int n=snprintf(out,cap,"%s/%s",player.dir,name);return n<0||(size_t)n>=cap?-1:0;}
static int atomic_file(const char *name,const char *text){
    char dest[1024],tmp[1100];if(path(dest,sizeof(dest),name))return -1;snprintf(tmp,sizeof(tmp),"%s.tmp.%ld",dest,(long)getpid());
    int fd=open(tmp,O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC,0600);if(fd<0)return -1;
    int rc=writeall(fd,text,strlen(text));if(!rc)rc=fsync(fd);if(close(fd)&&!rc)rc=-1;
    if(!rc)rc=rename(tmp,dest);if(rc)unlink(tmp);else{int d=open(player.dir,O_RDONLY|O_DIRECTORY);if(d>=0){fsync(d);close(d);}}return rc;
}
static int read_file(const char *name,char *out,size_t cap){char p[1024];if(path(p,sizeof(p),name))return -1;int fd=open(p,O_RDONLY|O_CLOEXEC|O_NOFOLLOW);if(fd<0)return -1;ssize_t n=read(fd,out,cap-1);char c;int extra=n>=0?read(fd,&c,1):0;close(fd);if(n<0||extra>0)return -1;out[n]=0;while(n>0&&(out[n-1]=='\n'||out[n-1]=='\r'))out[--n]=0;return 0;}
static int save(void){
    char raw[16384];Buffer b={raw,0,sizeof(raw),0};
    add(&b,"{\"version\":1,\"active\":%d,\"proxy\":",player.active);quoted(&b,player.sc.proxy);add(&b,",\"client_id\":");quoted(&b,player.sc.client_id);add(&b,",\"device\":");quoted(&b,player.device);add(&b,",\"volume\":%d,\"accounts\":[",atomic_load(&player.audio.shared->volume));
    for(int i=0;i<2;i++){if(i)add(&b,",");add(&b,"{\"token\":");quoted(&b,player.accounts[i].token);add(&b,",\"refresh\":");quoted(&b,player.accounts[i].refresh);add(&b,",\"expires\":%lld}",player.accounts[i].expires);}add(&b,"]}\n");
    if(b.bad||atomic_file("panel-player.json",raw))return -1;
    /* Каноническая конфигурация уже сохранена. Ошибка зеркала не откатывает токены. */
    if(atomic_file("active_account",player.active?"other":"me"))note("Не записалось совместимое зеркало active_account");
    return 0;
}
static void getstr(JDoc *d,int obj,const char *key,char *out,size_t cap){int t=j_find(d,obj,key);if(t>=0)j_string(d,t,out,cap);}
static int load(void){
    copy(player.dir,sizeof(player.dir),env("PANEL_PLAYER_DIR","/data/opt/panel-player"));copy(player.device,sizeof(player.device),env("PANEL_PLAYER_DEVICE","default"));copy(player.sc.curl,sizeof(player.sc.curl),env("PANEL_CURL","/data/opt/entware/bin/curl"));copy(player.state,sizeof(player.state),"stopped");
    for(int i=0;i<2;i++)copy(player.accounts[i].login,sizeof(player.accounts[i].login),"missing");
    char raw[16384];JDoc *d=calloc(1,sizeof(*d));if(!d)return -1;
    if(!read_file("panel-player.json",raw,sizeof(raw))){
        if(j_parse(d,raw)<0){free(d);note("Повреждён panel-player.json; исходный файл не перезаписан");return -1;}
        getstr(d,0,"proxy",player.sc.proxy,sizeof(player.sc.proxy));getstr(d,0,"client_id",player.sc.client_id,sizeof(player.sc.client_id));getstr(d,0,"device",player.device,sizeof(player.device));player.active=j_int(d,j_find(d,0,"active"),0)==1;
        int volume=(int)j_int(d,j_find(d,0,"volume"),100);atomic_store(&player.audio.shared->volume,volume>=0&&volume<=100?volume:100);
        int acc=j_find(d,0,"accounts");for(int i=0;i<2;i++){int obj=j_at(d,acc,i);getstr(d,obj,"token",player.accounts[i].token,sizeof(player.accounts[i].token));getstr(d,obj,"refresh",player.accounts[i].refresh,sizeof(player.accounts[i].refresh));player.accounts[i].expires=j_int(d,j_find(d,obj,"expires"),0);}
    }else if(!strcmp(env("PANEL_IMPORT_LEGACY","0"),"1")){
        /* Миграция данных разрешена отдельно; исходный config.json не меняется. */
        if(!read_file("config.json",raw,sizeof(raw))&&j_parse(d,raw)>=0){getstr(d,0,"proxy",player.sc.proxy,sizeof(player.sc.proxy));getstr(d,0,"token",player.accounts[0].token,sizeof(player.accounts[0].token));}
        char active[32];if(!read_file("active_account",active,sizeof(active))&&!strcmp(active,"other"))player.active=1;
        read_file("client_id",player.sc.client_id,sizeof(player.sc.client_id));
        for(int i=0;i<2;i++){read_file(i?"access_token_other":"access_token_me",player.accounts[i].token,sizeof(player.accounts[i].token));read_file(i?"refresh_token_other":"refresh_token",player.accounts[i].refresh,sizeof(player.accounts[i].refresh));}
    }
    free(d);for(int i=0;i<2;i++)if(player.accounts[i].token[0])copy(player.accounts[i].login,sizeof(player.accounts[i].login),"unchecked");return 0;
}
static void sync_config(void){copy(player.sc.token,sizeof(player.sc.token),player.accounts[player.active].token);}
static void cancel_job(void){if(player.job>0){kill(-player.job,SIGTERM);kill(-player.job,SIGKILL);while(waitpid(player.job,NULL,0)<0&&errno==EINTR){}}if(player.jobfd>=0)close(player.jobfd);player.job=0;player.jobfd=-1;player.jobread=0;}
/* Сетевые задачи отдельными процессами: пауза/стоп/статус никогда не ждут SoundCloud. */
static int start_job(int kind,const char *what,const char *query,int account){
    if(player.job)return -1;int fds[2];if(pipe(fds))return -1;sync_config();ScConfig cfg=player.sc;copy(cfg.token,sizeof(cfg.token),player.accounts[account].token);
    if(kind==2&&player.jobrevision!=player.revisions[account])return 0;
    player.job=fork();
    if(!player.job){
        setpgid(0,0);signal(SIGTERM,SIG_DFL);signal(SIGINT,SIG_DFL);close(fds[0]);for(int fd=3;fd<256;fd++)if(fd!=fds[1])close(fd);
        JobResult *r=calloc(1,sizeof(*r));if(!r)_exit(1);r->kind=kind;r->account=account;
        if(kind==1)r->ok=sc_queue(&cfg,what,query,player.queue.count?&player.queue.tracks[player.index]:NULL,&r->queue,r->error,sizeof(r->error))==0;
        else {
            int ok=r->ok=cfg.token[0]&&sc_check_login(&cfg,r->error,sizeof(r->error))==0;
            /* Ротация нужна при неподтверждённом входе, неизвестном сроке или за
             * 15 минут до истечения: иначе неудачный refresh перетирал бы
             * успешную проверку. Отозванный refresh не трогаем час. */
            long long exp=player.accounts[account].expires;
            int expiring=exp<=0||exp<((long long)time(NULL)+900);
            if((!ok||expiring)&&player.accounts[account].refresh[0]&&monotime()>=player.accounts[account].retry_after){
                char login_err[256];copy(login_err,sizeof(login_err),r->error);
                int rotated=0;
                /* Прокси периодически отваливается целиком: если пару токенов
                 * получить не удалось, второй раз идём напрямую, мимо прокси. */
                for(int attempt=0;attempt<2&&!rotated;attempt++){
                    ScConfig net=cfg;if(attempt)net.proxy[0]=0;
                    rotated=sc_refresh(&net,player.accounts[account].refresh,r->at,sizeof(r->at),r->rt,sizeof(r->rt),&r->expires,r->error,sizeof(r->error))==0;
                    if(rotated)break;
                    /* Отозванный токен и отказ SoundCloud повтором не лечатся. */
                    if(strstr(r->error,"invalid_grant")||strstr(r->error,"HTTP 401")||strstr(r->error,"HTTP 403"))break;
                }
                if(rotated){copy(cfg.token,sizeof(cfg.token),r->at);r->ok=sc_check_login(&cfg,r->error,sizeof(r->error))==0;}
                else if(ok){/* токен ещё жив: ротацию отложили, вход подтверждён */
                    r->ok=1;char msg[360];
                    if(strstr(r->error,"invalid_grant"))snprintf(msg,sizeof(msg),"SoundCloud: refresh-токен %s отозван, нужен новый вход; текущий вход пока рабочий",account?"other":"me");
                    else snprintf(msg,sizeof(msg),"refresh SoundCloud не удался, токен пока рабочий: %s",r->error[0]?r->error:"без описания");
                    note(msg);
                }else copy(r->error,sizeof(r->error),login_err);
            }
        }
        int rc=writeall(fds[1],r,sizeof(*r));close(fds[1]);free(r);_exit(rc?1:0);
    }
    close(fds[1]);if(player.job<0){close(fds[0]);player.job=0;return -1;}setpgid(player.job,player.job);player.jobfd=fds[0];fcntl(player.jobfd,F_SETFL,O_NONBLOCK);player.jobread=0;player.jobaccount=account;player.jobkind=kind;player.jobrevision=player.revisions[account];player.jobstart=monotime();return 0;
}
static void play_index(void){player.paused=0;sync_config();player.error[0]=0;if(!player.queue.count){copy(player.state,sizeof(player.state),"stopped");return;}if(audio_start(&player.audio,&player.sc,&player.queue.tracks[player.index],player.device,0)){copy(player.error,sizeof(player.error),"Не удалось запустить аудиопроцесс");copy(player.state,sizeof(player.state),"error");}else copy(player.state,sizeof(player.state),"loading");}
static void collect_job(void){
    if(!player.job)return;
    ssize_t n=read(player.jobfd,(char*)&jobresult+player.jobread,sizeof(jobresult)-player.jobread);
    if(n>0)player.jobread+=(size_t)n;
    if(player.jobread==sizeof(jobresult)){
        /* Уже прочитан весь pipe: wait не может зависнуть на забитом stdout. */
        int st;while(waitpid(player.job,&st,0)<0&&errno==EINTR){}kill(-player.job,SIGKILL);close(player.jobfd);player.job=0;player.jobfd=-1;
        JobResult *r=&jobresult;Account *a=&player.accounts[r->account];
        if(r->kind==1){if(r->ok&&r->queue.count){player.queue=r->queue;player.index=0;play_index();}else{copy(player.error,sizeof(player.error),r->error[0]?r->error:"Пустая очередь");if(!player.audio.pid)copy(player.state,sizeof(player.state),"error");}}
        else {
            if(r->ok)copy(a->login,sizeof(a->login),"ok");
            else if(r->error[0]&&(strstr(r->error,"401")||strstr(r->error,"403")||strstr(r->error,"invalid_grant")))copy(a->login,sizeof(a->login),"refused");
            else copy(a->login,sizeof(a->login),"unavailable");
            if(!r->ok){char msg[320];snprintf(msg,sizeof(msg),"проверка входа %s: %s",r->account?"other":"me",r->error[0]?r->error:"без описания");note(msg);}
            /* Отозванный refresh-токен не повторяем час: это не лечится запросами. */
            if(strstr(r->error,"invalid_grant"))a->retry_after=monotime()+3600000;
            /* Ротированную пару сохраняем даже при неудачной повторной проверке:
             * refresh-токен на стороне SoundCloud уже заменён, старый не вернуть. */
            if(r->at[0]&&strcmp(a->token,r->at)){
                copy(a->token,sizeof(a->token),r->at);if(r->rt[0])copy(a->refresh,sizeof(a->refresh),r->rt);a->expires=(long long)time(NULL)+(r->expires>0?r->expires:3600);a->retry_after=0;
                if(save())note("Не удалось сохранить ротированную пару токенов");
                else{char msg[160];snprintf(msg,sizeof(msg),"SoundCloud: токены %s обновлены, действуют %ld мин",r->account?"other":"me",(long)(r->expires/60));note(msg);}
            }
            sync_config();
            /* Подтверждённый вход проверяем раз в 10 минут, проблемный — каждую
             * минуту: лишние запросы SoundCloud считает подозрительной
             * активностью и может отозвать сессию. */
            player.next_login[r->account]=monotime()+(r->ok?600000:60000);
        }
    }else if(n==0||monotime()-player.jobstart>120000){cancel_job();copy(player.error,sizeof(player.error),"Сетевая задача завершилась без результата");}
}
static int patch(const char *raw,char *err,size_t cap){
    JDoc *d=calloc(1,sizeof(*d));if(!d)return -1;if(j_parse(d,raw)<0||d->tokens[0].kind!=J_OBJECT){free(d);copy(err,cap,"Некорректный JSON");return -1;}
    Account old[2];memcpy(old,player.accounts,sizeof(old));ScConfig oldcfg=player.sc;int oldactive=player.active;int volume=atomic_load(&player.audio.shared->volume);
    int t=j_find(d,0,"account");if(t>=0){char who[32];if(j_string(d,t,who,sizeof(who))<0||(strcmp(who,"me")&&strcmp(who,"other"))){free(d);copy(err,cap,"account: me или other");return -1;}player.active=!strcmp(who,"other");}
    int account=player.active;t=j_find(d,0,"save_account");if(t>=0){char who[32];int n=j_string(d,t,who,sizeof(who));if(n<0||(size_t)n!=strlen(who)||(strcmp(who,"me")&&strcmp(who,"other"))){player.active=oldactive;free(d);copy(err,cap,"save_account: me или other");return -1;}account=!strcmp(who,"other");}
    const char *keys[]={"token","sc_refresh","proxy","client_id"};char *dest[]={player.accounts[account].token,player.accounts[account].refresh,player.sc.proxy,player.sc.client_id};size_t sizes[]={2048,1024,512,128};
    int bad=0;for(int i=0;i<4;i++){t=j_find(d,0,keys[i]);if(t>=0){int n=j_string(d,t,dest[i],sizes[i]);if(n<0||(size_t)n!=strlen(dest[i]))bad=1;}}
    if(strpbrk(player.accounts[account].token,"\r\n ")||strpbrk(player.accounts[account].refresh,"\r\n ")||strpbrk(player.sc.proxy,"\r\n"))bad=1;
    if(player.sc.proxy[0]&&strncmp(player.sc.proxy,"socks5://",9)&&strncmp(player.sc.proxy,"socks5h://",10)&&strncmp(player.sc.proxy,"http://",7)&&strncmp(player.sc.proxy,"https://",8))bad=1;
    t=j_find(d,0,"volume");long vol=j_int(d,t,t>=0?-1:volume);if(vol<0||vol>100)bad=1;else atomic_store(&player.audio.shared->volume,(int)vol);
    free(d);
    if(bad||save()){memcpy(player.accounts,old,sizeof(old));player.sc=oldcfg;player.active=oldactive;atomic_store(&player.audio.shared->volume,volume);copy(err,cap,bad?"Некорректное значение настройки":"Не удалось сохранить настройки");return -1;}
    player.revisions[account]++;player.jobrevision=0;
    if(player.job&&player.jobkind==2&&player.jobaccount==account)cancel_job();
    copy(player.accounts[account].login,sizeof(player.accounts[account].login),player.accounts[account].token[0]?"unchecked":"missing");sync_config();player.next_login[account]=monotime();return 0;
}
static void command(const char *body,Buffer *b){
    char input[8192];if(strlen(body)>=sizeof(input)){add(b,"error: слишком длинная команда");return;}copy(input,sizeof(input),body);
    char *q=input;while(*q==' ')q++;char *arg=strchr(q,' ');if(arg){*arg++=0;}else arg=q+strlen(q);
    while(*arg==' ')arg++;
    if(!strcmp(q,"status")){current_state();add(b,"%s",player.state);if(player.queue.count)add(b,": %s — %s [%ld:%02ld] (%zu/%zu)",player.queue.tracks[player.index].artist,player.queue.tracks[player.index].title,audio_position(&player.audio)/60000,audio_position(&player.audio)/1000%60,player.index+1,player.queue.count);return;}
    if(!strcmp(q,"json")){status_json(b);return;}
    if(!strcmp(q,"queue")){add(b,"[");for(size_t i=0;i<player.queue.count;i++){if(i)add(b,",");track_json(b,&player.queue.tracks[i]);}add(b,"]");return;}
    if(!strcmp(q,"pause")){player.paused=1;audio_pause(&player.audio,1);if(player.audio.pid)copy(player.state,sizeof(player.state),"paused");add(b,"OK");return;}
    if(!strcmp(q,"stop")){cancel_job();audio_stop(&player.audio);player.paused=0;copy(player.state,sizeof(player.state),"stopped");add(b,"OK");return;}
    if(!strcmp(q,"resume")||(!strcmp(q,"play")&&!*arg)||!strcmp(q,"toggle")){
        if(!strcmp(q,"toggle")&&player.audio.pid&&!player.paused){player.paused=1;audio_pause(&player.audio,1);copy(player.state,sizeof(player.state),"paused");}
        else if(player.audio.pid){player.paused=0;audio_pause(&player.audio,0);current_state();}else if(player.queue.count)play_index();else{add(b,"error: очередь пуста");return;}add(b,"OK");return;
    }
    if(!strcmp(q,"next")||!strcmp(q,"prev")){if(!player.queue.count){add(b,"error: очередь пуста");return;}cancel_job();player.index=!strcmp(q,"next")?(player.index+1)%player.queue.count:(player.index+player.queue.count-1)%player.queue.count;play_index();add(b,"OK");return;}
    if(!strcmp(q,"index")){
        char *end;errno=0;long n=strtol(arg,&end,10);
        if(!*arg||*end||errno||n<0||(size_t)n>=player.queue.count){add(b,"error: index — номер трека очереди");return;}
        cancel_job();player.index=(size_t)n;play_index();add(b,"OK index %ld",n);return;
    }
    if(!strcmp(q,"shuffle")){if(*arg&&strcmp(arg,"on")&&strcmp(arg,"off")){add(b,"error: shuffle on|off");return;}player.shuffle=*arg?!strcmp(arg,"on"):!player.shuffle;add(b,"OK shuffle %s",player.shuffle?"on":"off");return;}
    if(!strcmp(q,"repeat")){if(*arg&&strcmp(arg,"on")&&strcmp(arg,"off")){add(b,"error: repeat on|off");return;}player.repeat=*arg?!strcmp(arg,"on"):!player.repeat;add(b,"OK repeat %s",player.repeat?"on":"off");return;}
    if(!strcmp(q,"seek")){
        char *end;errno=0;double seconds=strtod(arg,&end);if(!*arg||*end||errno||seconds>86400||seconds< -86400||seconds!=seconds||!player.queue.count){add(b,"error: seek — число секунд");return;}
        long ms=(long)(seconds*1000);if(*arg=='+'||*arg=='-')ms+=audio_position(&player.audio);if(ms<0)ms=0;long duration=player.queue.tracks[player.index].duration_ms;if(duration>0&&ms>=duration)ms=duration-1;
        int paused=player.paused;sync_config();audio_start(&player.audio,&player.sc,&player.queue.tracks[player.index],player.device,ms);if(paused)audio_pause(&player.audio,1);add(b,"OK");return;
    }
    if(!strcmp(q,"account")){
        char *mode=strchr(arg,' ');if(mode)*mode++=0;
        if(strcmp(arg,"me")&&strcmp(arg,"other")){add(b,"error: account me|other [likes|recs]");return;}
        if(player.job){add(b,"error: выполняется сетевая задача");return;}
        int prev=player.active;player.active=!strcmp(arg,"other");sync_config();if(save()){player.active=prev;sync_config();add(b,"error: не удалось сохранить аккаунт");return;}
        if(mode&&(!strcmp(mode,"likes")||!strcmp(mode,"recs"))){if(start_job(1,mode,"",player.active)){add(b,"error: не удалось создать задачу");return;}}add(b,"OK account %s",arg);return;
    }
    if(!strcmp(q,"play")||!strcmp(q,"likes")||!strcmp(q,"recs")||!strcmp(q,"similar")||!strcmp(q,"playlist")){
        if((!strcmp(q,"play")||!strcmp(q,"playlist"))&&!*arg){add(b,"error: нужен запрос");return;}
        if(player.job){add(b,"error: выполняется сетевая задача");return;}
        if(start_job(1,q,arg,player.active)){add(b,"error: не удалось запустить запрос");return;}add(b,"OK queued");return;
    }
    if(!strcmp(q,"update")){add(b,"error: плеер обновляется только вместе с нашей панелью; внешний автоапдейтер отключён");return;}
    add(b,"error: неизвестная или ещё не реализованная команда");
}

typedef struct {int fd;char input[REQUEST_MAX];size_t used;char *output;size_t length,sent;long long deadline;} Client;
static void close_client(Client *c){if(c->fd>=0)close(c->fd);free(c->output);memset(c,0,sizeof(*c));c->fd=-1;}
static void respond(Client *c,int code,const char *type,const char *text){
    size_t n=strlen(text);c->output=malloc(n+512);if(!c->output){close_client(c);return;}
    int head=snprintf(c->output,n+512,"HTTP/1.1 %d %s\r\nContent-Type: %s; charset=utf-8\r\nContent-Length: %zu\r\nConnection: close\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n\r\n",code,code==200?"OK":"Error",type,n);
    memcpy(c->output+head,text,n);c->length=(size_t)head+n;c->deadline=monotime()+5000;
}
/* Этот порт доступен панели по доверенному туннелю. По умолчанию — только localhost. */
static void route(Client *c,const char *method,const char *target,const char *body,int unsafe_origin){
    char *out=calloc(1,RESPONSE_MAX);if(!out){close_client(c);return;}Buffer b={out,0,RESPONSE_MAX,0};const char *type="text/plain";int code=200;
    if(!strcmp(method,"POST")&&unsafe_origin){respond(c,403,type,"error: посторонний Origin");free(out);return;}
    if(!strcmp(method,"GET")&&!strcmp(target,"/api/status")){type="application/json";status_json(&b);}
    else if(!strcmp(method,"POST")&&!strcmp(target,"/api/cmd"))command(body,&b);
    else if(!strcmp(method,"POST")&&!strcmp(target,"/api/settings")){char err[256];if(patch(body,err,sizeof(err))){code=400;add(&b,"error: %s",err);}else add(&b,"OK");}
    else if(!strcmp(method,"GET")&&!strcmp(target,"/health")){type="application/json";add(&b,"{\"ok\":true,\"backend\":\"panel-player\",\"version\":\"" VERSION "\"}");}
    else if(!strcmp(method,"GET")&&!strcmp(target,"/"))add(&b,"Собственный плеер панели. API: /api/status, /api/cmd, /api/settings. Интерфейс — в панели Станции.");
    else{code=404;add(&b,"error: маршрут не найден");}
    if(b.bad){code=500;copy(out,RESPONSE_MAX,"error: ответ слишком большой");}respond(c,code,type,out);free(out);
}
static void request(Client *c){
    char *end=strstr(c->input,"\r\n\r\n");if(!end)return;size_t header=(size_t)(end-c->input)+4;
    char method[16],target[256],version[16];if(sscanf(c->input,"%15s %255s %15s",method,target,version)!=3||strcmp(version,"HTTP/1.1")){respond(c,400,"text/plain","error: неверная строка HTTP");return;}
    size_t length=0;int seen=0,unsafe=0;char *line=strstr(c->input,"\r\n");
    while(line&&line+2<end){line+=2;char *next=strstr(line,"\r\n");if(!next)break;
        if(!strncasecmp(line,"Content-Length:",15)){char *tail;errno=0;unsigned long n=strtoul(line+15,&tail,10);while(tail<next&&*tail==' ')tail++;if(seen++||errno||tail!=next||n>8192){respond(c,400,"text/plain","error: неверная длина тела");return;}length=n;}
        if(!strncasecmp(line,"Transfer-Encoding:",18)){respond(c,400,"text/plain","error: chunked не поддерживается");return;}
        if(!strncasecmp(line,"Origin:",7)){
            char origin[512];size_t n=(size_t)(next-line-7);if(n>=sizeof(origin)){unsafe=1;}else{memcpy(origin,line+7,n);origin[n]=0;char *o=origin;while(*o==' ')o++;
                const char *allowed=env("PANEL_ALLOWED_ORIGIN","");unsafe=strcmp(o,allowed)!=0;}
        }line=next;
    }
    if(header+length>=REQUEST_MAX){respond(c,413,"text/plain","error: запрос слишком большой");return;}
    if(c->used<header+length)return;if(c->used!=header+length){respond(c,400,"text/plain","error: лишние данные в запросе");return;}
    c->input[header+length]=0;route(c,method,target,c->input+header,unsafe);
}
static int serve(void){
    int lockfd;char lockpath[1024];path(lockpath,sizeof(lockpath),"player.lock");lockfd=open(lockpath,O_RDWR|O_CREAT|O_CLOEXEC,0600);if(lockfd<0||flock(lockfd,LOCK_EX|LOCK_NB)){note("Другой экземпляр уже работает либо папка недоступна");return 1;}
    int s=socket(AF_INET,SOCK_STREAM|SOCK_CLOEXEC|SOCK_NONBLOCK,0);if(s<0)return 1;int yes=1;setsockopt(s,SOL_SOCKET,SO_REUSEADDR,&yes,sizeof(yes));
    struct sockaddr_in addr={0};addr.sin_family=AF_INET;
    char *end;long port=strtol(env("PANEL_PLAYER_PORT","8765"),&end,10);if(*end||port<1||port>65535)return 1;addr.sin_port=htons((uint16_t)port);
    if(inet_pton(AF_INET,env("PANEL_PLAYER_HOST","127.0.0.1"),&addr.sin_addr)!=1||bind(s,(struct sockaddr*)&addr,sizeof(addr))||listen(s,16)){note("Не удалось открыть HTTP-порт");close(s);return 1;}
    Client clients[CLIENTS];memset(clients,0,sizeof(clients));for(int i=0;i<CLIENTS;i++)clients[i].fd=-1;
    note("Собственный плеер запущен; IPC-соединения к Алисе не создаются");player.next_login[0]=player.next_login[1]=monotime()+2000;
    while(!shutdown_requested){
        struct pollfd fds[CLIENTS+1];fds[0]=(struct pollfd){s,POLLIN,0};for(int i=0;i<CLIENTS;i++)fds[i+1]=(struct pollfd){clients[i].fd,clients[i].output?POLLOUT:POLLIN,0};
        if(poll(fds,CLIENTS+1,50)<0&&errno!=EINTR)break;
        if(fds[0].revents&POLLIN){int fd=accept4(s,NULL,NULL,SOCK_CLOEXEC|SOCK_NONBLOCK);if(fd>=0){int i;for(i=0;i<CLIENTS;i++)if(clients[i].fd<0)break;if(i==CLIENTS)close(fd);else{clients[i].fd=fd;clients[i].deadline=monotime()+5000;}}}
        for(int i=0;i<CLIENTS;i++){
            Client *c=&clients[i];if(c->fd<0)continue;
            if(monotime()>c->deadline||fds[i+1].revents&(POLLERR|POLLHUP|POLLNVAL)){close_client(c);continue;}
            if(!c->output&&(fds[i+1].revents&POLLIN)){ssize_t n=read(c->fd,c->input+c->used,sizeof(c->input)-c->used-1);if(n<=0){if(n==0||errno!=EAGAIN)close_client(c);continue;}c->used+=(size_t)n;c->input[c->used]=0;if(c->used>=REQUEST_MAX-1)respond(c,413,"text/plain","error: слишком большой запрос");else request(c);}
            if(c->output&&(fds[i+1].revents&POLLOUT)){ssize_t n=write(c->fd,c->output+c->sent,c->length-c->sent);if(n>0)c->sent+=(size_t)n;else if(errno!=EAGAIN){close_client(c);continue;}if(c->sent==c->length)close_client(c);}
        }
        collect_job();int done=audio_poll(&player.audio);
        if(done<0){
            copy(player.error,sizeof(player.error),player.audio.shared->error);
            /* Мёртвый источник — не стоп для очереди: переходим к следующему
             * треку, пока очередь не кончится. Ошибка остаётся в статусе. */
            if(player.index+1<player.queue.count){
                player.index=(player.index+1)%player.queue.count;play_index();
            }else copy(player.state,sizeof(player.state),"error");
        }
        else if(done>0){if(player.repeat)play_index();else if(player.index+1<player.queue.count){player.index=player.shuffle?(size_t)rand()%player.queue.count:player.index+1;play_index();}else copy(player.state,sizeof(player.state),"stopped");}
        if(!player.job){
            /* У каждого аккаунта свой срок следующей проверки: иначе успешный
             * вход одного откладывает проверку второго на все 10 минут. */
            for(int i=0;i<2;i++){
                Account *a=&player.accounts[i];
                if(monotime()<player.next_login[i]||(!a->token[0]&&!a->refresh[0]))continue;
                start_job(2,"","",i);if(player.job)break;
            }
        }
    }
    for(int i=0;i<CLIENTS;i++)close_client(&clients[i]);close(s);cancel_job();audio_close(&player.audio);close(lockfd);return 0;
}
int main(int argc,char **argv){
    signal(SIGINT,terminate);signal(SIGTERM,terminate);signal(SIGPIPE,SIG_IGN);player.jobfd=-1;
    if(argc==2&&!strcmp(argv[1],"--version")){puts("panel-player " VERSION);return 0;}
    if(audio_init(&player.audio)||load())return 1;
    if(argc>=2&&!strcmp(argv[1],"--status-config")){char raw[4096];Buffer b={raw,0,sizeof(raw),0};add(&b,"{\"active_account\":\"%s\",\"accounts\":[",player.active?"other":"me");for(int i=0;i<2;i++){if(i)add(&b,",");add(&b,"{\"has_token\":%s,\"has_refresh\":%s}",player.accounts[i].token[0]?"true":"false",player.accounts[i].refresh[0]?"true":"false");}add(&b,"]}");puts(raw);audio_close(&player.audio);return 0;}
    return serve();
}
