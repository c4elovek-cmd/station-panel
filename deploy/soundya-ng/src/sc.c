#define _POSIX_C_SOURCE 200809L
#include "sc.h"
#include "json.h"
#include <ctype.h>
#include <limits.h>
#ifndef SC_FIXTURE_ONLY
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#endif
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifndef SC_FIXTURE_ONLY
#include <sys/socket.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#endif

#define API "https://api-v2.soundcloud.com"
#define BODY_MAX (4U*1024U*1024U)
#define URL_MAX 8192U

static int err(char *out,size_t cap,const char *s){if(out&&cap)snprintf(out,cap,"%s",s);return -1;}
static void clear(char *out,size_t cap){if(out&&cap)out[0]=0;}
static int text(const char *s){if(!s)return 0;for(;*s;s++)if((unsigned char)*s<32||(unsigned char)*s==127)return 0;return 1;}
#ifndef SC_FIXTURE_ONLY
static int terminated(const char *s,size_t cap){return s&&memchr(s,0,cap)!=NULL;}
static int config(const ScConfig *c,char *e,size_t n){
    if(!c||!terminated(c->curl,sizeof(c->curl))||!terminated(c->proxy,sizeof(c->proxy))||
       !terminated(c->client_id,sizeof(c->client_id))||!terminated(c->token,sizeof(c->token))||
       !text(c->curl)||!text(c->proxy)||!text(c->client_id)||!text(c->token))
        return err(e,n,"Некорректная конфигурация SoundCloud.");
    return 0;
}
static int login_required(const ScConfig *c,char *e,size_t n){return c->token[0]?0:err(e,n,"Нужен вход в SoundCloud.");}
#endif
static int host(const char *u,const char *h,int sub){
    const char *p;size_t n,l=strlen(h);
    if(!u||strncmp(u,"https://",8)||!text(u)||strchr(u,'\\')||strchr(u,'#'))return 0;
    p=u+8;n=strcspn(p,"/?");
    for(size_t i=0;i<n;i++)if(!((p[i]>='a'&&p[i]<='z')||(p[i]>='0'&&p[i]<='9')||p[i]=='-'||p[i]=='.'))return 0;
    if(n==l&&!memcmp(p,h,l))return 1;
    return sub&&n>l+1&&p[n-l-1]=='.'&&!memcmp(p+n-l,h,l);
}
#ifndef SC_FIXTURE_ONLY
static int enc(const char *s,char *out,size_t cap){
    static const char hx[]="0123456789ABCDEF";size_t n=0;
    if(!s||!cap)return -1;
    for(;*s;s++){unsigned char c=(unsigned char)*s;
        if((c>='a'&&c<='z')||(c>='A'&&c<='Z')||(c>='0'&&c<='9')||strchr("-_.~",c)){
            if(n+1>=cap)return -1;out[n++]=(char)c;
        }else{if(n+3>=cap)return -1;out[n++]='%';out[n++]=hx[c>>4];out[n++]=hx[c&15];}}
    out[n]=0;return 0;
}
static int add(char *d,size_t cap,const char *s){size_t a=strlen(d),b=strlen(s);if(a>=cap||b>=cap-a)return -1;memcpy(d+a,s,b+1);return 0;}
static int option(char *d,size_t cap,const char *key,const char *value){
    if(!text(value)||add(d,cap,key)||add(d,cap," = \""))return -1;
    for(;*value;value++){char b[3]={*value,0,0};if(*value=='"'||*value=='\\'){b[0]='\\';b[1]=*value;}if(add(d,cap,b))return -1;}
    return add(d,cap,"\"\n");
}
static long long clock_ms(void){struct timespec t;if(clock_gettime(CLOCK_MONOTONIC,&t))return -1;return (long long)t.tv_sec*1000+t.tv_nsec/1000000;}
static int nb(int fd){int f=fcntl(fd,F_GETFL,0);return f<0?-1:fcntl(fd,F_SETFL,f|O_NONBLOCK);}
static void reap(pid_t p,int *status){while(waitpid(p,status,0)<0&&errno==EINTR){}}
static int status_error(int code,char *e,size_t cap){
    switch(code){
    case 401:err(e,cap,"Вход SoundCloud истёк или токен недействителен.");break;
    case 403:err(e,cap,"SoundCloud запретил доступ к ресурсу.");break;
    case 404:err(e,cap,"Ресурс SoundCloud не найден.");break;
    case 429:err(e,cap,"Лимит SoundCloud: повторите запрос позже.");break;
    default:if(e&&cap)snprintf(e,cap,"Ошибка SoundCloud: HTTP %d.",code);break;
    }return code?-code:-1;
}

/* Секреты только через stdin curl: ни shell, ни секретов в argv/логах.
 * socketpair+MSG_NOSIGNAL исключает SIGPIPE. stdout ограничен; stderr подавлен.
 */
static int request(const ScConfig *c,const char *method,const char *url,const char *form,
                   int auth,char **body,char *e,size_t cap){
    char opts[32768]={0},header[2100];char *buf=NULL;
    int in[2]={-1,-1},out[2]={-1,-1},nullfd=-1,status=0,dead=0,eof=0,bad=0;
    size_t sent=0,used=0,total;pid_t pid;long long start;
    const char *exe=c->curl[0]?c->curl:getenv("SOUNDYA_CURL");
    *body=NULL;if(!exe||!*exe)exe="/data/opt/entware/bin/curl";
    if(exe[0]!='/'||!text(exe))return err(e,cap,"Путь curl должен быть абсолютным.");
    if(option(opts,sizeof(opts),"url",url)||option(opts,sizeof(opts),"proxy",c->proxy)||
       option(opts,sizeof(opts),"noproxy","")||option(opts,sizeof(opts),"header","Accept: application/json"))
        return err(e,cap,"Запрос SoundCloud слишком длинный.");
    if(auth&&c->token[0]){
        snprintf(header,sizeof(header),"Authorization: OAuth %s",c->token);
        if(option(opts,sizeof(opts),"header",header))return err(e,cap,"Запрос SoundCloud слишком длинный.");
    }
    if(form&&(option(opts,sizeof(opts),"header","Content-Type: application/x-www-form-urlencoded")||
              option(opts,sizeof(opts),"data",form)))return err(e,cap,"Запрос авторизации слишком длинный.");
    total=strlen(opts);start=clock_ms();buf=malloc(BODY_MAX+16);
    if(start<0||!buf||socketpair(AF_UNIX,SOCK_STREAM,0,in)||pipe(out)||(nullfd=open("/dev/null",O_RDWR))<0){
        if(in[0]>=0){close(in[0]);close(in[1]);}if(out[0]>=0){close(out[0]);close(out[1]);}
        free(buf);return err(e,cap,"Не удалось подготовить запрос SoundCloud.");
    }
    pid=fork();
    if(pid<0){close(in[0]);close(in[1]);close(out[0]);close(out[1]);close(nullfd);free(buf);return err(e,cap,"Не удалось запустить curl.");}
    if(pid==0){
        if(dup2(in[1],0)<0||dup2(out[1],1)<0||dup2(nullfd,2)<0)_exit(126);
        /* curl не должен удерживать серверные сокеты, lock-файлы и аудио pipe. */
        {long maxfd=sysconf(_SC_OPEN_MAX);if(maxfd<0||maxfd>1048576)maxfd=1048576;
         for(int fd=3;fd<maxfd;fd++)close(fd);}
        execl(exe,exe,"--disable","--config","-","--silent","--show-error","--proto","=https",
              "--connect-timeout","5","--max-time","25","--max-filesize","4194304",
              "--request",method,"--write-out","\n%{http_code}",(char*)NULL);_exit(127);
    }
    close(in[1]);close(out[1]);close(nullfd);
    if(nb(in[0])||nb(out[0]))bad=1;
    while(!bad&&(!dead||!eof)){
        struct pollfd f[2]={{in[0],POLLOUT,0},{eof?-1:out[0],POLLIN,0}};
        long long now=clock_ms();pid_t w;
        if(now<start||now-start>30000){bad=2;break;}
        if(poll(f,2,100)<0&&errno!=EINTR){bad=1;break;}
        if(in[0]>=0&&f[0].revents){
            ssize_t n=send(in[0],opts+sent,total-sent,MSG_NOSIGNAL);
            if(n>0)sent+=(size_t)n;
            else if(n<0&&errno!=EINTR&&errno!=EAGAIN&&errno!=EWOULDBLOCK){close(in[0]);in[0]=-1;}
            if(sent==total&&in[0]>=0){close(in[0]);in[0]=-1;}
        }
        if(!eof&&f[1].revents){for(;;){
            ssize_t n;if(used==BODY_MAX+15){bad=3;break;}
            n=read(out[0],buf+used,BODY_MAX+15-used);
            if(n>0){used+=(size_t)n;continue;}if(!n)eof=1;
            else if(errno!=EINTR&&errno!=EAGAIN&&errno!=EWOULDBLOCK)bad=1;break;
        }}
        if(!dead){w=waitpid(pid,&status,WNOHANG);if(w==pid)dead=1;else if(w<0&&errno!=EINTR){bad=1;break;}}
    }
    if(in[0]>=0)close(in[0]);close(out[0]);memset(opts,0,sizeof(opts));memset(header,0,sizeof(header));
    if(!dead){kill(pid,SIGKILL);reap(pid,&status);}
    if(bad||!WIFEXITED(status)||WEXITSTATUS(status)){
        int x=WIFEXITED(status)?WEXITSTATUS(status):-1;free(buf);
        if(bad==2||x==28)return err(e,cap,"Таймаут запроса SoundCloud.");
        if(bad==3||x==63)return err(e,cap,"Ответ SoundCloud слишком большой.");
        if(x==127)return err(e,cap,"curl не найден или не запускается.");
        return err(e,cap,"Ошибка сети при обращении к SoundCloud.");
    }
    if(used<4||buf[used-4]!='\n'||!isdigit((unsigned char)buf[used-3])||
       !isdigit((unsigned char)buf[used-2])||!isdigit((unsigned char)buf[used-1])||memchr(buf,0,used)){
        free(buf);return err(e,cap,"Некорректный ответ curl.");}
    {int code=(buf[used-3]-'0')*100+(buf[used-2]-'0')*10+buf[used-1]-'0';
     if(code<200||code>=300){free(buf);return status_error(code,e,cap);}}
    buf[used-4]=0;*body=buf;return 0;
}
static int get(const ScConfig *c,const char *u,char **body,char *e,size_t cap){
    char url[URL_MAX],v[384];if(!host(u,"api-v2.soundcloud.com",0))return err(e,cap,"Недопустимый адрес API SoundCloud.");
    if(strlen(u)>=sizeof(url))return err(e,cap,"Адрес API слишком длинный.");strcpy(url,u);
    if(c->client_id[0]&&!strstr(url,"?client_id=")&&!strstr(url,"&client_id=")){
        if(enc(c->client_id,v,sizeof(v))||add(url,sizeof(url),strchr(url,'?')?"&client_id=":"?client_id=")||add(url,sizeof(url),v))
            return err(e,cap,"Адрес API слишком длинный.");}
    return request(c,"GET",url,NULL,1,body,e,cap);
}
#endif
static JDoc *doc_parse(const char *s,char *e,size_t cap){
    JDoc *d=malloc(sizeof(*d));if(!d){err(e,cap,"Недостаточно памяти для ответа SoundCloud.");return NULL;}
    if(j_parse(d,s)<0||d->count<1){free(d);err(e,cap,"Некорректный JSON ответа SoundCloud.");return NULL;}return d;
}
static int str(const JDoc *d,int t,char *out,size_t cap){
    int n;clear(out,cap);if(!out||!cap||t<0||t>=d->count||d->tokens[t].kind!=J_STRING)return -1;
    n=j_string(d,t,out,cap);if(n<0||(size_t)n!=strlen(out)){clear(out,cap);return -1;}return 0;
}
static int field(const JDoc *d,int obj,const char *key,char *out,size_t cap){return str(d,j_find(d,obj,key),out,cap);}
static int id_norm(const char *s,const char *type,char *out,size_t cap){
    char prefix[48];size_t n;if(!s||!text(s))return -1;
    snprintf(prefix,sizeof(prefix),"soundcloud:%s:",type);if(!strncmp(s,prefix,strlen(prefix)))s+=strlen(prefix);
    while(s[0]=='0'&&s[1])s++;
    n=strlen(s);if(!n||n>=cap||n>20||!strcmp(s,"0"))return -1;
    for(size_t i=0;i<n;i++)if(s[i]<'0'||s[i]>'9')return -1;
    memcpy(out,s,n+1);return 0;
}
static int id_obj(const JDoc *d,int obj,const char *type,char *out,size_t cap){
    char raw[80];int t=j_find(d,obj,"id");
    if(t>=0&&t<d->count){
        if(d->tokens[t].kind==J_STRING){if(!str(d,t,raw,sizeof(raw))&&!id_norm(raw,type,out,cap))return 0;}
        else if(d->tokens[t].kind==J_NUMBER){long long id=j_int(d,t,-1);if(id>0){int n=snprintf(out,cap,"%lld",id);if(n>0&&(size_t)n<cap)return 0;}}
    }
    return field(d,obj,"urn",raw,sizeof(raw))?-1:id_norm(raw,type,out,cap);
}
static int has_ci(const char *s,const char *needle){
    size_t n=strlen(needle);for(;*s;s++){size_t i;for(i=0;i<n&&s[i];i++)if(tolower((unsigned char)s[i])!=tolower((unsigned char)needle[i]))break;if(i==n)return 1;}return 0;
}
/* 1: полный трек; 2: ID-заглушка; 0: недоступен. */
static int decode(const JDoc *d,int obj,ScTrack *out){
    char k[32],a[32];int media,list,count,best=0;long long duration;
    if(obj<0||obj>=d->count||d->tokens[obj].kind!=J_OBJECT)return 0;
    memset(out,0,sizeof(*out));if(id_obj(d,obj,"tracks",out->id,sizeof(out->id)))return 0;
    if(!field(d,obj,"kind",k,sizeof(k))&&strcmp(k,"track"))return 0;
    if(!field(d,obj,"access",a,sizeof(a))&&(!strcmp(a,"preview")||!strcmp(a,"blocked")))return 0;
    if(!j_bool(d,j_find(d,obj,"streamable"),1))return 0;
    field(d,obj,"title",out->title,sizeof(out->title));field(d,obj,"artwork_url",out->artwork,sizeof(out->artwork));
    field(d,obj,"permalink_url",out->permalink,sizeof(out->permalink));
    field(d,j_find(d,obj,"user"),"username",out->artist,sizeof(out->artist));
    if(!out->artist[0])field(d,j_find(d,obj,"publisher_metadata"),"artist",out->artist,sizeof(out->artist));
    duration=j_int(d,j_find(d,obj,"duration"),0);if(duration<0)duration=0;
    out->duration_ms=duration>LONG_MAX?LONG_MAX:(long)duration;
    media=j_find(d,obj,"media");list=j_find(d,media,"transcodings");if(list<0)return 2;count=j_count(d,list);
    for(int i=0;i<count;i++){
        int t=j_at(d,list,i),fmt=j_find(d,t,"format"),rank;
        char url[2048],proto[32],mime[128],preset[256];long long part=j_int(d,j_find(d,t,"duration"),0);
        field(d,t,"preset",preset,sizeof(preset));
        if(has_ci(preset,"preview")||j_bool(d,j_find(d,t,"snipped"),0)||j_bool(d,j_find(d,t,"is_preview"),0)||
           (duration>0&&part>0&&duration-part>2000))continue;
        if(field(d,t,"url",url,sizeof(url))||!host(url,"api-v2.soundcloud.com",0)||has_ci(url,"preview")||
           field(d,fmt,"protocol",proto,sizeof(proto))||field(d,fmt,"mime_type",mime,sizeof(mime)))continue;
        if(!strcmp(proto,"progressive"))rank=300;
        else if(!strcmp(proto,"hls")&&has_ci(mime,"audio/mpeg"))rank=200;
        else if(!strcmp(proto,"hls"))rank=100;else continue;
        if(!strncmp(mime,"audio/",6))rank+=5;
        if(rank>best){best=rank;strcpy(out->transcoding,url);strcpy(out->protocol,proto);strcpy(out->mime,mime);}
    }return best?1:0;
}
static int contains(const ScQueue *q,const char *id){for(size_t i=0;i<q->count;i++)if(!strcmp(q->tracks[i].id,id))return 1;return 0;}
static int list_root(const JDoc *d){int t;if(d->tokens[0].kind==J_ARRAY)return 0;
    t=j_find(d,0,"collection");if(t>=0&&d->tokens[t].kind==J_ARRAY)return t;
    t=j_find(d,0,"tracks");return t>=0&&d->tokens[t].kind==J_ARRAY?t:-1;}
static int track_obj(const JDoc *d,int t){int x=j_find(d,t,"track");return x>=0&&d->tokens[x].kind==J_OBJECT?x:t;}
int sc_decode_queue(const char *json,ScQueue *q,char *e,size_t cap){
    JDoc *d;int list,n;clear(e,cap);if(!json||!q)return err(e,cap,"Не заданы данные очереди.");q->count=0;
    d=doc_parse(json,e,cap);if(!d)return -1;list=list_root(d);n=list>=0?j_count(d,list):1;
    for(int i=0;i<n&&q->count<256;i++){ScTrack t;int obj=list>=0?j_at(d,list,i):0;
        if(decode(d,track_obj(d,obj),&t)==1&&!contains(q,t.id))q->tracks[q->count++]=t;}
    free(d);return 0;
}
#ifndef SC_FIXTURE_ONLY
static int fetch_track(const ScConfig *c,const char *id,ScTrack *t,char *e,size_t cap){
    char url[256],*body;JDoc *d;int r;snprintf(url,sizeof(url),API "/tracks/%s",id);
    r=get(c,url,&body,e,cap);if(r)return r;d=doc_parse(body,e,cap);if(!d){free(body);return -1;}
    r=decode(d,0,t);free(d);free(body);if(r!=1){err(e,cap,"Полный поток этого трека недоступен.");return 1;}return 0;
}
static int me(const ScConfig *c,char *id,size_t idcap,char *e,size_t cap){
    char *body;JDoc *d;int r;if(login_required(c,e,cap))return -1;
    r=get(c,API "/me",&body,e,cap);if(r)return r;d=doc_parse(body,e,cap);if(!d){free(body);return -1;}
    r=id_obj(d,0,"users",id,idcap);free(d);free(body);return r?err(e,cap,"SoundCloud не вернул ID пользователя."):0;
}
int sc_check_login(const ScConfig *c,char *e,size_t cap){char id[64];clear(e,cap);if(config(c,e,cap))return -1;return me(c,id,sizeof(id),e,cap)?-1:0;}

static int have(const char ids[][64],int count,const char *id){
    for(int i=0;i<count;i++)if(!strcmp(ids[i],id))return 1;return 0;
}
/* Волна: /me/track_recommendations для аккаунтов пары отвечает 404, поэтому
 * рекомендации берутся из /tracks/{id}/related — того же источника, из которого
 * SoundCloud строит похожие треки. Источники волны: текущий трек и последние
 * лайки аккаунта; сами лайки и текущий трек в очередь не попадают. */
static int recs(const ScConfig *c,const ScTrack *current,ScQueue *q,char *e,size_t cap){
    char user[64],seeds[4][64],liked[200][64],url[URL_MAX];int seedn=0,likedn=0;
    if(login_required(c,e,cap))return -1;
    if(me(c,user,sizeof(user),e,cap))return -1;
    if(current&&!id_norm(current->id,"tracks",seeds[seedn],sizeof(seeds[0])))seedn++;
    {   /* Лайки дают и исходные точки волны, и список исключений. Страницы по 24:
         * большой ответ превышает лимит токенов JSON-парсера. */
        char *body;JDoc *d;int list,n;
        snprintf(url,sizeof(url),API "/users/%s/likes?linked_partitioning=true&limit=24",user);
        if(get(c,url,&body,e,cap))return -1;
        d=doc_parse(body,e,cap);if(!d){free(body);return -1;}
        list=list_root(d);n=list>=0?j_count(d,list):0;
        for(int i=0;i<n;i++){
            char id[64];int obj=track_obj(d,j_at(d,list,i));
            if(id_obj(d,obj,"tracks",id,sizeof(id)))continue;
            if(seedn<4&&!have((const char (*)[64])seeds,seedn,id))strcpy(seeds[seedn++],id);
            if(likedn<200&&!have((const char (*)[64])liked,likedn,id))strcpy(liked[likedn++],id);
        }
        free(d);free(body);
    }
    if(!seedn)return err(e,cap,"Нет исходных треков для волны.");
    for(int s=0;s<seedn&&q->count<200;s++){
        char *body;JDoc *d;int list,n,rc;
        snprintf(url,sizeof(url),API "/tracks/%s/related?limit=24&linked_partitioning=true",seeds[s]);
        rc=get(c,url,&body,e,cap);
        if(rc){if(s)break;q->count=0;return -1;}
        d=doc_parse(body,e,cap);if(!d){free(body);if(s)break;q->count=0;return -1;}
        list=list_root(d);n=list>=0?j_count(d,list):0;
        for(int i=0;i<n&&q->count<200;i++){
            ScTrack t;char id[64];int obj=track_obj(d,j_at(d,list,i));
            /* Заглушки без потоков пропускаем: волна не делает по запросу на трек. */
            if(id_obj(d,obj,"tracks",id,sizeof(id))||have((const char (*)[64])liked,likedn,id)||
               have((const char (*)[64])seeds,seedn,id)||contains(q,id))continue;
            if(decode(d,obj,&t)==1)q->tracks[q->count++]=t;
        }
        free(d);free(body);
    }
    return q->count?0:err(e,cap,"Похожие треки не найдены.");
}

int sc_queue(const ScConfig *c,const char *kind,const char *query,const ScTrack *current,
             ScQueue *q,char *e,size_t cap){
    char url[URL_MAX],previous[URL_MAX]={0},value[6147],id[64]={0};int playlist=0;
    unsigned hydrated=0;
    clear(e,cap);if(!q)return err(e,cap,"Не задана очередь SoundCloud.");q->count=0;
    if(config(c,e,cap))return -1;if(!kind||!text(kind))return err(e,cap,"Не задан режим SoundCloud.");
    if(!query)query="";if(strlen(query)>2048||!text(query))return err(e,cap,"Запрос слишком длинный или некорректный.");
    if(!strcmp(kind,"play")||!strcmp(kind,"playlist")){
        playlist=!strcmp(kind,"playlist");if(!*query)return err(e,cap,"Введите название или ссылку SoundCloud.");
        if(host(query,"soundcloud.com",1)){
            if(enc(query,value,sizeof(value)))return err(e,cap,"Ссылка слишком длинная.");
            snprintf(url,sizeof(url),API "/resolve?url=%s",value);
        }else if(playlist&&!id_norm(query,"playlists",id,sizeof(id)))snprintf(url,sizeof(url),API "/playlists/%s",id);
        else if(playlist)return err(e,cap,"Нужна ссылка или ID плейлиста SoundCloud.");
        else if(!strncmp(query,"http:",5)||!strncmp(query,"https:",6))return err(e,cap,"Поддерживаются только ссылки SoundCloud.");
        else{if(enc(query,value,sizeof(value)))return err(e,cap,"Поисковый запрос слишком длинный.");
        snprintf(url,sizeof(url),API "/search/tracks?q=%s&limit=24&linked_partitioning=true",value);}
    }else if(!strcmp(kind,"likes")){
        if(me(c,id,sizeof(id),e,cap))return -1;
        /* Страницы по 24 лайка: полный ответ при limit=100 превышает лимит
         * токенов JSON-парсера (366 КБ при сотне элементов). */
        snprintf(url,sizeof(url),API "/users/%s/likes?linked_partitioning=true&limit=24",id);
    }else if(!strcmp(kind,"recs")){
        /* Отдельная процедура: нужны несколько источников и список исключений. */
        return recs(c,current,q,e,cap);
    }else if(!strcmp(kind,"similar")){
        if(!current||!terminated(current->id,sizeof(current->id))||id_norm(current->id,"tracks",id,sizeof(id)))
            return err(e,cap,"Сначала выберите трек для похожих рекомендаций.");
        snprintf(url,sizeof(url),API "/tracks/%s/related?limit=24&linked_partitioning=true",id);
    }else return err(e,cap,"Неизвестный режим очереди SoundCloud.");
    for(unsigned page=0;page<16&&q->count<256;page++){
        char *body,next[URL_MAX]={0};JDoc *d;int list,n,nt;
        if(get(c,url,&body,e,cap)){q->count=0;return -1;}
        d=doc_parse(body,e,cap);if(!d){free(body);q->count=0;return -1;}list=list_root(d);
        if(playlist&&!page&&j_find(d,0,"tracks")<0){free(d);free(body);return err(e,cap,"Ссылка не указывает на плейлист SoundCloud.");}
        n=list>=0?j_count(d,list):1;
        for(int i=0;i<n&&q->count<256;i++){
            ScTrack t;int obj=list>=0?j_at(d,list,i):0,r=decode(d,track_obj(d,obj),&t);
            if(!r||contains(q,t.id))continue;
            if(r==2){char te[256];
                /* Ограничение запросов по заглушкам даже для недоступного плейлиста. */
                if(hydrated>=256)continue;
                hydrated++;r=fetch_track(c,t.id,&t,te,sizeof(te));
                if(r==1||r==-403||r==-404)continue;
                if(r){free(d);free(body);q->count=0;return err(e,cap,te);}}
            if(!strcmp(kind,"similar")&&!strcmp(t.id,id))continue;
            q->tracks[q->count++]=t;
        }
        nt=j_find(d,0,"next_href");
        if(nt>=0&&d->tokens[nt].kind==J_STRING&&str(d,nt,next,sizeof(next))){free(d);free(body);q->count=0;return err(e,cap,"Некорректная ссылка следующей страницы.");}
        free(d);free(body);if(!next[0]||q->count>=256)break;
        if(!host(next,"api-v2.soundcloud.com",0)){q->count=0;return err(e,cap,"Небезопасная ссылка следующей страницы SoundCloud.");}
        if(!strcmp(next,url)||!strcmp(next,previous))break;strcpy(previous,url);strcpy(url,next);
    }
    return q->count?0:err(e,cap,"Полные доступные треки не найдены.");
}
int sc_stream(const ScConfig *c,const ScTrack *t,char *out,size_t cap,char *e,size_t ecap){
    ScTrack full;char id[64],*body,url[URL_MAX];const char *u;JDoc *d;int r;
    clear(e,ecap);if(!t||!out||!cap)return err(e,ecap,"Не задан трек или буфер потока.");
    /* out может совпадать с transcoding: исходные данные сначала сохраняем. */
    full=*t;t=&full;clear(out,cap);
    if(config(c,e,ecap))return -1;
    if(!terminated(t->id,sizeof(t->id))||!terminated(t->transcoding,sizeof(t->transcoding)))return err(e,ecap,"Некорректные данные трека.");
    u=t->transcoding;if(!*u){if(id_norm(t->id,"tracks",id,sizeof(id)))return err(e,ecap,"Некорректный ID трека.");
        if(fetch_track(c,id,&full,e,ecap))return -1;u=full.transcoding;}
    if(has_ci(u,"preview"))return err(e,ecap,"Фрагмент трека вместо полного потока не поддерживается.");
    if(get(c,u,&body,e,ecap))return -1;d=doc_parse(body,e,ecap);if(!d){free(body);return -1;}
    r=field(d,0,"url",url,sizeof(url));free(d);free(body);
    if(r||(!host(url,"sndcdn.com",1)&&!host(url,"soundcloud.com",1)&&!host(url,"soundcloud.cloud",1)))return err(e,ecap,"SoundCloud не вернул безопасный адрес аудиопотока.");
    if(strlen(url)>=cap)return err(e,ecap,"Буфер адреса потока слишком мал.");strcpy(out,url);return 0;
}
int sc_like(const ScConfig *c,const ScTrack *t,int enabled,char *e,size_t cap){
    char user[64],id[64],url[1024],value[384],*body;clear(e,cap);
    if(config(c,e,cap)||login_required(c,e,cap))return -1;
    if(!t||!terminated(t->id,sizeof(t->id))||id_norm(t->id,"tracks",id,sizeof(id)))return err(e,cap,"Некорректный ID трека для лайка.");
    if(me(c,user,sizeof(user),e,cap))return -1;snprintf(url,sizeof(url),API "/users/%s/track_likes/%s",user,id);
    if(c->client_id[0]&&(enc(c->client_id,value,sizeof(value))||add(url,sizeof(url),"?client_id=")||add(url,sizeof(url),value)))
        return err(e,cap,"Запрос лайка слишком длинный.");
    if(request(c,enabled?"PUT":"DELETE",url,NULL,1,&body,e,cap))return -1;free(body);return 0;
}
int sc_refresh(const ScConfig *c,const char *refresh,char *at,size_t atcap,
               char *rt,size_t rtcap,long *expires,char *e,size_t cap){
    char form[32768]="grant_type=refresh_token&client_id=",v[12289],refresh_enc[12289],*body;
    char access[4097],renewed[4097];const char *secret=getenv("SOUNDYA_SC_CLIENT_SECRET");JDoc *d;long long seconds;int bad;
    clear(e,cap);
    /* Можно обновить rt на месте: сначала копируем вход. */
    bad=!refresh||!text(refresh)||strlen(refresh)>4096||enc(refresh,refresh_enc,sizeof(refresh_enc));
    clear(at,atcap);clear(rt,rtcap);if(expires)*expires=0;
    if(bad)return err(e,cap,"Некорректный токен обновления SoundCloud.");
    if(config(c,e,cap))return -1;
    if(!at||!rt||!atcap||!rtcap||!expires||!c->client_id[0])return err(e,cap,"Для обновления нужны client_id и буферы токенов.");
    if(enc(c->client_id,v,sizeof(v))||add(form,sizeof(form),v)||add(form,sizeof(form),"&refresh_token=")||add(form,sizeof(form),refresh_enc))
        return err(e,cap,"Запрос обновления слишком длинный.");
    if(secret&&*secret){if(!text(secret)||strlen(secret)>4096||enc(secret,v,sizeof(v))||add(form,sizeof(form),"&client_secret=")||add(form,sizeof(form),v))
        return err(e,cap,"Некорректный секрет приложения SoundCloud.");}
    bad=request(c,"POST","https://secure.soundcloud.com/oauth/token",form,0,&body,e,cap);
    memset(form,0,sizeof(form));memset(v,0,sizeof(v));memset(refresh_enc,0,sizeof(refresh_enc));if(bad)return -1;
    d=doc_parse(body,e,cap);if(!d){free(body);return -1;}
    bad=field(d,0,"access_token",access,sizeof(access))||field(d,0,"refresh_token",renewed,sizeof(renewed));
    seconds=j_int(d,j_find(d,0,"expires_in"),0);free(d);memset(body,0,strlen(body));free(body);
    if(bad||!access[0]||!renewed[0]||!text(access)||!text(renewed)||seconds<=0||seconds>LONG_MAX)
        return err(e,cap,"Некорректный ответ обновления токенов SoundCloud.");
    if(strlen(access)>=atcap||strlen(renewed)>=rtcap)return err(e,cap,"Буферы токенов слишком малы; требуется повторный вход.");
    strcpy(at,access);strcpy(rt,renewed);*expires=(long)seconds;memset(access,0,sizeof(access));memset(renewed,0,sizeof(renewed));return 0;
}
#endif
