#ifndef SOUNDYA_HLS_H
#define SOUNDYA_HLS_H

#include <stddef.h>
#include <stdio.h>

#define HLS_MAX_SEGMENTS 1024
#define HLS_URL_CAP 4096
#define HLS_MAX_MEDIA_BYTES (16u * 1024u * 1024u)

typedef struct {
    char init_url[4096];
    char segments[1024][4096];
    size_t count;
    int encrypted;
    double duration;
} HlsList;

/* Все функции: 0 — успех, -1 — ошибка; err может быть NULL при cap == 0.
 * HlsList лучше размещать в куче: он занимает около 4 МиБ.
 * Master: count == 1, duration == 0; вызывающий код следует segments[0]
 * с собственным ограничением глубины. EXTINF должен быть положительным,
 * иначе duration == 0 неоднозначен. Текст ограничен 16 МиБ.
 * BYTERANGE и шифрование не поддержаны.
 */
int hls_parse(const char *text, const char *base_url, HlsList *list,
              char *err, size_t cap);

/* Только стандартный mp4a/esds и двухбайтовый ASC AAC-LC, mono/stereo.
 * Не поддержаны HE-AAC, нестандартные sample entry и зашифрованные дорожки.
 */
int hls_aac_config(const unsigned char *data, size_t len,
                   unsigned char asc[2], char *err, size_t cap);

/* Один moof, один traf, до 128 trun. Начальная позиция данных задаётся
 * trun.data_offset либо tfhd.base_data_offset; дальнейшие trun могут
 * продолжать предыдущий. trex не угадывается: размеры нужны в tfhd/trun.
 * Фрейм AAC ограничен длиной ADTS (8184 байта полезных данных).
 * До записи проводится полная проверка. Ошибка fwrite может оставить
 * неполный вывод: вызывающий код должен отбросить такой временный файл.
 */
int hls_aac_segment(const unsigned char *data, size_t len,
                    const unsigned char asc[2], FILE *out,
                    char *err, size_t cap);

/* MPEG-TS 188 байт: PAT/PMT, первый MPEG audio (0x03/04) или ADTS AAC
 * (0x0f), снятие PES. Возвращает исходный elementary stream, не PCM.
 * Также принимает целиком валидный прямой ADTS. LATM/AC3 и шифрование
 * отклоняются. PAT/PMT должны присутствовать в данном сегменте.
 */
int hls_ts_audio(const unsigned char *data, size_t len, FILE *out,
                 char *err, size_t cap);

#endif
