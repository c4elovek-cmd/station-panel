#include <stdio.h>
#include "hls.h"

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

static int fail(char *err, size_t cap, const char *message)
{
    if (err && cap) snprintf(err, cap, "%s", message);
    return -1;
}

static void clear_error(char *err, size_t cap)
{
    if (err && cap) err[0] = '\0';
}

static uint32_t be32(const unsigned char *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8) | p[3];
}

static uint64_t be64(const unsigned char *p)
{
    return ((uint64_t)be32(p) << 32) | be32(p + 4);
}

#define FOUR(a,b,c,d) (((uint32_t)(a)<<24)|((uint32_t)(b)<<16)|((uint32_t)(c)<<8)|(d))
#define ISBOX(b,a,c,d,e) ((b).type == FOUR(a,c,d,e))

typedef struct {
    size_t start, payload, end;
    uint32_t type;
} Box;

/* Каждая граница проверяется относительно непосредственного родителя. */
static int next_box(const unsigned char *data, size_t end, size_t *pos, Box *b)
{
    size_t header = 8;
    uint64_t size;
    if (*pos == end) return 0;
    if (*pos > end || end - *pos < 8) return -1;
    b->start = *pos;
    size = be32(data + *pos);
    b->type = be32(data + *pos + 4);
    if (size == 1) {
        if (end - *pos < 16) return -1;
        size = be64(data + *pos + 8);
        header = 16;
    } else if (size == 0) size = end - *pos;
    if (size < header || size > end - *pos) return -1;
    b->payload = *pos + header;
    b->end = *pos + (size_t)size;
    *pos = b->end;
    return 1;
}

static int encrypted_box(uint32_t t)
{
    return t == FOUR('s','i','n','f') || t == FOUR('s','e','n','c') ||
           t == FOUR('s','a','i','z') || t == FOUR('s','a','i','o') ||
           t == FOUR('t','e','n','c') ||
           t == FOUR('e','n','c','a') || t == FOUR('e','n','c','v') ||
           t == FOUR('p','s','s','h') || t == FOUR('u','u','i','d');
}

static int clean_uri(const char *s)
{
    const unsigned char *p = (const unsigned char *)s;
    if (!*p) return 0;
    for (; *p; ++p) if (*p <= 32 || *p == 127 || *p == '\\') return 0;
    return 1;
}

/* Удаление dot-segments по RFC 3986, без декодирования %-последовательностей. */
static int normalize_url(const char *url, char out[HLS_URL_CAP])
{
    const char *authority, *path, *suffix, *p;
    size_t origin, n, restore[HLS_URL_CAP / 2], depth = 0;
    if (!clean_uri(url)) return -1;
    if (!strncmp(url, "https://", 8)) authority = url + 8;
    else if (!strncmp(url, "http://", 7)) authority = url + 7;
    else return -1;
    path = authority + strcspn(authority, "/?#");
    if (path == authority || memchr(authority, '@', (size_t)(path - authority))) return -1;
    origin = (size_t)(path - url);
    suffix = path + strcspn(path, "?#");
    if (origin + 2 >= HLS_URL_CAP) return -1;
    memcpy(out, url, origin);
    n = origin;
    if (*path != '/') out[n++] = '/';
    p = path;
    while (p < suffix) {
        const char *start;
        size_t len, old = n;
        if (*p == '/') ++p;
        start = p;
        while (p < suffix && *p != '/') ++p;
        len = (size_t)(p - start);
        if (len == 1 && start[0] == '.') {
            if (p == suffix && (n == origin || out[n - 1] != '/')) out[n++] = '/';
            continue;
        }
        if (len == 2 && start[0] == '.' && start[1] == '.') {
            if (depth) n = restore[--depth];
            if (p == suffix && (n == origin || out[n - 1] != '/')) out[n++] = '/';
            continue;
        }
        if (n + len + 1 >= HLS_URL_CAP || depth >= HLS_URL_CAP / 2) return -1;
        out[n++] = '/';
        memcpy(out + n, start, len);
        n += len;
        restore[depth++] = old;
    }
    if (n == origin) out[n++] = '/';
    if (strlen(suffix) >= HLS_URL_CAP - n) return -1;
    strcpy(out + n, suffix);
    return 0;
}

static int resolve_uri(const char *base, const char *uri, char out[HLS_URL_CAP])
{
    char b[HLS_URL_CAP], joined[HLS_URL_CAP];
    const char *path, *end, *slash;
    size_t prefix, ulen = strlen(uri);
    if (normalize_url(base, b) < 0 || !clean_uri(uri) || ulen >= HLS_URL_CAP) return -1;
    if (!strncmp(uri, "https://", 8) || !strncmp(uri, "http://", 7)) return normalize_url(uri, out);
    if (strchr(uri, ':') && (size_t)(strchr(uri, ':') - uri) < strcspn(uri, "/?#")) return -1;
    if (uri[0] == '/' && uri[1] == '/') {
        prefix = (size_t)(strchr(b, ':') - b) + 1;
    } else {
        path = strstr(b, "://") + 3;
        path += strcspn(path, "/?#");
        end = b + strcspn(b, "?#");
        if (uri[0] == '/') prefix = (size_t)(path - b);
        else if (uri[0] == '?') prefix = (size_t)(end - b);
        else if (uri[0] == '#') prefix = strcspn(b, "#");
        else {
            slash = end;
            while (slash > path && slash[-1] != '/') --slash;
            prefix = (size_t)(slash - b);
        }
    }
    if (prefix + ulen >= sizeof joined) return -1;
    memcpy(joined, b, prefix);
    strcpy(joined + prefix, uri);
    return normalize_url(joined, out);
}

/* Чтение атрибутов, в том числе запятых внутри кавычек. */
static int attribute(const char *s, const char *name, char *out, size_t cap)
{
    int found = 0;
    while (*s) {
        const char *key, *value;
        size_t kn, vn;
        while (*s == ',' || *s == ' ' || *s == '\t') ++s;
        if (!*s) break;
        key = s;
        while (*s && *s != '=' && *s != ',') ++s;
        kn = (size_t)(s - key);
        if (*s != '=') return -1;
        ++s;
        if (*s == '"') {
            value = ++s;
            while (*s && *s != '"') ++s;
            if (!*s) return -1;
            vn = (size_t)(s++ - value);
        } else {
            value = s;
            while (*s && *s != ',') ++s;
            vn = (size_t)(s - value);
        }
        while (*s == ' ' || *s == '\t') ++s;
        if (*s && *s != ',') return -1;
        if (strlen(name) == kn && !memcmp(key, name, kn)) {
            if (found || !vn || vn >= cap) return -1;
            memcpy(out, value, vn);
            out[vn] = '\0';
            found = 1;
        }
    }
    return found;
}

int hls_parse(const char *text, const char *base_url, HlsList *list, char *err, size_t cap)
{
    const char *p;
    char line[8192], value[HLS_URL_CAP], base[HLS_URL_CAP];
    int header = 0, variant = 0, master = 0, media = 0, pending = 0;
    size_t text_size;
    double seconds = 0;
    clear_error(err, cap);
    if (!text || !base_url || !list) return fail(err, cap, "Некорректные аргументы HLS");
    memset(list, 0, sizeof *list);
    if (normalize_url(base_url, base) < 0) return fail(err, cap, "Некорректный базовый URL HLS");
    for (text_size = 0; text_size <= HLS_MAX_MEDIA_BYTES && text[text_size]; ++text_size) {}
    if (text_size > HLS_MAX_MEDIA_BYTES) return fail(err, cap, "Список HLS превышает 16 МиБ");
    p = text;
    if (text_size >= 3 && (unsigned char)p[0] == 0xef && (unsigned char)p[1] == 0xbb && (unsigned char)p[2] == 0xbf) p += 3;
    while (*p) {
        size_t len = strcspn(p, "\r\n");
        char *s, *tail;
        if (len >= sizeof line) return fail(err, cap, "Строка HLS слишком длинная");
        memcpy(line, p, len);
        line[len] = '\0';
        p += len;
        if (*p == '\r') ++p;
        if (*p == '\n') ++p;
        s = line;
        while (*s == ' ' || *s == '\t') ++s;
        tail = s + strlen(s);
        while (tail > s && (tail[-1] == ' ' || tail[-1] == '\t')) *--tail = '\0';
        if (!*s) continue;
        if (!header) {
            if (strcmp(s, "#EXTM3U")) return fail(err, cap, "Отсутствует заголовок EXTM3U");
            header = 1;
            continue;
        }
        if (!strncmp(s, "#EXT-X-BYTERANGE", 16)) return fail(err, cap, "HLS BYTERANGE пока не поддерживается");
        if (!strncmp(s, "#EXT-X-KEY:", 11) || !strncmp(s, "#EXT-X-SESSION-KEY:", 19)) {
            const char *attrs = strchr(s, ':') + 1;
            if (attribute(attrs, "METHOD", value, sizeof value) != 1) return fail(err, cap, "Некорректный METHOD ключа HLS");
            if (strcmp(value, "NONE")) {
                list->encrypted = 1;
                return fail(err, cap, "Зашифрованный HLS не поддерживается");
            }
        } else if (!strncmp(s, "#EXT-X-MAP:", 11)) {
            int br = attribute(s + 11, "BYTERANGE", value, sizeof value);
            if (br != 0) return fail(err, cap, "HLS MAP BYTERANGE не поддерживается");
            if (attribute(s + 11, "URI", value, sizeof value) != 1) return fail(err, cap, "Отсутствует URI в HLS MAP");
            if (list->init_url[0]) {
                char resolved[HLS_URL_CAP];
                if (resolve_uri(base, value, resolved) < 0 || strcmp(resolved, list->init_url)) return fail(err, cap, "Смена HLS MAP внутри списка не поддерживается");
            } else if (resolve_uri(base, value, list->init_url) < 0) return fail(err, cap, "Некорректный URL HLS MAP");
        } else if (!strncmp(s, "#EXT-X-STREAM-INF:", 18)) {
            if (pending || variant || media) return fail(err, cap, "Некорректный master HLS");
            master = variant = 1;
        } else if (!strncmp(s, "#EXTINF:", 8)) {
            char *end;
            if (master || pending) return fail(err, cap, "Некорректный EXTINF HLS");
            seconds = strtod(s + 8, &end);
            if (end == s + 8 || *end != ',' || !(seconds > 0) || seconds > 86400) return fail(err, cap, "Некорректная длительность EXTINF");
            media = pending = 1;
        } else if (*s != '#') {
            if (master) {
                if (!variant) return fail(err, cap, "URI без STREAM-INF в master HLS");
                if (list->count == 0) {
                    if (resolve_uri(base, s, list->segments[0]) < 0) return fail(err, cap, "Некорректный URI варианта HLS");
                    list->count = 1;
                }
                variant = 0;
            } else {
                if (!pending) return fail(err, cap, "URI сегмента без EXTINF");
                if (list->count == HLS_MAX_SEGMENTS) return fail(err, cap, "Превышен лимит 1024 сегментов HLS");
                if (resolve_uri(base, s, list->segments[list->count]) < 0) return fail(err, cap, "Некорректный URI сегмента HLS");
                ++list->count;
                list->duration += seconds;
                pending = 0;
            }
        }
    }
    if (!header || !list->count || variant || pending) return fail(err, cap, "Неполный или пустой список HLS");
    return 0;
}

static int valid_asc(const unsigned char asc[2])
{
    unsigned object = asc[0] >> 3;
    unsigned freq = ((asc[0] & 7u) << 1) | (asc[1] >> 7);
    unsigned channels = (asc[1] >> 3) & 15u;
    return object == 2 && freq <= 12 && (channels == 1 || channels == 2) && !(asc[1] & 7);
}

typedef struct { unsigned tag; size_t payload, end; } Descriptor;

static int descriptor(const unsigned char *data, size_t end, size_t *pos, Descriptor *d)
{
    size_t size = 0;
    unsigned i, byte = 0;
    if (*pos >= end) return -1;
    d->tag = data[(*pos)++];
    for (i = 0; i < 4; ++i) {
        if (*pos >= end) return -1;
        byte = data[(*pos)++];
        size = (size << 7) | (byte & 127u);
        if (!(byte & 128u)) break;
    }
    if (byte & 128u || size > end - *pos) return -1;
    d->payload = *pos;
    d->end = *pos + size;
    *pos = d->end;
    return 0;
}

static int parse_esds(const unsigned char *data, Box b, unsigned char asc[2])
{
    Descriptor es, decoder, child;
    size_t p, q;
    unsigned flags;
    int found = 0;
    if (b.end - b.payload < 4 || be32(data + b.payload) != 0) return -1;
    p = b.payload + 4;
    if (descriptor(data, b.end, &p, &es) < 0 || es.tag != 3 || p != b.end || es.end - es.payload < 3) return -1;
    q = es.payload + 3;
    flags = data[es.payload + 2];
    if (flags & 128) { if (es.end - q < 2) return -1; q += 2; }
    if (flags & 64) {
        unsigned n;
        if (q == es.end) return -1;
        n = data[q++];
        if (n > es.end - q) return -1;
        q += n;
    }
    if (flags & 32) { if (es.end - q < 2) return -1; q += 2; }
    if (descriptor(data, es.end, &q, &decoder) < 0 || decoder.tag != 4 || decoder.end - decoder.payload < 13) return -1;
    if (data[decoder.payload] != 0x40 || (data[decoder.payload + 1] >> 2) != 5) return -1;
    p = decoder.payload + 13;
    while (p < decoder.end) {
        if (descriptor(data, decoder.end, &p, &child) < 0) return -1;
        if (child.tag == 5) {
            if (found || child.end - child.payload != 2) return -1;
            asc[0] = data[child.payload]; asc[1] = data[child.payload + 1];
            if (!valid_asc(asc)) return -1;
            found = 1;
        }
    }
    while (q < es.end) if (descriptor(data, es.end, &q, &child) < 0) return -1;
    return found ? 0 : -1;
}

static int parse_stsd(const unsigned char *data, Box stsd, unsigned char asc[2], int *found)
{
    size_t p;
    uint32_t count, i;
    Box entry, child;
    int rc;
    if (stsd.end - stsd.payload < 8 || be32(data + stsd.payload) != 0) return -1;
    count = be32(data + stsd.payload + 4);
    if (!count || count > 64) return -1;
    p = stsd.payload + 8;
    for (i = 0; i < count; ++i) {
        size_t q;
        if (next_box(data, stsd.end, &p, &entry) != 1 || encrypted_box(entry.type)) return -1;
        if (!ISBOX(entry,'m','p','4','a')) continue;
        /* AudioSampleEntry версии 0: 28 байт, затем дочерние боксы. */
        if (*found || count != 1 || entry.end - entry.payload < 28 ||
            data[entry.payload + 8] || data[entry.payload + 9]) return -1;
        q = entry.payload + 28;
        while ((rc = next_box(data, entry.end, &q, &child)) > 0) {
            if (encrypted_box(child.type)) return -1;
            if (ISBOX(child,'e','s','d','s')) {
                if (*found || parse_esds(data, child, asc) < 0) return -1;
                *found = 1;
            }
        }
        if (rc < 0 || !*found) return -1;
    }
    return p == stsd.end ? 0 : -1;
}

static int config_walk(const unsigned char *data, size_t start, size_t end,
                       unsigned depth, unsigned char asc[2], int *found)
{
    static const uint32_t path[] = {
        FOUR('m','o','o','v'), FOUR('t','r','a','k'), FOUR('m','d','i','a'),
        FOUR('m','i','n','f'), FOUR('s','t','b','l'), FOUR('s','t','s','d')
    };
    Box b;
    size_t p = start;
    int rc;
    while ((rc = next_box(data, end, &p, &b)) > 0) {
        if (encrypted_box(b.type)) return -1;
        if (b.type != path[depth]) continue;
        if (depth == 5) {
            if (parse_stsd(data, b, asc, found) < 0) return -1;
        } else if (config_walk(data, b.payload, b.end, depth + 1, asc, found) < 0) return -1;
    }
    return rc < 0 ? -1 : 0;
}

int hls_aac_config(const unsigned char *data, size_t len, unsigned char asc[2], char *err, size_t cap)
{
    unsigned char result[2] = {0, 0};
    int found = 0;
    clear_error(err, cap);
    if (!data || !asc || !len || len > HLS_MAX_MEDIA_BYTES) return fail(err, cap, "Некорректный размер init fMP4");
    if (config_walk(data, 0, len, 0, result, &found) < 0 || !found) return fail(err, cap, "Некорректный, зашифрованный или неподдерживаемый init AAC-LC fMP4");
    memcpy(asc, result, 2);
    return 0;
}

typedef struct {
    size_t start, count, stride;
    uint32_t flags;
    int has_offset;
    int64_t offset;
} Run;

typedef struct {
    Box mdats[128];
    size_t mdat_count, moof_start, base;
    int explicit_base, have_tfhd;
    uint32_t default_size;
    Run runs[128];
    size_t run_count;
} Fragment;

static int parse_tfhd(const unsigned char *data, Box b, Fragment *f)
{
    size_t p = b.payload;
    uint32_t flags;
    if (f->have_tfhd || b.end - p < 8 || data[p] != 0) return -1;
    flags = be32(data + p) & 0xffffffu;
    if (flags & ~0x03003bu || flags & 0x010000u || !be32(data + p + 4)) return -1;
    p += 8;
    f->base = f->moof_start;
    if (flags & 1u) {
        uint64_t base;
        if (b.end - p < 8 || flags & 0x020000u) return -1;
        base = be64(data + p); p += 8;
        if (base > HLS_MAX_MEDIA_BYTES) return -1;
        f->base = (size_t)base;
        f->explicit_base = 1;
    }
    if (flags & 2u) { if (b.end - p < 4 || be32(data + p) != 1) return -1; p += 4; }
    if (flags & 8u) { if (b.end - p < 4) return -1; p += 4; }
    if (flags & 16u) { if (b.end - p < 4) return -1; f->default_size = be32(data + p); p += 4; }
    if (flags & 32u) { if (b.end - p < 4) return -1; p += 4; }
    if (p != b.end) return -1;
    f->have_tfhd = 1;
    return 0;
}

static int parse_trun(const unsigned char *data, Box b, Fragment *f)
{
    Run *r;
    size_t p = b.payload;
    uint32_t flags, raw;
    if (f->run_count == 128 || b.end - p < 8 || data[p] > 1) return -1;
    flags = be32(data + p) & 0xffffffu;
    if (flags & ~0x000f05u || ((flags & 4u) && (flags & 0x400u))) return -1;
    r = &f->runs[f->run_count++];
    memset(r, 0, sizeof *r);
    r->flags = flags;
    r->count = be32(data + p + 4);
    if (!r->count || r->count > 131072) return -1;
    p += 8;
    if (flags & 1u) {
        if (b.end - p < 4) return -1;
        raw = be32(data + p); p += 4;
        r->offset = raw <= INT32_MAX ? (int64_t)raw : (int64_t)raw - INT64_C(4294967296);
        r->has_offset = 1;
    }
    if (flags & 4u) { if (b.end - p < 4) return -1; p += 4; }
    r->stride = ((flags & 0x100u) ? 4u : 0u) + ((flags & 0x200u) ? 4u : 0u) +
                ((flags & 0x400u) ? 4u : 0u) + ((flags & 0x800u) ? 4u : 0u);
    if (r->count * r->stride != b.end - p) return -1;
    r->start = p;
    return 0;
}

static int parse_fragment(const unsigned char *data, size_t len, Fragment *f)
{
    Box b, moof = {0}, traf = {0}, child;
    size_t p = 0, q, z;
    int rc, found = 0, have_traf = 0;
    memset(f, 0, sizeof *f);
    while ((rc = next_box(data, len, &p, &b)) > 0) {
        if (encrypted_box(b.type)) return -1;
        if (ISBOX(b,'m','o','o','f')) {
            if (found++) return -1;
            moof = b;
            f->moof_start = b.start;
        } else if (ISBOX(b,'m','d','a','t')) {
            if (f->mdat_count == 128) return -1;
            f->mdats[f->mdat_count++] = b;
        }
    }
    if (rc < 0 || !found || !f->mdat_count) return -1;
    q = moof.payload;
    while ((rc = next_box(data, moof.end, &q, &b)) > 0) {
        if (encrypted_box(b.type)) return -1;
        if (ISBOX(b,'t','r','a','f')) {
            if (have_traf++) return -1;
            traf = b;
        }
    }
    if (rc < 0 || !have_traf) return -1;
    z = traf.payload;
    while ((rc = next_box(data, traf.end, &z, &child)) > 0) {
        if (encrypted_box(child.type) || ISBOX(child,'s','g','p','d') || ISBOX(child,'s','b','g','p')) return -1;
        if (ISBOX(child,'t','f','h','d')) { if (parse_tfhd(data, child, f) < 0) return -1; }
        else if (ISBOX(child,'t','r','u','n')) { if (parse_trun(data, child, f) < 0) return -1; }
    }
    return rc < 0 || !f->have_tfhd || !f->run_count ? -1 : 0;
}

static int within_mdat(const Fragment *f, size_t pos, size_t n)
{
    size_t i;
    for (i = 0; i < f->mdat_count; ++i) {
        Box b = f->mdats[i];
        if (pos >= b.payload && pos <= b.end && n <= b.end - pos) return 1;
    }
    return 0;
}

static int adts_write(FILE *out, const unsigned char asc[2], const unsigned char *data, size_t n)
{
    unsigned freq = ((asc[0] & 7u) << 1) | (asc[1] >> 7);
    unsigned channels = (asc[1] >> 3) & 15u;
    unsigned frame = (unsigned)n + 7u;
    unsigned char h[7] = {0xff, 0xf1, 0, 0, 0, 0, 0xfc};
    h[2] = (unsigned char)(0x40u | (freq << 2) | (channels >> 2));
    h[3] = (unsigned char)((channels << 6) | (frame >> 11));
    h[4] = (unsigned char)(frame >> 3);
    h[5] = (unsigned char)((frame << 5) | 0x1fu);
    return fwrite(h, 1, 7, out) == 7 && fwrite(data, 1, n, out) == n ? 0 : -1;
}

static int fragment_samples(const unsigned char *data, size_t len, const Fragment *f,
                            const unsigned char asc[2], FILE *out)
{
    size_t i, j, pos = 0, total = 0;
    for (i = 0; i < f->run_count; ++i) {
        const Run *r = &f->runs[i];
        size_t entry = r->start;
        if (r->has_offset) {
            int64_t absolute = (int64_t)f->base + r->offset;
            if (absolute < 0 || (uint64_t)absolute > len) return -1;
            pos = (size_t)absolute;
        } else if (!i) {
            if (!f->explicit_base) return -1;
            pos = f->base;
        }
        if (r->count > 131072 - total) return -1;
        total += r->count;
        for (j = 0; j < r->count; ++j, entry += r->stride) {
            uint32_t size = f->default_size;
            if (r->flags & 0x200u) size = be32(data + entry + ((r->flags & 0x100u) ? 4 : 0));
            if (!size || size > 8184 || !within_mdat(f, pos, size)) return -1;
            if (out && adts_write(out, asc, data + pos, size) < 0) return -2;
            pos += size;
        }
    }
    return 0;
}

int hls_aac_segment(const unsigned char *data, size_t len, const unsigned char asc[2],
                    FILE *out, char *err, size_t cap)
{
    Fragment fragment;
    clear_error(err, cap);
    if (!data || !asc || !out || !len || len > HLS_MAX_MEDIA_BYTES || !valid_asc(asc)) return fail(err, cap, "Некорректные аргументы сегмента AAC");
    if (parse_fragment(data, len, &fragment) < 0 || fragment_samples(data, len, &fragment, asc, NULL) < 0) return fail(err, cap, "Повреждённый, зашифрованный или неподдерживаемый сегмент fMP4");
    if (fragment_samples(data, len, &fragment, asc, out) < 0) return fail(err, cap, "Ошибка записи ADTS");
    return 0;
}

typedef struct {
    unsigned pid, cc;
    int start, discontinuity;
    const unsigned char *payload;
    size_t size;
} TsPacket;

static int ts_packet(const unsigned char *data, TsPacket *packet)
{
    unsigned control;
    size_t p = 4;
    if (data[0] != 0x47 || data[1] & 0x80 || data[3] & 0xc0) return -1;
    memset(packet, 0, sizeof *packet);
    packet->pid = ((data[1] & 31u) << 8) | data[2];
    packet->start = !!(data[1] & 0x40);
    packet->cc = data[3] & 15u;
    control = (data[3] >> 4) & 3u;
    if (!control) return -1;
    if (control & 2u) {
        size_t n = data[p++];
        if (n > 188 - p) return -1;
        if (n) packet->discontinuity = !!(data[p] & 0x80);
        p += n;
        if (control == 2 && p != 188) return -1;
    }
    if (control & 1u) {
        if (p >= 188) return -1;
        packet->payload = data + p;
        packet->size = 188 - p;
    }
    return 0;
}

typedef struct {
    unsigned char bytes[1024];
    size_t used, expected;
    int cc;
} Psi;

typedef struct {
    unsigned program, pmt_pid, audio_pid, codec;
    int pat, pmt;
} TsTracks;

static uint32_t psi_crc(const unsigned char *data, size_t n)
{
    uint32_t crc = 0xffffffffu;
    size_t i;
    unsigned b;
    for (i = 0; i < n; ++i) {
        crc ^= (uint32_t)data[i] << 24;
        for (b = 0; b < 8; ++b) crc = (crc << 1) ^ ((crc & 0x80000000u) ? 0x04c11db7u : 0);
    }
    return crc;
}

/* CA_descriptor объявляет условный доступ: даже нескремблированный
 * текущий пакет не означает, что дорожка пригодна для открытого декодера. */
static int pmt_descriptors(const unsigned char *d, size_t n)
{
    size_t p = 0;
    while (p < n) {
        size_t length;
        if (n - p < 2 || d[p] == 9) return -1;
        length = d[p + 1];
        p += 2;
        if (length > n - p) return -1;
        p += length;
    }
    return 0;
}

static int psi_table(const unsigned char *d, size_t n, int is_pmt, TsTracks *tracks)
{
    size_t p, end = n - 4;
    unsigned pid, program;
    if (n < 12 || !(d[1] & 0x80) || !(d[5] & 1) || d[6] || d[7] || psi_crc(d, n)) return -1;
    if (!is_pmt) {
        if (d[0] != 0 || (end - 8) % 4) return -1;
        for (p = 8; p < end; p += 4) {
            program = ((unsigned)d[p] << 8) | d[p + 1];
            pid = ((d[p + 2] & 31u) << 8) | d[p + 3];
            if (!program) continue;
            if (!pid || pid == 8191) return -1;
            if (tracks->pat && (tracks->program != program || tracks->pmt_pid != pid)) return -1;
            tracks->program = program; tracks->pmt_pid = pid; tracks->pat = 1;
            return 0;
        }
        return -1;
    }
    if (d[0] != 2 || n < 16 || (((unsigned)d[3] << 8) | d[4]) != tracks->program) return -1;
    p = 12 + (((d[10] & 15u) << 8) | d[11]);
    if (p > end || pmt_descriptors(d + 12, p - 12) < 0) return -1;
    pid = 0;
    program = 0;
    while (p < end) {
        unsigned type, current_pid;
        size_t info;
        if (end - p < 5) return -1;
        type = d[p];
        current_pid = ((d[p + 1] & 31u) << 8) | d[p + 2];
        info = ((d[p + 3] & 15u) << 8) | d[p + 4];
        p += 5;
        if (info > end - p || pmt_descriptors(d + p, info) < 0) return -1;
        if (!pid && (type == 3 || type == 4 || type == 15)) { pid = current_pid; program = type; }
        p += info;
    }
    if (!pid || pid == 8191 || pid == tracks->pmt_pid) return -1;
    if (tracks->pmt && (tracks->audio_pid != pid || tracks->codec != program)) return -1;
    tracks->audio_pid = pid; tracks->codec = program; tracks->pmt = 1;
    return 0;
}

static int psi_feed(Psi *psi, const unsigned char *p, size_t n, int is_pmt, TsTracks *tracks)
{
    while (n) {
        size_t take, wanted;
        if (!psi->used && *p == 0xff) {
            while (n--) if (*p++ != 0xff) return -1;
            return 0;
        }
        wanted = psi->used < 3 ? 3 : psi->expected;
        take = wanted - psi->used;
        if (take > n) take = n;
        memcpy(psi->bytes + psi->used, p, take);
        psi->used += take; p += take; n -= take;
        if (psi->used == 3 && !psi->expected) {
            psi->expected = 3 + (((psi->bytes[1] & 15u) << 8) | psi->bytes[2]);
            if (psi->expected < 12 || psi->expected > sizeof psi->bytes) return -1;
        }
        if (psi->used == psi->expected) {
            if (psi_table(psi->bytes, psi->used, is_pmt, tracks) < 0) return -1;
            psi->used = psi->expected = 0;
        }
    }
    return 0;
}

static int psi_packet(Psi *psi, const TsPacket *packet, int is_pmt, TsTracks *tracks)
{
    const unsigned char *p = packet->payload;
    size_t n = packet->size;
    if (packet->discontinuity) { psi->cc = -1; psi->used = psi->expected = 0; }
    if (!n) return 0;
    if (psi->cc >= 0 && packet->cc != ((unsigned)psi->cc + 1u) % 16u) return -1;
    psi->cc = (int)packet->cc;
    if (packet->start) {
        size_t pointer = *p++;
        --n;
        if (pointer > n) return -1;
        if (psi->used && (psi_feed(psi, p, pointer, is_pmt, tracks) < 0 || psi->used)) return -1;
        p += pointer; n -= pointer;
        return psi_feed(psi, p, n, is_pmt, tracks);
    }
    return psi->used ? psi_feed(psi, p, n, is_pmt, tracks) : 0;
}

static int ts_tracks(const unsigned char *data, size_t len, TsTracks *tracks)
{
    Psi pat = {{0}, 0, 0, -1}, pmt = {{0}, 0, 0, -1};
    size_t i;
    memset(tracks, 0, sizeof *tracks);
    for (i = 0; i < len; i += 188) {
        TsPacket packet;
        if (ts_packet(data + i, &packet) < 0) return -1;
        if (!packet.pid) {
            if (psi_packet(&pat, &packet, 0, tracks) < 0) return -1;
        } else if (tracks->pat && packet.pid == tracks->pmt_pid) {
            if (psi_packet(&pmt, &packet, 1, tracks) < 0) return -1;
        }
    }
    return tracks->pat && tracks->pmt && !pat.used && !pmt.used ? 0 : -1;
}

typedef struct {
    unsigned char header[264];
    size_t used, header_size, remaining;
    int active, ready, unlimited;
} Pes;

static int pes_feed(Pes *pes, const unsigned char *p, size_t n, FILE *out, size_t *audio_bytes)
{
    while (n && !pes->ready) {
        size_t wanted = pes->used < 9 ? 9 : pes->header_size;
        size_t take = wanted - pes->used;
        if (take > n) take = n;
        memcpy(pes->header + pes->used, p, take);
        pes->used += take; p += take; n -= take;
        if (pes->used == 9 && !pes->header_size) {
            unsigned packet_length = ((unsigned)pes->header[4] << 8) | pes->header[5];
            if (pes->header[0] || pes->header[1] || pes->header[2] != 1 ||
                !((pes->header[3] >= 0xc0 && pes->header[3] <= 0xdf) || pes->header[3] == 0xbd) ||
                (pes->header[6] & 0xf0) != 0x80) return -1;
            pes->header_size = 9 + pes->header[8];
            if (packet_length && packet_length < pes->header_size - 6) return -1;
            pes->unlimited = !packet_length;
            pes->remaining = packet_length ? packet_length - (pes->header_size - 6) : 0;
        }
        if (pes->header_size && pes->used == pes->header_size) pes->ready = 1;
    }
    if (n && pes->ready) {
        size_t take = pes->unlimited || n <= pes->remaining ? n : pes->remaining;
        if (out && fwrite(p, 1, take, out) != take) return -2;
        *audio_bytes += take;
        if (!pes->unlimited) {
            pes->remaining -= take;
            p += take; n -= take;
            while (n--) if (*p++ != 0xff) return -1;
        }
    }
    return 0;
}

static int ts_pes(const unsigned char *data, size_t len, unsigned pid, FILE *out)
{
    Pes pes = {{0}, 0, 0, 0, 0, 0, 0};
    size_t i, audio_bytes = 0;
    int cc = -1;
    for (i = 0; i < len; i += 188) {
        TsPacket packet;
        int rc;
        if (ts_packet(data + i, &packet) < 0) return -1;
        if (packet.pid != pid) continue;
        if (packet.discontinuity) return -1;
        if (!packet.size) continue;
        if (cc >= 0 && packet.cc != ((unsigned)cc + 1u) % 16u) return -1;
        cc = (int)packet.cc;
        if (packet.start) {
            if (pes.active && (!pes.ready || (!pes.unlimited && pes.remaining))) return -1;
            memset(&pes, 0, sizeof pes);
            pes.active = 1;
        }
        if (!pes.active) return -1;
        rc = pes_feed(&pes, packet.payload, packet.size, out, &audio_bytes);
        if (rc < 0) return rc;
    }
    return audio_bytes && pes.ready && (pes.unlimited || !pes.remaining) ? 0 : -1;
}

static int direct_adts(const unsigned char *data, size_t len)
{
    size_t p = 0;
    while (p < len) {
        unsigned freq, channels, frame, header;
        const unsigned char *d = data + p;
        if (len - p < 7 || d[0] != 0xff || (d[1] & 0xf6) != 0xf0 || (d[2] >> 6) != 1) return -1;
        freq = (d[2] >> 2) & 15u;
        channels = ((d[2] & 1u) << 2) | (d[3] >> 6);
        frame = ((d[3] & 3u) << 11) | ((unsigned)d[4] << 3) | (d[5] >> 5);
        header = d[1] & 1 ? 7 : 9;
        if (freq > 12 || !channels || channels > 2 || (d[6] & 3) || frame <= header || frame > len - p) return -1;
        p += frame;
    }
    return p ? 0 : -1;
}

int hls_ts_audio(const unsigned char *data, size_t len, FILE *out, char *err, size_t cap)
{
    TsTracks tracks;
    clear_error(err, cap);
    if (!data || !out || !len || len > HLS_MAX_MEDIA_BYTES) return fail(err, cap, "Некорректные аргументы TS/ADTS");
    if (data[0] == 0xff) {
        if (direct_adts(data, len) < 0) return fail(err, cap, "Повреждённый или неподдерживаемый прямой ADTS");
        if (fwrite(data, 1, len, out) != len) return fail(err, cap, "Ошибка записи ADTS");
        return 0;
    }
    if (len % 188 || ts_tracks(data, len, &tracks) < 0) return fail(err, cap, "Повреждённый/зашифрованный TS либо отсутствует поддерживаемый CODEC PAT/PMT");
    if (ts_pes(data, len, tracks.audio_pid, NULL) < 0) return fail(err, cap, "Повреждённый или неполный PES аудиодорожки");
    if (ts_pes(data, len, tracks.audio_pid, out) < 0) return fail(err, cap, "Ошибка записи аудиодорожки TS");
    return 0;
}
