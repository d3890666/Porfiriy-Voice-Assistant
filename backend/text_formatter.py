from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("text_formatter")

# Базовые значения русских числительных (включая падежные основы)
_ONES: Dict[str, int] = {
    "ноль": 0, "нуля": 0, "нулю": 0, "нулём": 0, "нулем": 0, "нуле": 0,
    "один": 1, "одна": 1, "одно": 1, "одного": 1, "одной": 1, "одному": 1, "одну": 1, "одним": 1, "одном": 1,
    "два": 2, "две": 2, "двух": 2, "двум": 2, "двумя": 2,
    "три": 3, "трёх": 3, "трех": 3, "трём": 3, "трем": 3, "тремя": 3,
    "четыре": 4, "четырёх": 4, "четырех": 4, "четырём": 4, "четырем": 4, "четырьмя": 4,
    "пять": 5, "пяти": 5, "пятью": 5,
    "шесть": 6, "шести": 6, "шестью": 6,
    "семь": 7, "семи": 7, "семью": 7,
    "восемь": 8, "восьми": 8, "восемью": 8, "восьмью": 8,
    "девять": 9, "девяти": 9, "девятью": 9,
}

_TEENS: Dict[str, int] = {
    "десять": 10, "десяти": 10, "десятью": 10,
    "одиннадцать": 11, "одиннадцати": 11,
    "двенадцать": 12, "двенадцати": 12,
    "тринадцать": 13, "тринадцати": 13,
    "четырнадцать": 14, "четырнадцати": 14,
    "пятнадцать": 15, "пятнадцати": 15,
    "шестнадцать": 16, "шестнадцати": 16,
    "семнадцать": 17, "семнадцати": 17,
    "восемнадцать": 18, "восемнадцати": 18,
    "девятнадцать": 19, "девятнадцати": 19,
}

_TENS: Dict[str, int] = {
    "двадцать": 20, "двадцати": 20, "двадцатью": 20,
    "тридцать": 30, "тридцати": 30, "тридцатью": 30,
    "сорок": 40, "сорока": 40,
    "пятьдесят": 50, "пятидесяти": 50, "пятьюдесятью": 50,
    "шестьдесят": 60, "шестидесяти": 60, "шестьюдесятью": 60,
    "семьдесят": 70, "семидесяти": 70, "семьюдесятью": 70,
    "восемьдесят": 80, "восьмидесяти": 80, "восемьюдесятью": 80,
    "девяносто": 90, "девяноста": 90,
}

_HUNDREDS: Dict[str, int] = {
    "сто": 100, "ста": 100,
    "двести": 200, "двухсот": 200, "двумстам": 200, "двумястами": 200, "двухстах": 200,
    "триста": 300, "трёхсот": 300, "трехсот": 300, "трёмстам": 300, "тремстам": 300, "тремястами": 300,
    "четыреста": 400, "четырёхсот": 400, "четырехсот": 400, "четырёмстам": 400, "четыремстам": 400,
    "пятьсот": 500, "пятисот": 500, "пятистам": 500, "пятьюстами": 500,
    "шестьсот": 600, "шестисот": 600, "шестистам": 600,
    "семьсот": 700, "семисот": 700, "семистам": 700,
    "восемьсот": 800, "восьмисот": 800, "восьмистам": 800,
    "девятьсот": 900, "девятисот": 900, "девятистам": 900,
}

# Порядковые числительные (все родовые/падежные формы строятся по корню)
_ORD_EXACT: Dict[str, int] = {}
_ORD_STEMS: List[Tuple[str, int]] = [
    ("перв", 1), ("втор", 2), ("трет", 3), ("четверт", 4), ("четвёрт", 4),
    ("пят", 5), ("шест", 6), ("седьм", 7), ("восьм", 8), ("девят", 9),
    ("десят", 10), ("одиннадцат", 11), ("двенадцат", 12), ("тринадцат", 13),
    ("четырнадцат", 14), ("пятнадцат", 15), ("шестнадцат", 16), ("семнадцат", 17),
    ("восемнадцат", 18), ("девятнадцат", 19),
    ("двадцат", 20), ("тридцат", 30), ("сороков", 40),
    ("пятидесят", 50), ("шестидесят", 60), ("семидесят", 70), ("восьмидесят", 80),
    ("девяност", 90), ("сот", 100), ("двухсот", 200), ("трёхсот", 300), ("трехсот", 300),
]
_ORD_ENDINGS = ("ый", "ая", "ое", "ые", "ого", "его", "ому", "ему", "ую", "юю", "ым", "им", "ом", "ем", "ой", "ей", "ых", "их", "ыми", "ими", "ья", "ье", "ьи", "ьего", "ьему", "ью", "ьим", "ьем", "ьей", "ьих", "ьими")
for _stem, _val in _ORD_STEMS:
    for _end in _ORD_ENDINGS:
        _ORD_EXACT[_stem + _end] = _val

# Исключаем ложные срабатывания ("святой", "пятый" -> "пятая" ок, "сотый" ок, "пятое" ок, но не "пятак", "пятница")
_SCALES: Dict[str, int] = {
    "тысяча": 1000, "тысячи": 1000, "тысяч": 1000, "тысяче": 1000, "тысячу": 1000, "тысячей": 1000, "тысячам": 1000, "тысячами": 1000, "тысячах": 1000,
    "миллион": 1_000_000, "миллиона": 1_000_000, "миллионов": 1_000_000, "миллиону": 1_000_000, "миллионом": 1_000_000, "миллионе": 1_000_000,
    "миллиард": 1_000_000_000, "миллиарда": 1_000_000_000, "миллиардов": 1_000_000_000,
}

_ALL_NUM_WORDS = set(_ONES) | set(_TEENS) | set(_TENS) | set(_HUNDREDS) | set(_ORD_EXACT) | set(_SCALES)

_MONTHS_GEN = (
    "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
)

_SCALE_RE_PART = r"(?:тысяч[аиеу]?|тысячей|тысячами|миллион(?:а|ов|у|ом|е)?|миллиард(?:а|ов)?)"


def _word_to_int(w: str) -> Optional[int]:
    lw = w.lower().replace("ё", "е")
    if lw in _ONES:
        return _ONES[lw]
    if lw in _TEENS:
        return _TEENS[lw]
    if lw in _TENS:
        return _TENS[lw]
    if lw in _HUNDREDS:
        return _HUNDREDS[lw]
    if lw in _ORD_EXACT:
        return _ORD_EXACT[lw]
    # Вариант с 'ё' обратно для трёх/четырёх
    orig = w.lower()
    for d in (_ONES, _TEENS, _TENS, _HUNDREDS, _ORD_EXACT):
        if orig in d:
            return d[orig]
    return None


def parse_russian_int_phrase(phrase: str) -> Optional[int]:
    """Парсит русскую числительную фразу ('две тысячи двадцать седьмого', 'двадцати одного', 'пятьсот') -> int."""
    tokens = [t for t in re.split(r"[\s\-]+", phrase.strip()) if t]
    if not tokens:
        return None
    total = 0
    current = 0
    seen = False
    for tok in tokens:
        lw = tok.lower()
        if lw in _SCALES:
            scale = _SCALES[lw]
            if not seen and current == 0:
                current = 1
            total += current * scale
            current = 0
            seen = True
            continue
        val = _word_to_int(tok)
        if val is None:
            return None
        current += val
        seen = True
    return (total + current) if seen else None


def _num_phrase_pattern(include_scales: bool = True) -> str:
    """Регулярка для одного или нескольких подряд идущих слов-числительных."""
    word_set = _ALL_NUM_WORDS if include_scales else (set(_ONES) | set(_TEENS) | set(_TENS) | set(_HUNDREDS) | set(_ORD_EXACT))
    words = sorted(word_set | {w.replace("ё", "е") for w in word_set}, key=len, reverse=True)
    alt = "|".join(re.escape(w) for w in words)
    return rf"(?:{alt})(?:\s+(?:{alt}))*"


_NUM_PHRASE_RE = _num_phrase_pattern(include_scales=True)
_NUM_NO_SCALES_RE = _num_phrase_pattern(include_scales=False)

# Дробные выражения: "семнадцать целых и одну десятую", "шести целых и четырёх десятых", "два с половиной"
_DECIMAL_RE = re.compile(
    rf"(?P<int>{_NUM_PHRASE_RE})\s+цел(?:ых|ая|ую|ой)\s+(?:и\s+)?(?P<frac>{_NUM_PHRASE_RE})\s+"
    rf"(?P<denom>десят(?:ых|ую|ой|ая)|сот(?:ых|ую|ой|ая))",
    re.IGNORECASE,
)

_HALF_RE = re.compile(
    rf"\b(?P<int>{_NUM_PHRASE_RE})\s+с\s+половиной\b",
    re.IGNORECASE,
)


def _format_num_token(raw: str) -> Optional[str]:
    """Пробует превратить фразу или число в строку-число ('6,4', '21', '13')."""
    s = raw.strip()
    if re.fullmatch(r"\d+(?:[.,]\d+)?", s):
        return s
    val = parse_russian_int_phrase(s)
    return str(val) if val is not None else None


def _replace_decimals(text: str) -> str:
    def _dec_sub(m: re.Match) -> str:
        int_val = parse_russian_int_phrase(m.group("int"))
        frac_val = parse_russian_int_phrase(m.group("frac"))
        if int_val is None or frac_val is None:
            return m.group(0)
        denom = m.group("denom").lower()
        if denom.startswith("сот"):
            return f"{int_val},{frac_val:02d}" if frac_val < 100 else f"{int_val},{frac_val}"
        return f"{int_val},{frac_val}"

    text = _DECIMAL_RE.sub(_dec_sub, text)

    def _half_sub(m: re.Match) -> str:
        int_val = parse_russian_int_phrase(m.group("int"))
        if int_val is None:
            return m.group(0)
        return f"{int_val},5"

    return _HALF_RE.sub(_half_sub, text)


def normalize_ru_numbers_python(text: str) -> str:
    """
    Контекстная нормализация русских числительных в цифры без поломки идиом.
    Переводит время, даты, десятичные дроби, цены, проценты, температуру, единицы и адреса.
    """
    if not text or not isinstance(text, str):
        return text

    out = text

    # 1. Дробные числительные ("семнадцать целых и одну десятую" -> "17,1")
    out = _replace_decimals(out)

    # 2. Временные интервалы: "с тринадцати до двадцати одного часа", "с 13 до 21 часа"
    num_or_word = rf"(?:\d{{1,2}}(?:[.,]\d+)?|{_NUM_PHRASE_RE})"
    re_time_range = re.compile(
        rf"\bс\s+(?P<h1>{num_or_word})\s+до\s+(?P<h2>{num_or_word})\s+час(?:а|ов)?\b",
        re.IGNORECASE,
    )

    def _time_range_sub(m: re.Match) -> str:
        v1 = _format_num_token(m.group("h1"))
        v2 = _format_num_token(m.group("h2"))
        if v1 is None or v2 is None or not v1.isdigit() or not v2.isdigit():
            return m.group(0)
        h1, h2 = int(v1), int(v2)
        if 0 <= h1 <= 24 and 0 <= h2 <= 24:
            return f"с {h1}:00 до {h2}:00"
        return m.group(0)

    out = re_time_range.sub(_time_range_sub, out)

    # 3. Время суток: "в восемнадцать тридцать", "в девять часов [утра/вечера]"
    re_clock_hm = re.compile(
        rf"\b(?P<prep>в|к|около|после|до|с)\s+(?P<h>{_NUM_PHRASE_RE})\s+(?P<m>{_NUM_PHRASE_RE})\b"
        rf"(?=\s+(?:утра|дня|вечера|ночи|по\s+москве|мск)\b|(?:\s*[.,!?;:\n]|$))",
        re.IGNORECASE,
    )

    def _clock_hm_sub(m: re.Match) -> str:
        hv = parse_russian_int_phrase(m.group("h"))
        mv = parse_russian_int_phrase(m.group("m"))
        if hv is None or mv is None:
            return m.group(0)
        if 0 <= hv <= 23 and 1 <= mv <= 59:
            return f"{m.group('prep')} {hv}:{mv:02d}"
        return m.group(0)

    out = re_clock_hm.sub(_clock_hm_sub, out)

    re_clock_h = re.compile(
        rf"\b(?P<prep>в|к|с|до|после)\s+(?P<h>{_NUM_PHRASE_RE})\s+час(?:а|ов|у)?\b",
        re.IGNORECASE,
    )

    def _clock_h_sub(m: re.Match) -> str:
        hv = parse_russian_int_phrase(m.group("h"))
        if hv is None or not (0 <= hv <= 24):
            return m.group(0)
        return f"{m.group('prep')} {hv}:00"

    out = re_clock_h.sub(_clock_h_sub, out)

    # 4. Даты: "десятого января две тысячи двадцать седьмого года", "восемнадцатого октября"
    re_date_full = re.compile(
        rf"\b(?P<day>{_NUM_PHRASE_RE})\s+(?P<mon>{_MONTHS_GEN})"
        rf"(?:\s+(?P<year>{_NUM_PHRASE_RE})\s+(?P<yword>года|году|год|г\.?))?\b",
        re.IGNORECASE,
    )

    def _date_sub(m: re.Match) -> str:
        dv = parse_russian_int_phrase(m.group("day"))
        if dv is None or not (1 <= dv <= 31):
            return m.group(0)
        res = f"{dv} {m.group('mon').lower()}"
        if m.group("year"):
            yv = parse_russian_int_phrase(m.group("year"))
            if yv is not None and 1000 <= yv <= 2100:
                res += f" {yv} {m.group('yword')}"
            else:
                res += f" {m.group('year')} {m.group('yword')}"
        return res

    out = re_date_full.sub(_date_sub, out)

    # Отдельно: "в две тысячи двадцать седьмом году"
    re_year = re.compile(
        rf"\b(?P<year>{_NUM_PHRASE_RE})\s+(?P<yword>года|году|год)\b",
        re.IGNORECASE,
    )

    def _year_sub(m: re.Match) -> str:
        yv = parse_russian_int_phrase(m.group("year"))
        if yv is not None and 1900 <= yv <= 2100:
            return f"{yv} {m.group('yword')}"
        return m.group(0)

    out = re_year.sub(_year_sub, out)

    # 5. Диапазоны "от X до Y <единица>" ("от 6,4 до двадцати одного миллиона рублей")
    num_any = rf"(?:\d+(?:[.,]\d+)?|{_NUM_NO_SCALES_RE})"
    re_range_unit = re.compile(
        rf"\bот\s+(?P<a>{num_any})(?:\s+(?P<sa>{_SCALE_RE_PART}))?\s+до\s+(?P<b>{num_any})(?:\s+(?P<sb>{_SCALE_RE_PART}))?\s+"
        rf"(?P<unit>рубл(?:ей|я|ь)|доллар(?:ов|а)?|евро|процент(?:ов|а)?|градус(?:ов|а)?|"
        rf"минут(?:ы|у)?|час(?:ов|а)?|дн(?:ей|я)|недел(?:ь|и)|месяц(?:ев|а)?|лет|год(?:а|ов)?|"
        rf"км|километр(?:ов|а)?|метр(?:ов|а)?|мм|см|мл|миллилитр(?:ов|а)?|грамм(?:ов|а)?|кг|килограмм(?:ов|а)?)\b",
        re.IGNORECASE,
    )

    def _norm_scale(sc: Optional[str]) -> str:
        if not sc:
            return ""
        lsc = sc.lower()
        if lsc.startswith("тысяч"):
            return " тыс."
        if lsc.startswith("миллион"):
            return " млн"
        if lsc.startswith("миллиард"):
            return " млрд"
        return f" {sc}"

    def _norm_unit(u: str) -> str:
        lu = u.lower()
        if lu.startswith("рубл"):
            return " ₽"
        if lu.startswith("процент"):
            return "%"
        if lu.startswith("градус"):
            return "°"
        return f" {u}"

    def _range_unit_sub(m: re.Match) -> str:
        va = _format_num_token(m.group("a"))
        vb = _format_num_token(m.group("b"))
        if va is None or vb is None:
            return m.group(0)
        sa = _norm_scale(m.group("sa"))
        sb = _norm_scale(m.group("sb"))
        u = _norm_unit(m.group("unit"))
        return f"от {va}{sa} до {vb}{sb}{u}"

    out = re_range_unit.sub(_range_unit_sub, out)

    # 6. Одиночное число (или дробь) + масштаб + единица: "17,1 миллиона рублей" -> "17,1 млн ₽"
    re_single_scale_unit = re.compile(
        rf"\b(?P<a>{num_any})\s+(?P<sa>{_SCALE_RE_PART})\s+"
        rf"(?P<unit>рубл(?:ей|я|ь)|доллар(?:ов|а)?|евро|человек|жител(?:ей|я)|штук|раз)\b",
        re.IGNORECASE,
    )

    def _single_su_sub(m: re.Match) -> str:
        va = _format_num_token(m.group("a"))
        if va is None:
            return m.group(0)
        sa = _norm_scale(m.group("sa"))
        u = _norm_unit(m.group("unit"))
        return f"{va}{sa}{u}"

    out = re_single_scale_unit.sub(_single_su_sub, out)

    # 7. Число + масштаб ("пятьсот миллионов", "около двух тысяч")
    re_scale_only = re.compile(
        rf"\b(?P<a>{num_any})\s+(?P<sa>{_SCALE_RE_PART})\b",
        re.IGNORECASE,
    )

    def _scale_only_sub(m: re.Match) -> str:
        va = _format_num_token(m.group("a"))
        if va is None:
            return m.group(0)
        return f"{va}{_norm_scale(m.group('sa'))}"

    out = re_scale_only.sub(_scale_only_sub, out)

    # 8. Температура: "минус пять градусов" -> "-5°", "плюс двадцать градусов" -> "+20°"
    re_temp = re.compile(
        rf"\b(?:(?P<sign>минус|плюс)\s+)?(?P<a>{num_any})\s+градус(?:ов|а)?\b",
        re.IGNORECASE,
    )

    def _temp_sub(m: re.Match) -> str:
        va = _format_num_token(m.group("a"))
        if va is None:
            return m.group(0)
        sign = ""
        if m.group("sign"):
            sign = "-" if m.group("sign").lower() == "минус" else "+"
        return f"{sign}{va}°"

    out = re_temp.sub(_temp_sub, out)

    # 9. Деньги / проценты / явные единицы измерения
    re_unit = re.compile(
        rf"\b(?P<a>{_NUM_PHRASE_RE})\s+"
        rf"(?P<unit>рубл(?:ей|я|ь)|процент(?:ов|а)?|"
        rf"доллар(?:ов|а)?|евро|минут(?:ы|у)?|секунд(?:ы|у)?|час(?:ов|а)?|дн(?:ей|я|ень)|недел(?:ь|и|ю)|месяц(?:ев|а)?|лет|год(?:а|ов)?|"
        rf"миллилитр(?:ов|а)?|мл|литр(?:ов|а)?|грамм(?:ов|а)?|граммов|кг|килограмм(?:ов|а)?|метр(?:ов|а)?|километр(?:ов|а)?|см|мм|этаж(?:е|ей|а)?|комнат(?:ы)?)\b",
        re.IGNORECASE,
    )

    def _unit_sub(m: re.Match) -> str:
        va = parse_russian_int_phrase(m.group("a"))
        if va is None:
            return m.group(0)
        u = m.group("unit")
        lu = u.lower()
        if lu.startswith("рубл"):
            return f"{va} ₽"
        if lu.startswith("процент"):
            return f"{va}%"
        if lu.startswith("миллилитр"):
            return f"{va} мл"
        if lu.startswith("килограмм"):
            return f"{va} кг"
        if lu.startswith("грамм"):
            return f"{va} г"
        if lu.startswith("километр"):
            return f"{va} км"
        return f"{va} {u}"

    out = re_unit.sub(_unit_sub, out)

    # 10. Адреса / номера: "дом сорок шесть" -> "дом 46", "строение три" -> "строение 3", "квартира двенадцать" -> "квартира 12"
    re_addr = re.compile(
        rf"\b(?P<kw>дом|доме|дома|строение|строении|корпус|корпусе|квартира|квартире|подъезд|этаж|офис|кабинет|номер)\s+(?P<a>{_NUM_PHRASE_RE})\b",
        re.IGNORECASE,
    )

    def _addr_sub(m: re.Match) -> str:
        va = parse_russian_int_phrase(m.group("a"))
        if va is None:
            return m.group(0)
        return f"{m.group('kw')} {va}"

    out = re_addr.sub(_addr_sub, out)

    return out


# Признак оставшихся сложных/составных числительных после Python-нормализатора
_COMPLEX_REMAINDER_RE = re.compile(
    r"\b("
    r"целых|целая|десятых|десятую|сотых|полтора|полутора|четверти|"
    r"полвторого|полтретьего|полчетвертого|полпятого|полшестого|полседьмого|полвосьмого|полдевятого|полдесятого|"
    r"тысяч[аи]?|миллион(?:а|ов)?|миллиард(?:а|ов)?|"
    r"одиннадцат[а-я]*|двенадцат[а-я]*|тринадцат[а-я]*|четырнадцат[а-я]*|пятнадцат[а-я]*|"
    r"шестнадцат[а-я]*|семнадцат[а-я]*|восемнадцат[а-я]*|девятнадцат[а-я]*|"
    r"двадцат[а-я]*|тридцат[а-я]*|сорок[а]?|пятьдесят|пятидесяти|шестьдесят|шестидесяти|"
    r"семьдесят|семидесяти|восемьдесят|восьмидесяти|девяност[ао]|"
    r"двести|двухсот|триста|тр[её]хсот|четыреста|четыр[её]хсот|"
    r"пятьсот|пятисот|шестьсот|шестисот|семьсот|семисот|восемьсот|восьмисот|девятьсот|девятисот"
    r")\b",
    re.IGNORECASE,
)


def has_complex_number_words(text: str) -> bool:
    """Возвращает True, если в тексте ещё остались составные или разговорные числительные словами."""
    if not text:
        return False
    return bool(_COMPLEX_REMAINDER_RE.search(text))


async def format_reply_text(text: str, options: Optional[Dict[str, Any]] = None) -> str:
    """
    Гибридная нормализация текстового ответа для Telegram и часов Wear OS:
    1) Быстрый контекстный Python-нормализатор (< 1 мс).
    2) Если включён пул моделей И остались сложные/разговорные числительные — дожимает через big_model_pool.
    """
    if not text or not isinstance(text, str):
        return text
    opts = options or {}
    if not opts.get("normalize_numbers_enabled", True):
        return text

    step1 = normalize_ru_numbers_python(text)
    if not has_complex_number_words(step1):
        return step1

    if not opts.get("big_models_enabled"):
        return step1

    try:
        try:
            from .big_models import big_model_pool_singleton
        except ImportError:
            from big_models import big_model_pool_singleton

        prompt = (
            "Преобразуй только русские числительные, время, даты, цены, дроби и номера домов/адресов "
            "в компактную цифровую запись (примеры: 'с 13:00 до 21:00', '17,1 млн ₽', 'от 6,4 до 21 млн ₽', '10 января 2027 года', 'дом 46').\n"
            "СТРОГИЕ ПРАВИЛА:\n"
            "1. НЕ меняй смысл, стиль, знаки препинания и остальные слова.\n"
            "2. НЕ заменяй устойчивые обороты и идиомы ('в двух словах', 'один момент', 'первый раз', 'пара минут').\n"
            "3. Верни СТРОГО только исправленный текст без пояснений и кавычек.\n\n"
            f"ТЕКСТ:\n{step1}"
        )
        res = await asyncio.wait_for(
            big_model_pool_singleton.run_text_task(prompt, opts, enable_google_search=False),
            timeout=4.5,
        )
        if res.get("success"):
            candidate = str(res.get("text") or "").strip()
            ratio = len(candidate) / max(len(step1), 1)
            if candidate and 0.45 <= ratio <= 1.35:
                return candidate
    except asyncio.TimeoutError:
        logger.debug("[ITN] Big model pool timed out (4.5s), using Python ITN result")
    except Exception as e:
        logger.debug(f"[ITN] Big model pool fallback error: {e}")

    return step1
