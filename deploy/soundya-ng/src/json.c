#include "json.h"

#include <limits.h>
#include <stdint.h>
#include <string.h>

typedef struct {
    JDoc *doc;
    const char *s;
    size_t length, pos;
} Parser;

static int hex_digit(unsigned char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static int hex4(const char *s, size_t end, size_t *pos, uint32_t *value)
{
    uint32_t n = 0;
    int i;
    if (*pos > end || end - *pos < 4) return -1;
    for (i = 0; i < 4; ++i) {
        int h = hex_digit((unsigned char)s[*pos + (size_t)i]);
        if (h < 0) return -1;
        n = (n << 4) | (uint32_t)h;
    }
    *pos += 4;
    *value = n;
    return 0;
}

/* Один скаляр UTF-8: без overlong, суррогатов и значений выше U+10FFFF. */
static int utf8_read(const char *s, size_t end, size_t *pos, uint32_t *value)
{
    size_t p = *pos;
    uint32_t n, minimum;
    unsigned char first;
    int extra, i;
    if (p >= end) return -1;
    first = (unsigned char)s[p++];
    if (first < 0x80) {
        *pos = p;
        *value = first;
        return 0;
    }
    if (first >= 0xc2 && first <= 0xdf) {
        extra = 1; n = first & 0x1fu; minimum = 0x80;
    } else if (first >= 0xe0 && first <= 0xef) {
        extra = 2; n = first & 0x0fu; minimum = 0x800;
    } else if (first >= 0xf0 && first <= 0xf4) {
        extra = 3; n = first & 0x07u; minimum = 0x10000;
    } else {
        return -1;
    }
    if (end - p < (size_t)extra) return -1;
    for (i = 0; i < extra; ++i) {
        unsigned char c = (unsigned char)s[p++];
        if ((c & 0xc0u) != 0x80u) return -1;
        n = (n << 6) | (c & 0x3fu);
    }
    if (n < minimum || n > 0x10ffff || (n >= 0xd800 && n <= 0xdfff)) return -1;
    *pos = p;
    *value = n;
    return 0;
}

/* Один декодированный скаляр внутри JSON-строки, без внешних кавычек. */
static int string_unit(const char *s, size_t end, size_t *pos, uint32_t *value)
{
    size_t p = *pos;
    uint32_t n, low;
    unsigned char c;
    if (p >= end) return -1;
    c = (unsigned char)s[p];
    if (c != '\\') {
        if (c < 0x20 || c == '"') return -1;
        return utf8_read(s, end, pos, value);
    }
    if (end - p < 2) return -1;
    c = (unsigned char)s[p + 1];
    p += 2;
    switch (c) {
    case '"': n = '"'; break;
    case '\\': n = '\\'; break;
    case '/': n = '/'; break;
    case 'b': n = '\b'; break;
    case 'f': n = '\f'; break;
    case 'n': n = '\n'; break;
    case 'r': n = '\r'; break;
    case 't': n = '\t'; break;
    case 'u':
        if (hex4(s, end, &p, &n) < 0) return -1;
        if (n >= 0xd800 && n <= 0xdbff) {
            if (end - p < 2 || s[p] != '\\' || s[p + 1] != 'u') return -1;
            p += 2;
            if (hex4(s, end, &p, &low) < 0 || low < 0xdc00 || low > 0xdfff) return -1;
            n = 0x10000u + ((n - 0xd800u) << 10) + (low - 0xdc00u);
        } else if (n >= 0xdc00 && n <= 0xdfff) {
            return -1;
        }
        break;
    default: return -1;
    }
    *pos = p;
    *value = n;
    return 0;
}

static size_t utf8_write(uint32_t n, char out[4])
{
    if (n < 0x80) {
        out[0] = (char)n;
        return 1;
    }
    if (n < 0x800) {
        out[0] = (char)(0xc0u | (n >> 6));
        out[1] = (char)(0x80u | (n & 0x3fu));
        return 2;
    }
    if (n < 0x10000) {
        out[0] = (char)(0xe0u | (n >> 12));
        out[1] = (char)(0x80u | ((n >> 6) & 0x3fu));
        out[2] = (char)(0x80u | (n & 0x3fu));
        return 3;
    }
    out[0] = (char)(0xf0u | (n >> 18));
    out[1] = (char)(0x80u | ((n >> 12) & 0x3fu));
    out[2] = (char)(0x80u | ((n >> 6) & 0x3fu));
    out[3] = (char)(0x80u | (n & 0x3fu));
    return 4;
}

static void skip_space(Parser *p)
{
    while (p->pos < p->length) {
        char c = p->s[p->pos];
        if (c != ' ' && c != '\t' && c != '\r' && c != '\n') break;
        ++p->pos;
    }
}

static int add_token(Parser *p, int kind, int parent, size_t start, size_t end)
{
    int index;
    JToken *t;
    if (p->doc->count >= J_MAX_TOKENS) return -1;
    index = p->doc->count++;
    t = &p->doc->tokens[index];
    t->source = p->s;
    t->start = start;
    t->end = end;
    t->parent = parent;
    t->kind = kind;
    return index;
}

static int parse_string(Parser *p, int parent)
{
    size_t start;
    uint32_t ignored;
    int index;
    if (p->pos >= p->length || p->s[p->pos] != '"') return -1;
    start = ++p->pos;
    while (p->pos < p->length && p->s[p->pos] != '"') {
        if (string_unit(p->s, p->length, &p->pos, &ignored) < 0) return -1;
    }
    if (p->pos >= p->length) return -1;
    index = add_token(p, J_STRING, parent, start, p->pos);
    ++p->pos;
    return index;
}

static int digit(char c)
{
    return c >= '0' && c <= '9';
}

static int parse_number(Parser *p, int parent)
{
    size_t start = p->pos;
    if (p->s[p->pos] == '-') ++p->pos;
    if (p->pos >= p->length) return -1;
    if (p->s[p->pos] == '0') {
        ++p->pos;
        if (p->pos < p->length && digit(p->s[p->pos])) return -1;
    } else {
        if (p->s[p->pos] < '1' || p->s[p->pos] > '9') return -1;
        do { ++p->pos; } while (p->pos < p->length && digit(p->s[p->pos]));
    }
    if (p->pos < p->length && p->s[p->pos] == '.') {
        ++p->pos;
        if (p->pos >= p->length || !digit(p->s[p->pos])) return -1;
        do { ++p->pos; } while (p->pos < p->length && digit(p->s[p->pos]));
    }
    if (p->pos < p->length && (p->s[p->pos] == 'e' || p->s[p->pos] == 'E')) {
        ++p->pos;
        if (p->pos < p->length && (p->s[p->pos] == '+' || p->s[p->pos] == '-')) ++p->pos;
        if (p->pos >= p->length || !digit(p->s[p->pos])) return -1;
        do { ++p->pos; } while (p->pos < p->length && digit(p->s[p->pos]));
    }
    return add_token(p, J_NUMBER, parent, start, p->pos);
}

static int parse_value(Parser *p, int parent, int depth);

static int parse_container(Parser *p, int parent, int depth, int kind)
{
    int index;
    char closing = kind == J_OBJECT ? '}' : ']';
    if (depth >= J_MAX_DEPTH) return -1;
    index = add_token(p, kind, parent, p->pos, p->pos);
    if (index < 0) return -1;
    ++p->pos;
    skip_space(p);
    if (p->pos < p->length && p->s[p->pos] == closing) {
        p->doc->tokens[index].end = ++p->pos;
        return index;
    }
    for (;;) {
        if (kind == J_OBJECT) {
            if (parse_string(p, index) < 0) return -1;
            skip_space(p);
            if (p->pos >= p->length || p->s[p->pos] != ':') return -1;
            ++p->pos;
        }
        if (parse_value(p, index, depth + 1) < 0) return -1;
        skip_space(p);
        if (p->pos >= p->length) return -1;
        if (p->s[p->pos] == closing) {
            p->doc->tokens[index].end = ++p->pos;
            return index;
        }
        if (p->s[p->pos] != ',') return -1;
        ++p->pos;
        skip_space(p);
        /* После запятой обязательно значение/ключ, а не закрывающая скобка. */
    }
}

static int parse_value(Parser *p, int parent, int depth)
{
    const char *literal;
    size_t size, start;
    int kind;
    skip_space(p);
    if (p->pos >= p->length) return -1;
    switch (p->s[p->pos]) {
    case '{': return parse_container(p, parent, depth, J_OBJECT);
    case '[': return parse_container(p, parent, depth, J_ARRAY);
    case '"': return parse_string(p, parent);
    case 't': literal = "true"; size = 4; kind = J_TRUE; break;
    case 'f': literal = "false"; size = 5; kind = J_FALSE; break;
    case 'n': literal = "null"; size = 4; kind = J_NULL; break;
    default:
        if (p->s[p->pos] == '-' || digit(p->s[p->pos])) return parse_number(p, parent);
        return -1;
    }
    if (p->length - p->pos < size || memcmp(p->s + p->pos, literal, size) != 0) return -1;
    start = p->pos;
    p->pos += size;
    return add_token(p, kind, parent, start, p->pos);
}

int j_parse(JDoc *doc, const char *source)
{
    Parser p;
    if (!doc) return -1;
    doc->source = source;
    doc->count = 0;
    if (!source) return -1;
    p.doc = doc;
    p.s = source;
    p.length = strlen(source);
    p.pos = 0;
    if (parse_value(&p, -1, 0) < 0) {
        doc->count = 0;
        return -1;
    }
    skip_space(&p);
    if (p.pos != p.length) {
        doc->count = 0;
        return -1;
    }
    return 0;
}

static const JToken *get_token(const JDoc *doc, int index, int kind)
{
    const JToken *t;
    if (!doc || !doc->source || doc->count < 1 || doc->count > J_MAX_TOKENS ||
        index < 0 || index >= doc->count) return NULL;
    t = &doc->tokens[index];
    if (t->source != doc->source || t->start > t->end || (kind && t->kind != kind)) return NULL;
    return t;
}

static int key_matches(const JToken *t, const char *key)
{
    size_t pos = t->start, k = 0;
    while (pos < t->end) {
        uint32_t cp;
        char bytes[4];
        size_t n, i;
        if (string_unit(t->source, t->end, &pos, &cp) < 0) return 0;
        n = utf8_write(cp, bytes);
        for (i = 0; i < n; ++i) {
            if (!key[k] || (unsigned char)key[k] != (unsigned char)bytes[i]) return 0;
            ++k;
        }
    }
    return key[k] == '\0';
}

int j_find(const JDoc *doc, int obj, const char *key)
{
    int i, pending = -1, result = -1;
    if (!key || !get_token(doc, obj, J_OBJECT)) return -1;
    for (i = obj + 1; i < doc->count; ++i) {
        const JToken *t = &doc->tokens[i];
        if (t->parent < obj) break;
        if (t->parent != obj) continue;
        if (pending < 0) {
            if (t->kind != J_STRING) return -1;
            pending = i;
        } else {
            if (key_matches(&doc->tokens[pending], key)) result = i;
            pending = -1;
        }
    }
    return result;
}

int j_at(const JDoc *doc, int arr, int index)
{
    int i;
    if (index < 0 || !get_token(doc, arr, J_ARRAY)) return -1;
    for (i = arr + 1; i < doc->count; ++i) {
        if (doc->tokens[i].parent < arr) break;
        if (doc->tokens[i].parent == arr && index-- == 0) return i;
    }
    return -1;
}

int j_count(const JDoc *doc, int arr)
{
    int i, count = 0;
    if (!get_token(doc, arr, J_ARRAY)) return -1;
    for (i = arr + 1; i < doc->count; ++i) {
        if (doc->tokens[i].parent < arr) break;
        if (doc->tokens[i].parent == arr) ++count;
    }
    return count;
}

int j_string(const JDoc *doc, int tok, char *out, size_t cap)
{
    const JToken *t;
    size_t pos, used = 0;
    if (out && cap) out[0] = '\0';
    t = get_token(doc, tok, J_STRING);
    if (!t || !out || !cap) return -1;
    pos = t->start;
    while (pos < t->end) {
        uint32_t cp;
        char bytes[4];
        size_t n;
        if (string_unit(t->source, t->end, &pos, &cp) < 0) goto failed;
        n = utf8_write(cp, bytes);
        if (n > (size_t)INT_MAX - used || n >= cap - used) goto failed;
        memcpy(out + used, bytes, n);
        used += n;
    }
    out[used] = '\0';
    return (int)used;
failed:
    out[0] = '\0';
    return -1;
}

long long j_int(const JDoc *doc, int tok, long long fallback)
{
    const JToken *t = get_token(doc, tok, J_NUMBER);
    unsigned long long n = 0, limit;
    size_t pos;
    int negative;
    if (!t || t->start == t->end) return fallback;
    pos = t->start;
    negative = t->source[pos] == '-';
    if (negative) ++pos;
    if (pos == t->end) return fallback;
    limit = negative ? (unsigned long long)(-(LLONG_MIN + 1)) + 1ULL : (unsigned long long)LLONG_MAX;
    for (; pos < t->end; ++pos) {
        unsigned int d;
        if (!digit(t->source[pos])) return fallback;
        d = (unsigned int)(t->source[pos] - '0');
        if (n > (limit - d) / 10ULL) return fallback;
        n = n * 10ULL + d;
    }
    if (!negative) return (long long)n;
    if (n == limit) return LLONG_MIN;
    return -(long long)n;
}

int j_bool(const JDoc *doc, int tok, int fallback)
{
    const JToken *t = get_token(doc, tok, 0);
    if (t && t->kind == J_TRUE) return 1;
    if (t && t->kind == J_FALSE) return 0;
    return fallback;
}

/* Измерение выполняется перед записью: неверный ввод не оставляет JSON-фрагмент. */
static int quote_pass(const char *text, size_t length, char *out, size_t *written)
{
    static const char hex[] = "0123456789abcdef";
    size_t pos = 0, used = 1;
    if (out) out[0] = '"';
    while (pos < length) {
        size_t start = pos, n;
        uint32_t cp;
        char bytes[6];
        if (utf8_read(text, length, &pos, &cp) < 0) return -1;
        if (cp == '"' || cp == '\\') {
            bytes[0] = '\\'; bytes[1] = (char)cp; n = 2;
        } else if (cp == '\b' || cp == '\f' || cp == '\n' || cp == '\r' || cp == '\t') {
            bytes[0] = '\\';
            bytes[1] = cp == '\b' ? 'b' : cp == '\f' ? 'f' : cp == '\n' ? 'n' : cp == '\r' ? 'r' : 't';
            n = 2;
        } else if (cp < 0x20) {
            bytes[0] = '\\'; bytes[1] = 'u'; bytes[2] = '0'; bytes[3] = '0';
            bytes[4] = hex[cp >> 4]; bytes[5] = hex[cp & 0xfu]; n = 6;
        } else {
            n = pos - start;
            memcpy(bytes, text + start, n);
        }
        if (n > (size_t)INT_MAX - used) return -1;
        if (out) memcpy(out + used, bytes, n);
        used += n;
    }
    if (used == (size_t)INT_MAX) return -1;
    if (out) {
        out[used] = '"';
        out[used + 1] = '\0';
    }
    *written = used + 1;
    return 0;
}

int j_quote(const char *text, char *out, size_t cap)
{
    size_t length, needed;
    if (out && cap) out[0] = '\0';
    if (!text || !out || !cap) return -1;
    length = strlen(text);
    if (quote_pass(text, length, NULL, &needed) < 0 || needed >= cap) return -1;
    if (quote_pass(text, length, out, &needed) < 0) {
        out[0] = '\0';
        return -1;
    }
    return (int)needed;
}
