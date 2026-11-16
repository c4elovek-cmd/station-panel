#include "../src/hls.h"

#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Фикстуры собираются здесь: никаких внешних файлов или медиабиблиотек. */
typedef struct { unsigned char d[8192]; size_t n; } Buffer;
static char error[256];

static void put8(Buffer *b, unsigned v) { assert(b->n < sizeof b->d); b->d[b->n++] = (unsigned char)v; }
static void put32(Buffer *b, uint32_t v) { put8(b,v>>24); put8(b,v>>16); put8(b,v>>8); put8(b,v); }
static void patch32(Buffer *b, size_t p, uint32_t v)
{
    assert(p + 4 <= b->n);
    b->d[p] = (unsigned char)(v >> 24); b->d[p+1] = (unsigned char)(v >> 16);
    b->d[p+2] = (unsigned char)(v >> 8); b->d[p+3] = (unsigned char)v;
}
static size_t box(Buffer *b, const char *name)
{
    size_t p = b->n, i;
    put32(b,0);
    for (i=0;i<4;++i) put8(b,(unsigned char)name[i]);
    return p;
}
static size_t extended_box(Buffer *b, const char *name)
{
    size_t p = box(b,name);
    patch32(b,p,1); put32(b,0); put32(b,0);
    return p;
}
static void close_box(Buffer *b, size_t p)
{
    if (b->d[p+3] == 1) patch32(b,p+12,(uint32_t)(b->n-p));
    else patch32(b,p,(uint32_t)(b->n-p));
}

static void zeros(Buffer *b, size_t n) { while(n--) put8(b,0); }

static Buffer init_fixture(int extended)
{
    Buffer b = {{0},0};
    size_t moov = extended ? extended_box(&b,"moov") : box(&b,"moov");
    size_t trak = box(&b,"trak"), mdia = box(&b,"mdia"), minf = box(&b,"minf");
    size_t stbl = box(&b,"stbl"), stsd = box(&b,"stsd"), mp4a, esds;
    put32(&b,0); put32(&b,1);
    mp4a = box(&b,"mp4a");
    zeros(&b,6); put8(&b,0); put8(&b,1); zeros(&b,8);
    put8(&b,0); put8(&b,2); put8(&b,0); put8(&b,16); zeros(&b,4); put32(&b,44100u<<16);
    esds = box(&b,"esds");
    put32(&b,0);
    put8(&b,3); put8(&b,25); put8(&b,0); put8(&b,1); put8(&b,0);
    put8(&b,4); put8(&b,17); put8(&b,0x40); put8(&b,0x15); zeros(&b,11);
    put8(&b,5); put8(&b,2); put8(&b,0x12); put8(&b,0x10);
    put8(&b,6); put8(&b,1); put8(&b,2);
    close_box(&b,esds); close_box(&b,mp4a); close_box(&b,stsd); close_box(&b,stbl);
    close_box(&b,minf); close_box(&b,mdia); close_box(&b,trak); close_box(&b,moov);
    return b;
}

static Buffer fragment_fixture(int defaults, int extended, int multiple_runs)
{
    Buffer b = {{0},0};
    size_t moof = box(&b,"moof"), traf = box(&b,"traf"), tfhd = box(&b,"tfhd");
    size_t trun, offset, mdat;
    put32(&b,0x020000u | (defaults ? 16u : 0u)); put32(&b,1);
    if (defaults) put32(&b,3);
    close_box(&b,tfhd);
    trun = box(&b,"trun");
    put32(&b,defaults ? 1u : 0x201u); put32(&b,multiple_runs ? 1 : 2);
    offset = b.n; put32(&b,0);
    if (!defaults) { put32(&b,3); if (!multiple_runs) put32(&b,2); }
    close_box(&b,trun);
    if (multiple_runs) {
        trun = box(&b,"trun"); put32(&b,defaults ? 0 : 0x200u); put32(&b,1);
        if (!defaults) put32(&b,2);
        close_box(&b,trun);
    }
    close_box(&b,traf); close_box(&b,moof);
    mdat = extended ? extended_box(&b,"mdat") : box(&b,"mdat");
    patch32(&b,offset,(uint32_t)b.n);
    put8(&b,0x11); put8(&b,0x22); put8(&b,0x33); put8(&b,0x44); put8(&b,0x55);
    if (defaults) put8(&b,0x66);
    close_box(&b,mdat);
    return b;
}

static size_t find_type(Buffer *b, const char *type)
{
    size_t i;
    for (i=4; i+4<=b->n; ++i) if (!memcmp(b->d+i,type,4)) return i-4;
    assert(0); return 0;
}

static size_t output(FILE *f, unsigned char *bytes, size_t cap)
{
    long n;
    assert(!fflush(f)); n=ftell(f); assert(n>=0 && (size_t)n<=cap);
    rewind(f); assert(fread(bytes,1,(size_t)n,f)==(size_t)n);
    return (size_t)n;
}
static void expect_segment(Buffer *b, size_t expected)
{
    const unsigned char asc[2] = {0x12,0x10};
    unsigned char bytes[64];
    FILE *f=tmpfile(); size_t n;
    assert(f);
    {
        int rc = hls_aac_segment(b->d,b->n,asc,f,error,sizeof error);
        if (rc) fprintf(stderr, "Сегмент %zu байт: %s\n", b->n, error);
        assert(rc==0);
    }
    n=output(f,bytes,sizeof bytes); assert(n==expected);
    assert(bytes[0]==0xff && bytes[1]==0xf1 && bytes[2]==0x50 && bytes[3]==0x80);
    assert(bytes[4]==1 && bytes[5]==0x5f && bytes[6]==0xfc);
    assert(bytes[7]==0x11 && bytes[8]==0x22 && bytes[9]==0x33 && bytes[10]==0xff);
    fclose(f);
}
static void reject_segment(Buffer *b)
{
    const unsigned char asc[2] = {0x12,0x10}; FILE *f=tmpfile();
    assert(f); assert(hls_aac_segment(b->d,b->n,asc,f,error,sizeof error)==-1);
    assert(ftell(f)==0); assert(error[0]); fclose(f);
}

static void test_playlist(void)
{
    HlsList *list=malloc(sizeof *list);
    char *many=malloc(65536); size_t i, n=0;
    assert(list && many);
    assert(!hls_parse("#EXTM3U\r\n#EXT-X-MAP:URI=\"../init.mp4?x=1,2\"\n#EXTINF:2.5,\n./a.m4s\n#EXTINF:3,\n/root/../b.m4s\n",
                     "https://radio.test/live/sub/list.m3u8?token=z",list,error,sizeof error));
    assert(list->count==2 && list->duration==5.5 && !list->encrypted);
    assert(!strcmp(list->init_url,"https://radio.test/live/init.mp4?x=1,2"));
    assert(!strcmp(list->segments[0],"https://radio.test/live/sub/a.m4s"));
    assert(!strcmp(list->segments[1],"https://radio.test/b.m4s"));
    assert(!hls_parse("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n../low/list.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=2\n//other.test/high.m3u8\n",
                     "https://radio.test/master/main.m3u8",list,error,sizeof error));
    assert(list->count==1 && list->duration==0);
    assert(!strcmp(list->segments[0],"https://radio.test/low/list.m3u8"));
    assert(hls_parse("#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"key\"\n","https://x/a",list,error,sizeof error)==-1 && list->encrypted);
    assert(hls_parse("#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES\n","https://x/a",list,error,sizeof error)==-1);
    assert(hls_parse("#EXTM3U\n#EXT-X-BYTERANGE:10@0\n","https://x/a",list,error,sizeof error)==-1);
    assert(hls_parse("#EXTM3U\n#EXT-X-MAP:URI=\"i\",BYTERANGE=\"10\"\n","https://x/a",list,error,sizeof error)==-1);
    assert(hls_parse("#EXTM3U\n#EXTINF:nan,\nx\n","https://x/a",list,error,sizeof error)==-1);
    assert(hls_parse("#EXTM3U\n#EXTINF:0,\nx\n","https://x/a",list,error,sizeof error)==-1);
    assert(!hls_parse("#EXTM3U\n#EXTINF:1,\n?token=new\n#EXTINF:1,\n//cdn.test/a\n",
                     "https://x/dir/list?token=old",list,error,sizeof error));
    assert(!strcmp(list->segments[0],"https://x/dir/list?token=new"));
    assert(!strcmp(list->segments[1],"https://cdn.test/a"));
    assert(hls_parse("#EXTM3U\n#EXT-X-SESSION-KEY:METHOD=AES-128\n","https://x/a",list,error,sizeof error)==-1 && list->encrypted);
    assert(hls_parse("#EXTM3U\n#EXTINF:1,\nfile:///tmp/a\n","https://x/a",list,error,sizeof error)==-1);
    assert(hls_parse("", "https://x/a",list,NULL,0)==-1);
    assert(hls_parse("#EXTM3U\n#EXTINF:1,\na\n","https://x/a",list,error,1)==0);
    n+=(size_t)sprintf(many+n,"#EXTM3U\n#EXT-X-KEY:METHOD=NONE\n");
    for(i=0;i<1024;++i) n+=(size_t)sprintf(many+n,"#EXTINF:1,\n%zu.aac\n",i);
    assert(!hls_parse(many,"https://x/list",list,error,sizeof error) && list->count==1024);
    strcpy(many+n,"#EXTINF:1,\nextra.aac\n");
    assert(hls_parse(many,"https://x/list",list,error,sizeof error)==-1);
    free(many); free(list);
}

static void test_init(void)
{
    unsigned char asc[2]={0,0};
    Buffer b=init_fixture(0), ext=init_fixture(1), corrupt;
    size_t esds=find_type(&b,"esds"), mp4a=find_type(&b,"mp4a"), i;
    assert(!hls_aac_config(b.d,b.n,asc,error,sizeof error));
    assert(asc[0]==0x12 && asc[1]==0x10);
    assert(!hls_aac_config(ext.d,ext.n,asc,error,sizeof error));
    for(i=0;i<b.n;++i) assert(hls_aac_config(b.d,i,asc,NULL,0)==-1);
    corrupt=b; corrupt.d[esds+8+4+2+3+2+13+2]=0x2a;
    assert(hls_aac_config(corrupt.d,corrupt.n,asc,error,sizeof error)==-1);
    corrupt=b; corrupt.d[esds+8+4+2+3+2+13+3]=0x18;
    assert(hls_aac_config(corrupt.d,corrupt.n,asc,error,sizeof error)==-1);
    corrupt=b; corrupt.d[esds+8+4+2+3+2+13+2]=0x17;
    corrupt.d[esds+8+4+2+3+2+13+3]=0x90;
    assert(hls_aac_config(corrupt.d,corrupt.n,asc,error,sizeof error)==-1);
    corrupt=b; memcpy(corrupt.d+mp4a+4,"enca",4);
    assert(hls_aac_config(corrupt.d,corrupt.n,asc,error,sizeof error)==-1);
    corrupt=b; patch32(&corrupt,esds,0xfffffff0u);
    assert(hls_aac_config(corrupt.d,corrupt.n,asc,error,sizeof error)==-1);
    corrupt=b; memcpy(corrupt.d+esds+4,"free",4);
    assert(hls_aac_config(corrupt.d,corrupt.n,asc,error,sizeof error)==-1);
    /* ASC в произвольных данных не должен находиться сканированием. */
    corrupt.n=0; i=box(&corrupt,"free"); put8(&corrupt,5); put8(&corrupt,2); put8(&corrupt,0x12); put8(&corrupt,0x10); close_box(&corrupt,i);
    assert(hls_aac_config(corrupt.d,corrupt.n,asc,error,sizeof error)==-1);
}

static void test_other_rate(void)
{
    Buffer b=fragment_fixture(0,0,0);
    const unsigned char asc[2]={0x11,0x88}; /* AAC-LC, 48 кГц, mono. */
    unsigned char bytes[64]; FILE *f=tmpfile();
    assert(f); assert(!hls_aac_segment(b.d,b.n,asc,f,error,sizeof error));
    assert(output(f,bytes,sizeof bytes)==19);
    assert(bytes[2]==0x4c && bytes[3]==0x40); fclose(f);
}

static void test_fragments(void)
{
    Buffer b=fragment_fixture(0,0,0), c;
    size_t trun=find_type(&b,"trun"), tfhd=find_type(&b,"tfhd"), traf=find_type(&b,"traf"), i;
    expect_segment(&b,19);
    c=fragment_fixture(1,0,0); expect_segment(&c,20);
    c=fragment_fixture(0,1,0); expect_segment(&c,19);
    c=fragment_fixture(0,0,1); expect_segment(&c,19);
    c=fragment_fixture(1,1,1); expect_segment(&c,20);
    for(i=0;i<b.n;++i) { c=b; c.n=i; reject_segment(&c); }
    c=b; patch32(&c,trun+16,0xfffffff0u); reject_segment(&c);
    c=b; patch32(&c,trun+20,9000); reject_segment(&c);
    c=b; patch32(&c,trun+24,200); reject_segment(&c);
    c=b; patch32(&c,trun+12,0xffffffffu); reject_segment(&c);
    c=b; memcpy(c.d+tfhd+4,"senc",4); reject_segment(&c);
    c=b; memcpy(c.d+trun+4,"saiz",4); reject_segment(&c);
    c=b; patch32(&c,0,0xffffffffu); reject_segment(&c);
    /* Смещения считаются от начала moof, а не всегда от начала буфера. */
    c=b;
    memmove(c.d+8,c.d,c.n); c.n+=8;
    patch32(&c,0,8); memcpy(c.d+4,"free",4);
    expect_segment(&c,19);
    /* Бокс mdat размера 0 занимает весь остаток родителя. */
    c=b; patch32(&c,find_type(&c,"mdat"),0); expect_segment(&c,19);
    /* Вторая traf с тем же содержимым не должна угадываться. */
    c=b;
    {
        size_t n=8 + (size_t)((b.d[traf+3]) | ((unsigned)b.d[traf+2]<<8));
        size_t end=(size_t)b.d[3];
        n-=8;
        assert(c.n+n<sizeof c.d);
        memmove(c.d+end+n,c.d+end,c.n-end);
        memcpy(c.d+end,b.d+traf,n); c.n+=n; patch32(&c,0,(uint32_t)(end+n));
        reject_segment(&c);
    }
    /* Валидный явный base_data_offset вместо trun.data_offset. */
    c=b;
    {
        size_t pos=tfhd+16;
        memmove(c.d+pos+8,c.d+pos,c.n-pos); c.n+=8;
        patch32(&c,tfhd,24); patch32(&c,tfhd+8,1); patch32(&c,pos,0);
        patch32(&c,pos+4,(uint32_t)(b.n+8-5));
        patch32(&c,traf,(uint32_t)(b.d[traf+3]+8)); patch32(&c,0,(uint32_t)(b.d[3]+8));
        trun+=8;
        memmove(c.d+trun+16,c.d+trun+20,c.n-(trun+20)); c.n-=4;
        patch32(&c,trun,24); patch32(&c,trun+8,0x200u);
        patch32(&c,traf,(uint32_t)(b.d[traf+3]+4)); patch32(&c,0,(uint32_t)(b.d[3]+4));
        patch32(&c,pos+4,(uint32_t)(b.n+4-5));
        expect_segment(&c,19);
    }
}

static uint32_t crc(const unsigned char *d,size_t n)
{
    uint32_t v=0xffffffffu; size_t i; unsigned j;
    for(i=0;i<n;++i) { v^=(uint32_t)d[i]<<24; for(j=0;j<8;++j) v=(v<<1)^((v&0x80000000u)?0x04c11db7u:0); }
    return v;
}
static void crc_finish(Buffer *b) { uint32_t v=crc(b->d,b->n); put32(b,v); }
static void ts_packet_write(Buffer *b,unsigned pid,unsigned cc,int start,const unsigned char *p,size_t n)
{
    size_t begin=b->n, adapt;
    assert(n<=184);
    put8(b,0x47); put8(b,(pid>>8)|(start?0x40u:0u)); put8(b,pid);
    if(n<184) {
        put8(b,0x30u|cc); adapt=183-n; put8(b,(unsigned)adapt);
        if(adapt) { put8(b,0); while(--adapt) put8(b,0xff); }
    } else put8(b,0x10u|cc);
    while(n--) put8(b,*p++);
    assert(b->n-begin==188);
}
static Buffer ts_fixture(unsigned type,int split_psi,int split_pes)
{
    Buffer b={{0},0}, pat={{0},0}, pmt={{0},0}, pes={{0},0}, section={{0},0};
    size_t i;
    const unsigned char pd[]={0x00,0xb0,13,0,1,0xc1,0,0,0,1,0xe1,0};
    const unsigned char pm[]={0x02,0xb0,18,0,1,0xc1,0,0,0xe1,1,0xf0,0,0,0xe1,1,0xf0,0};
    for(i=0;i<sizeof pd;++i) put8(&pat,pd[i]);
    crc_finish(&pat);
    for(i=0;i<sizeof pm;++i) put8(&pmt,pm[i]);
    pmt.d[12]=(unsigned char)type; crc_finish(&pmt);
    put8(&section,0); for(i=0;i<pat.n;++i) put8(&section,pat.d[i]);
    if(split_psi) {
        ts_packet_write(&b,0,0,1,section.d,7);
        ts_packet_write(&b,0,1,0,section.d+7,section.n-7);
    } else ts_packet_write(&b,0,0,1,section.d,section.n);
    section.n=0; put8(&section,0); for(i=0;i<pmt.n;++i) put8(&section,pmt.d[i]);
    ts_packet_write(&b,0x100,0,1,section.d,section.n);
    put8(&pes,0); put8(&pes,0); put8(&pes,1); put8(&pes,0xc0);
    put8(&pes,0); put8(&pes,6); put8(&pes,0x80); put8(&pes,0); put8(&pes,0);
    put8(&pes,0x11); put8(&pes,0x22); put8(&pes,0x33);
    if(split_pes) {
        ts_packet_write(&b,0x101,0,1,pes.d,5);
        ts_packet_write(&b,0x101,1,0,pes.d+5,pes.n-5);
    } else ts_packet_write(&b,0x101,0,1,pes.d,pes.n);
    return b;
}
static void expect_ts(Buffer *b,int good)
{
    unsigned char bytes[32]; FILE *f=tmpfile(); assert(f);
    assert(hls_ts_audio(b->d,b->n,f,error,sizeof error)==(good?0:-1));
    if(good) { assert(output(f,bytes,sizeof bytes)==3); assert(bytes[0]==0x11 && bytes[2]==0x33); }
    else assert(ftell(f)==0);
    fclose(f);
}
static void test_ts(void)
{
    Buffer b=ts_fixture(15,0,0), c;
    const unsigned char adts[]={0xff,0xf1,0x50,0x80,0x01,0x5f,0xfc,0x11,0x22,0x33};
    unsigned char bytes[32]; FILE *f;
    expect_ts(&b,1);
    c=ts_fixture(3,1,1); expect_ts(&c,1);
    c=ts_fixture(4,0,1); expect_ts(&c,1);
    c=ts_fixture(0x11,0,0); expect_ts(&c,0);
    c=b; c.d[0]=0; expect_ts(&c,0);
    c=b; c.d[3]|=0x80; expect_ts(&c,0);
    c=b; --c.n; expect_ts(&c,0);
    c=ts_fixture(15,0,1); c.d[3*188+3]=(unsigned char)((c.d[3*188+3]&0xf0)|5); expect_ts(&c,0);
    c=b; c.d[188+187]^=1; expect_ts(&c,0);
    c=ts_fixture(15,0,1); c.n-=188; expect_ts(&c,0);
    f=tmpfile(); assert(f);
    assert(!hls_ts_audio(adts,sizeof adts,f,error,sizeof error));
    assert(output(f,bytes,sizeof bytes)==sizeof adts && !memcmp(bytes,adts,sizeof adts)); fclose(f);
    f=tmpfile(); assert(f); assert(hls_ts_audio(adts,sizeof adts-1,f,error,sizeof error)==-1 && ftell(f)==0); fclose(f);
}

static void test_mutations(void)
{
    Buffer init=init_fixture(0), segment=fragment_fixture(0,0,0), changed;
    uint32_t seed=0x1a2b3c4du;
    unsigned char asc[2]={0x12,0x10}, result[2];
    size_t i;
    FILE *f=tmpfile(); assert(f);
    for (i=0;i<2000;++i) {
        int rc;
        seed=seed*1664525u+1013904223u;
        changed=init;
        changed.d[seed%changed.n]^=(unsigned char)(1u<<((seed>>24)&7u));
        rc=hls_aac_config(changed.d,changed.n,result,error,sizeof error);
        assert(rc==0 || rc==-1);
        seed=seed*1664525u+1013904223u;
        changed=segment;
        changed.d[seed%changed.n]^=(unsigned char)(1u<<((seed>>24)&7u));
        rc=hls_aac_segment(changed.d,changed.n,asc,f,error,sizeof error);
        assert(rc==0 || rc==-1);
    }
    fclose(f);
}

int main(void)
{
    test_playlist(); test_init(); test_fragments(); test_other_rate(); test_ts(); test_mutations();
    puts("HLS: все проверки пройдены");
    return 0;
}
