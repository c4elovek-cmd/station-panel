#ifndef SOUNDYA_JSON_H
#define SOUNDYA_JSON_H

#include <stddef.h>

#define J_MAX_TOKENS 8192
#define J_MAX_DEPTH 32

enum {
    J_OBJECT = 1,
    J_ARRAY = 2,
    J_STRING = 3,
    J_NUMBER = 4,
    J_TRUE = 5,
    J_FALSE = 6,
    J_NULL = 7
};

typedef struct {
    const char *source;
    size_t start, end;
    int parent, kind;
} JToken;

typedef struct {
    const char *source;
    JToken tokens[8192];
    int count;
} JDoc;

/* Вход — неизменяемая строка с завершающим NUL, живущая дольше документа.
 * Успех: 0, count >= 1, корень tokens[0], parent корня = -1.
 * Ошибка: -1, count = 0. Без выделения памяти; до 32 вложенных контейнеров.
 * start включён, end исключён. У строк кавычки не входят в диапазон.
 * У объектов и массивов диапазон включает открывающий и закрывающий символ.
 * Ключи и значения объекта имеют parent объекта и идут попарно.
 * Проверяются синтаксис, числа, UTF-8 и Unicode-escape, включая суррогаты.
 */
int j_parse(JDoc *doc, const char *source);

/* Поиск по декодированному ключу UTF-8. Повторный ключ: последнее значение.
 * Некорректный объект/отсутствующий ключ: -1. */
int j_find(const JDoc *doc, int obj, const char *key);
/* Только массивы: индекс с нуля; ошибочные аргументы возвращают -1. */
int j_at(const JDoc *doc, int arr, int index);
int j_count(const JDoc *doc, int arr);

/* Возвращает длину декодированных байтов без завершающего NUL либо -1.
 * cap включает NUL; при ошибке out[0] = NUL, если буфер доступен.
 * Escape \u0000 создаёт внутренний NUL: ориентироваться на длину результата.
 * Буфер вывода не должен перекрывать исходную строку документа.
 */
int j_string(const JDoc *doc, int tok, char *out, size_t cap);
/* Только целочисленная форма J_NUMBER (без точки/экспоненты), с контролем
 * диапазона long long; неверный тип/форма/переполнение возвращает fallback. */
long long j_int(const JDoc *doc, int tok, long long fallback);
/* Только J_TRUE/J_FALSE; остальные типы возвращают fallback без изменений. */
int j_bool(const JDoc *doc, int tok, int fallback);

/* Кодирует NUL-терминированный UTF-8 как JSON-строку, включая кавычки.
 * Длина включает кавычки, но не завершающий NUL. Ошибка/малый cap: -1,
 * out[0] = NUL, если буфер доступен. Некорректный UTF-8 отклоняется.
 * text и out не должны перекрываться. */
int j_quote(const char *text, char *out, size_t cap);

#endif
