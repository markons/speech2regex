"""
Text-to-Regex GUI: heuristically translates an English description of a
string pattern into a regular expression, with an editable output and a
built-in test area.

This is a rule-based parser (not general NLU). It recognizes common phrasings:
  - position: "first/second/... position or character", "last position",
    "second to last", "Nth position", "starts with", "ends with"
  - character classes: "numbers/digits", "letters", "uppercase/lowercase
    letters", "vowels", "consonants", "spaces", quoted sets like 'aA'
  - whole-string constraints: "only <class>" with no position given
  - containment: "contains X" / "does not contain X"
  - length: "exactly N characters", "at least/at most N characters",
    "between N and M characters", "length > N", "longer than N characters"
Anything it can't confidently parse is listed as a warning so the regex
can be hand-edited.

Optional AI fallback: when the heuristic parser leaves parts of the
description unmatched, the GUI can ask a local Ollama model (no API key,
no subscription, runs on localhost) to suggest a regex instead. This is
purely opt-in and fails gracefully with a helpful message if Ollama isn't
running or the model hasn't been pulled.
"""
import json
import queue
import re
import threading
import tkinter as tk
import urllib.error
import urllib.request
from tkinter import ttk, scrolledtext

# Optional: a tiny, model-free number-word parser (no download, pure Python)
# that extends number-word recognition beyond CARDINAL_WORDS' 0-10 range
# (e.g. "twelve digits", "twenty characters"). Not required — falls back to
# CARDINAL_WORDS if unavailable, so the app still runs with no third-party
# packages installed.
try:
    from word2number import w2n
    HAVE_WORD2NUMBER = True
except ImportError:
    w2n = None
    HAVE_WORD2NUMBER = False

# 127.0.0.1, not "localhost": on Windows, "localhost" can resolve to the
# IPv6 ::1 first, which hangs until the request times out instead of
# failing fast when Ollama is only listening on IPv4.
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "tinyllama:latest"
OLLAMA_STATUS_POLL_MS = 4000


CHAR_CLASS_KEYWORDS = [
    (re.compile(r'\bnumbers?\b|\bdigits?\b'), r'\d', 'digit'),
    (re.compile(r'\buppercase letters?\b|\bcapital letters?\b'), r'[A-Z]', 'uppercase letter'),
    (re.compile(r'\blowercase letters?\b|\bsmall letters?\b'), r'[a-z]', 'lowercase letter'),
    (re.compile(r'\bletters?\b|\balphabetic\b|\balphabet\b'), r'[A-Za-z]', 'letter'),
    (re.compile(r'\bvowels?\b'), r'[aeiouAEIOU]', 'vowel'),
    (re.compile(r'\bconsonants?\b'), r'[b-df-hj-np-tv-zB-DF-HJ-NP-TV-Z]', 'consonant'),
    (re.compile(r'\bspaces?\b|\bwhitespace\b'), r'\s', 'space'),
    (re.compile(r'\bpunctuation\b'), r'[^\w\s]', 'punctuation'),
    (re.compile(r'\bany character\b|\banything\b'), r'.', 'any character'),
]

ORDINAL_WORDS = {
    'first': 0, 'second': 1, 'third': 2, 'fourth': 3, 'fifth': 4,
    'sixth': 5, 'seventh': 6, 'eighth': 7, 'ninth': 8, 'tenth': 9,
}

LAST_PHRASES = [
    ('second to last', -2), ('second-to-last', -2), ('second last', -2),
    ('penultimate', -2), ('last', -1),
]

NEGATION_RE = re.compile(r"\b(not|no|without|doesn'?t|don'?t|never|cannot|can't|isn'?t)\b")
CONTAINS_RE = re.compile(r'\b(contains?|includes?|has(?: the)?(?: substring| text)?)\b')
ONLY_RE = re.compile(r'\bonly\b')
LENGTH_REMNANT_RE = re.compile(r'^\d+\s+characters?\.?$', re.I)
LENGTH_CLAUSE_RE = re.compile(
    r'\blength\s*(?:[<>]=?|=|==|is|of)\s*\d+|'
    r'\b(?:exactly|at least|at most|minimum|maximum|no more than|'
    r'longer than|shorter than|more than|fewer than|less than|greater than)\s+\d+\s+characters?\b|'
    r'\d+\s+characters?\s+long\b',
    re.I,
)


def get_position(clause_lower):
    if re.search(r'\b(start(?:s|ing)? with|begins? with)\b', clause_lower):
        return 0
    if re.search(r'\bends? with\b', clause_lower):
        return -1
    for phrase, idx in LAST_PHRASES:
        if re.search(rf'\b{re.escape(phrase)}\b', clause_lower):
            return idx
    m = re.search(r'\b(\d+)(?:st|nd|rd|th)\b', clause_lower)
    if m:
        return int(m.group(1)) - 1
    for word, idx in ORDINAL_WORDS.items():
        if re.search(rf'\b{word}\b', clause_lower):
            return idx
    return None


POSITION_RANGE_RE = re.compile(
    r'\bpos\w*tion\s+(\d+)\s*(?:-|to|through)\s*(\d+)\b', re.I)
ALTERNATIVES_LEAD_RE = re.compile(
    r'\b(?:can be|could be|must be|is one of|are)\b\s*(.+)$', re.I)
ALTERNATIVES_SPLIT_RE = re.compile(r'\s*(?:,\s*(?:or|and)?\s*|\bor\b|\band\b)\s*', re.I)
ALT_LEADING_CONNECTOR_RE = re.compile(r'^(?:or|and)\s+', re.I)


def get_position_range(clause_lower):
    """Detect 'position N-M' (also 'N to M' / 'N through M'); returns
    (start, end) as 0-indexed inclusive positions, or None."""
    m = POSITION_RANGE_RE.search(clause_lower)
    if not m:
        return None
    a, b = int(m.group(1)) - 1, int(m.group(2)) - 1
    return (a, b) if a <= b else (b, a)


def get_alternatives(clause):
    """Extract a literal alternatives list from '... can be \'20\' \'or 30\' or \'70\''
    style text (quoted, tolerant of a stray leading 'or'/'and' inside a quote) or
    from unquoted '20, 30 or 70'. Returns a list of literal strings, or None."""
    m = ALTERNATIVES_LEAD_RE.search(clause)
    tail = m.group(1) if m else clause
    quoted = re.findall(r'''['"]([^'"]+)['"]''', tail)
    if quoted:
        return [ALT_LEADING_CONNECTOR_RE.sub('', q.strip()) for q in quoted]
    parts = [p.strip() for p in ALTERNATIVES_SPLIT_RE.split(tail.strip()) if p.strip()]
    return parts if len(parts) > 1 else None


# Detects unquoted short-token alternative lists like "1 or 2 or 3" or
# "a, b or c" so a class keyword ("numbers", "letters", ...) attached to
# such a list builds an explicit character class instead of falling back
# to the keyword's generic class (e.g. "numbers 1 or 2 or 3" -> [123],
# not \d).
_INLINE_ALT_TOKEN = r'(?!(?:or|and)\b)[A-Za-z0-9]{1,2}\b'
_INLINE_ALT_SEP = r'(?:\s*,\s*(?:or|and)?\s*|\s+(?:or|and)\s+)'
INLINE_ALT_RUN_RE = re.compile(
    rf'\b{_INLINE_ALT_TOKEN}(?:{_INLINE_ALT_SEP}{_INLINE_ALT_TOKEN})+', re.I)


def get_inline_alternatives(clause):
    """Find a run like '1 or 2 or 3' / 'aA or bB' of short alnum tokens
    chained by or/and/comma; returns the list of tokens, or None."""
    m = INLINE_ALT_RUN_RE.search(clause)
    if not m:
        return None
    tokens = [t for t in ALTERNATIVES_SPLIT_RE.split(m.group(0)) if t.strip()]
    return tokens if len(tokens) > 1 else None


def get_charclass(clause, clause_lower, quoted_strings):
    if quoted_strings:
        chars = ''.join(dict.fromkeys(''.join(quoted_strings)))
        if len(chars) == 1:
            return re.escape(chars), f"'{chars}'"
        return f'[{re.escape(chars)}]', f"one of '{chars}'"

    has_class_keyword = any(p.search(clause_lower) for p, _, _ in CHAR_CLASS_KEYWORDS) or \
        re.search(r'\bcharacter\s+[a-zA-Z0-9]\b', clause_lower)
    if has_class_keyword:
        inline_alts = get_inline_alternatives(clause)
        if inline_alts:
            chars = ''.join(dict.fromkeys(''.join(inline_alts)))
            if len(chars) == 1:
                return re.escape(chars), f"'{chars}'"
            return f'[{re.escape(chars)}]', f"one of '{chars}'"

    for pattern, token, label in CHAR_CLASS_KEYWORDS:
        if pattern.search(clause_lower):
            return token, label
    m = re.search(r'\bcharacter\s+([a-zA-Z0-9])\b', clause_lower)
    if m:
        ch = m.group(1)
        return re.escape(ch), f"'{ch}'"
    return None, None


def negate_token(token):
    if token == r'\d':
        return r'\D'
    if token == r'\s':
        return r'\S'
    if token.startswith('[') and token.endswith(']') and not token.startswith('[^'):
        return '[^' + token[1:-1] + ']'
    if len(token) <= 2:
        return f'[^{token}]'
    return token  # can't safely negate, leave as-is


CARDINAL_WORDS = {
    'zero': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
}
_NUM_WORD_RE = r'(\d+|zero|one|two|three|four|five|six|seven|eight|nine|ten)'


def _cardinal_to_int(word):
    return int(word) if word.isdigit() else CARDINAL_WORDS.get(word)


def get_char_count(clause_lower):
    """Detect 'exactly/at least/at most N <char/class>' occurrence constraints."""
    m = re.search(rf'\b(?:at\s+most|no\s+more\s+than)\s+{_NUM_WORD_RE}\b', clause_lower)
    if m:
        return 'max', _cardinal_to_int(m.group(1))
    m = re.search(rf'\bat\s+least\s+{_NUM_WORD_RE}\b', clause_lower)
    if m:
        return 'min', _cardinal_to_int(m.group(1))
    m = re.search(rf'\b(?:only|exactly|precisely)\s+{_NUM_WORD_RE}\b', clause_lower)
    if m:
        return 'exact', _cardinal_to_int(m.group(1))
    return None, None


def build_count_fragment(token, kind, n):
    """A whole-string fragment (for use as (?=frag$)) requiring exactly/at least/
    at most n occurrences of token, interspersed with any number of non-token chars."""
    not_token = negate_token(token)
    if kind == 'exact':
        return f'{not_token}*(?:{token}{not_token}*){{{n}}}'
    if kind == 'min':
        return f'{not_token}*(?:{token}{not_token}*){{{n},}}'
    if kind == 'max':
        return f'{not_token}*(?:{token}{not_token}*){{0,{n}}}'
    return None


def parse_length(text_lower):
    exact = min_len = max_len = None
    m = re.search(r'\bbetween\s+(\d+)\s+and\s+(\d+)\s+characters?\b', text_lower)
    if m:
        min_len, max_len = int(m.group(1)), int(m.group(2))
        return exact, min_len, max_len
    m = re.search(r'\bexactly\s+(\d+)\s+characters?\b', text_lower)
    if not m:
        m = re.search(r'\b(\d+)\s+characters?\s+long\b', text_lower)
    if not m:
        m = re.search(r'\blength\s*(?:=|==|is|of)\s*(\d+)\b', text_lower)
    if m:
        exact = int(m.group(1))
        return exact, min_len, max_len

    # symbolic comparisons: length > 10, length >= 10, length < 10, length <= 10
    m = re.search(r'\blength\s*>=\s*(\d+)', text_lower)
    if m:
        min_len = int(m.group(1))
    else:
        m = re.search(r'\blength\s*>\s*(\d+)', text_lower)
        if m:
            min_len = int(m.group(1)) + 1
    m = re.search(r'\blength\s*<=\s*(\d+)', text_lower)
    if m:
        max_len = int(m.group(1))
    else:
        m = re.search(r'\blength\s*<\s*(\d+)', text_lower)
        if m:
            max_len = int(m.group(1)) - 1
    if min_len is not None or max_len is not None:
        return exact, min_len, max_len

    m = re.search(r'\bat least\s+(\d+)\s+characters?\b', text_lower)
    if not m:
        m = re.search(r'\bminimum\s+(?:of\s+)?(\d+)\s+characters?\b', text_lower)
    if not m:
        m = re.search(r'\b(?:longer than|more than|greater than)\s+(\d+)\s+characters?\b', text_lower)
        if m:
            min_len = int(m.group(1)) + 1
    if m and min_len is None:
        min_len = int(m.group(1))

    m = re.search(r'\bat most\s+(\d+)\s+characters?\b', text_lower)
    if not m:
        m = re.search(r'\bmaximum\s+(?:of\s+)?(\d+)\s+characters?\b', text_lower)
    if not m:
        m = re.search(r'\bno more than\s+(\d+)\s+characters?\b', text_lower)
    if not m:
        m = re.search(r'\b(?:shorter than|fewer than|less than)\s+(\d+)\s+characters?\b', text_lower)
        if m:
            max_len = int(m.group(1)) - 1
    if m and max_len is None:
        max_len = int(m.group(1))

    return exact, min_len, max_len


def split_clauses(text):
    parts = re.split(r'\s*(?:,\s*and\s+|\band\b|;|(?<=[a-zA-Z0-9\'"])\.\s+|\.\s*$)\s*', text)
    return [p.strip() for p in parts if p and p.strip()]


def build_ollama_prompt(description):
    return (
        "You convert an English description of a string pattern into a single "
        "Python-compatible regular expression.\n"
        "Rules:\n"
        "- Respond with ONLY the regular expression, on one line.\n"
        "- No explanation, no markdown formatting, no backticks, no surrounding quotes.\n"
        "- Anchor with ^ and $ unless the description clearly wants a substring search.\n\n"
        "Example:\n"
        "Description: select only strings which have only numbers in the first position "
        "and characters 'aA' on the last position.\n"
        "Regex: ^\\d.*[aA]$\n\n"
        f"Description: {description}\n"
        "Regex:"
    )


_REGEX_METACHAR_RE = re.compile(r'[\^\$\.\*\+\?\{\}\[\]\(\)\|\\]')
_FILLER_WORDS = {
    'the', 'is', 'a', 'an', 'of', 'for', 'this', 'that', 'would', 'be', 'here',
    'sure', 'answer', 'regex', 'pattern', 'string', 'strings', 'to', 'match',
    'following', 'you', 'can', 'use', 'should', 'it', 'as',
}


def _looks_like_regex(candidate):
    """Reject prose that happens to compile as a (useless) literal pattern."""
    if not candidate:
        return False
    if ' ' not in candidate:
        return True  # a single token is never mistaken prose
    if not _REGEX_METACHAR_RE.search(candidate):
        return False  # multi-word with no regex syntax at all: almost certainly prose
    tokens = re.findall(r'[A-Za-z]{2,}', candidate)
    filler_hits = sum(1 for t in tokens if t.lower() in _FILLER_WORDS)
    return filler_hits < 2


def _clean_candidate(candidate):
    candidate = candidate.strip().strip('"').strip("'").strip()
    if candidate.lower().startswith('regex:'):
        candidate = candidate[len('regex:'):].strip()
    return candidate


def extract_regex(raw_response):
    text = raw_response.strip()
    text = re.sub(r'^```[a-zA-Z]*\n?', '', text)
    text = re.sub(r'```$', '', text).strip()
    text = text.strip('`').strip()

    first_line = text.splitlines()[0].strip() if text else ''
    for raw_candidate in (first_line, text):
        candidate = _clean_candidate(raw_candidate)
        if not candidate or not _looks_like_regex(candidate):
            continue
        try:
            re.compile(candidate)
            return candidate
        except re.error:
            continue

    # last resort: pull an anchored-looking span out of a noisier response
    m = re.search(r'\^.*\$', text)
    if m:
        candidate = _clean_candidate(m.group(0))
        if candidate and _looks_like_regex(candidate):
            try:
                re.compile(candidate)
                return candidate
            except re.error:
                pass

    return None


def ask_ollama(description, model=DEFAULT_OLLAMA_MODEL, host=DEFAULT_OLLAMA_HOST, timeout=60):
    prompt = build_ollama_prompt(description)
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        # num_predict/stop bound worst-case latency: without them, a small
        # model can ramble past its coherent answer and run for minutes
        # instead of stopping after the one-line regex we asked for.
        "options": {"temperature": 0, "num_predict": 64, "stop": ["\n"]},
    }).encode('utf-8')
    url = f"{host.rstrip('/')}/api/generate"
    req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode('utf-8'))
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"could not reach Ollama at {host} ({e.reason if hasattr(e, 'reason') else e}). "
            f"Is Ollama running?"
        ) from e
    if 'error' in body:
        raise RuntimeError(
            f"Ollama error: {body['error']}. Try: ollama pull {model}"
        )
    raw = body.get('response', '')
    return extract_regex(raw), raw


def check_ollama_alive(host=DEFAULT_OLLAMA_HOST, timeout=2):
    """Quick liveness probe: is an Ollama server actually answering at host?"""
    url = f"{host.rstrip('/')}/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


SEQUENCE_CONNECTOR_RE = re.compile(r'\bthen\b', re.I)
FIND_IN_STRING_RE = re.compile(
    r'\bfind\b.*\bin\b.*\bstring\b|'
    r'\bsubstrings?\s+before\s+and\s+after\b|'
    r'\banywhere\s+in\s+(?:a|the)\s+string\b|'
    r'\b(?:any|arbitrary)\s+or\s+(?:null|no|zero)\s+characters?\b',
    re.I,
)
SAMPLE_RE = re.compile(r'\bsample:\s*(\S+)', re.I)
SLASH_ALTERNATIVES_RE = re.compile(r'^\w+(?:/\w+)+$')
SEPARATOR_WORDS = {'slash': '/', 'dash': '-', 'hyphen': '-', 'underscore': '_',
                    'dot': '.', 'period': '.'}
SEPARATOR_WORD_RE = re.compile(
    r'^(?:a\s+)?(' + '|'.join(SEPARATOR_WORDS) + r')$', re.I)


_NUMBER_WORD_TOKEN_RE = re.compile(
    r'^(zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|'
    r'thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|'
    r'thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|'
    r'million|billion|and)$'
)


def _extract_leading_count(words_lower):
    """Try increasingly long word prefixes as a number ('twenty two' before
    'twenty'); returns (count, remaining_words) or (None, words_lower).
    word2number ignores non-number words instead of rejecting them (e.g.
    word_to_num('seven digits') == 7), so prefixes are only tried if every
    token in them actually looks like a number word."""
    tokens = words_lower.split()
    for length in range(min(4, len(tokens)), 0, -1):
        prefix_tokens = tokens[:length]
        prefix = ' '.join(prefix_tokens)
        rest = ' '.join(tokens[length:])
        if prefix.isdigit():
            return int(prefix), rest
        if not all(_NUMBER_WORD_TOKEN_RE.match(t) for t in prefix_tokens):
            continue
        if HAVE_WORD2NUMBER:
            try:
                return w2n.word_to_num(prefix), rest
            except ValueError:
                continue
        elif length == 1 and prefix in CARDINAL_WORDS:
            return CARDINAL_WORDS[prefix], rest
    return None, words_lower


def tokenize_sequence_segment(segment):
    """Turn one 'then'-separated segment of a sequence description into a
    (regex_fragment, human_label) pair, or (None, None) if unrecognized."""
    seg = segment.strip().strip(',.').strip()
    if not seg:
        return None, None
    seg_lower = seg.lower()

    m = SEPARATOR_WORD_RE.match(seg_lower)
    if m:
        ch = SEPARATOR_WORDS[m.group(1).lower()]
        return re.escape(ch), f"'{ch}'"

    m = re.fullmatch(r'''['"]([^'"]+)['"]''', seg)
    if m:
        return re.escape(m.group(1)), f"'{m.group(1)}'"

    if SLASH_ALTERNATIVES_RE.match(seg):
        options = seg.split('/')
        return '(?:' + '|'.join(re.escape(o) for o in options) + ')', \
            f"one of {', '.join(options)}"

    count, rest = _extract_leading_count(seg_lower)
    if count is not None and rest:
        for pattern, token, label in CHAR_CLASS_KEYWORDS:
            if pattern.search(rest):
                return f'{token}{{{count}}}', f"{count} {label}"
        if re.fullmatch(r'characters?', rest):
            return f'.{{{count}}}', f"{count} character(s)"

    if re.fullmatch(r'[A-Za-z0-9]+', seg):
        return re.escape(seg), f"'{seg}'"

    for pattern, token, label in CHAR_CLASS_KEYWORDS:
        if pattern.search(seg_lower):
            return f'{token}+', label

    return None, None


def try_build_sequence_regex(description):
    """Handle 'A then B then C' style descriptions: an ordered composition of
    literal text, alternatives ('20/30/70'), and counted character classes
    ('seven digits'). Returns (regex, explanation, warnings) or (None, [], [])
    if the description doesn't look like this shape, or a segment can't be
    understood (in which case callers should fall back to the normal
    position/count/contains heuristics)."""
    sample_match = SAMPLE_RE.search(description)
    sample = sample_match.group(1) if sample_match else None
    text = description[:sample_match.start()] if sample_match else description

    target = None
    for clause in split_clauses(text):
        if SEQUENCE_CONNECTOR_RE.search(clause):
            target = clause

    if target is None:
        return None, [], []

    segments = SEQUENCE_CONNECTOR_RE.split(target)
    if len(segments) < 2:
        return None, [], []

    fragments = []
    explanation = []
    for segment in segments:
        frag, label = tokenize_sequence_segment(segment)
        if frag is None:
            return None, [], []
        fragments.append(frag)
        explanation.append(f"Then: {label}")

    body = ''.join(fragments)
    anchored = not FIND_IN_STRING_RE.search(description)
    regex = f'^{body}$' if anchored else body

    try:
        re.compile(regex)
    except re.error:
        return None, [], []

    warnings = []
    if sample:
        if re.search(regex, sample):
            warnings.append(f"Sample '{sample}' matches the generated pattern.")
        else:
            warnings.append(
                f"Warning: the given sample '{sample}' does NOT match the "
                f"generated pattern — double-check the description.")

    return regex, explanation, warnings


def text_to_regex(description):
    seq_regex, seq_explanation, seq_warnings = try_build_sequence_regex(description)
    if seq_regex is not None:
        return seq_regex, seq_explanation + seq_warnings, []

    exact_len, min_len, max_len = parse_length(description.lower())

    position_map = {}
    range_constraints = []
    whole_class = None
    contains_list = []
    count_fragments = []
    explanation = []
    unmatched = []

    for clause in split_clauses(description):
        cl = clause.lower()
        if re.search(r'^\s*(select|find|match|show)\b', cl) and not re.search(
                r'\b(numbers?|digits?|letters?|vowels?|consonants?|spaces?|characters?|'
                r'positions?|contains?|includes?|starts?|ends?|lengths?)\b', cl):
            continue  # pure lead-in like "select strings which"

        if LENGTH_REMNANT_RE.match(clause.strip()):
            continue  # leftover fragment of a "between N and M characters" clause

        pos_range = get_position_range(cl)
        if pos_range is not None:
            alternatives = get_alternatives(clause)
            if alternatives:
                start, end = pos_range
                token = '(?:' + '|'.join(re.escape(a) for a in alternatives) + ')'
                range_constraints.append((start, end, token))
                explanation.append(
                    f"Characters {start + 1}-{end + 1} must be one of: "
                    + ', '.join(f"'{a}'" for a in alternatives))
                continue

        quoted = re.findall(r"['\"]([^'\"]+)['\"]", clause)
        position = get_position(cl)
        token, label = get_charclass(clause, cl, quoted)
        negate = bool(NEGATION_RE.search(cl))

        if token is None:
            if quoted:
                continue  # already consumed as part of a length/other clause
            if LENGTH_CLAUSE_RE.search(cl):
                continue  # pure length wording, already accounted for by parse_length()
            if any(k in cl for k in ('character', 'position', 'digit', 'letter',
                                      'number', 'vowel', 'consonant', 'space', 'contain')):
                unmatched.append(clause)
            continue

        count_kind, count_n = get_char_count(cl)
        if count_kind is not None and position is None:
            count_fragments.append(build_count_fragment(token, count_kind, count_n))
            word = {'exact': 'exactly', 'min': 'at least', 'max': 'at most'}[count_kind]
            explanation.append(
                f"String must contain {word} {count_n} occurrence(s) of {label}")
            continue

        if CONTAINS_RE.search(cl) and position is None:
            value = quoted[0] if quoted else None
            if value is None:
                continue
            contains_list.append((value, negate))
            explanation.append(
                f"String must {'NOT ' if negate else ''}contain '{value}' (anywhere)")
            continue

        if position is not None:
            position_map[position] = (negate_token(token) if negate else token, negate)
            pos_desc = f"position {position + 1}" if position >= 0 else (
                "last position" if position == -1 else f"position {-position} from the end")
            explanation.append(
                f"Character at {pos_desc} must {'NOT be' if negate else 'be'} {label}")
        elif ONLY_RE.search(cl):
            whole_class = (negate_token(token) if negate else token, negate)
            explanation.append(
                f"Every character in the string must {'NOT be' if negate else 'be'} {label}")
        else:
            unmatched.append(clause)

    if exact_len is not None:
        explanation.append(f"String must be exactly {exact_len} characters long")
    elif min_len is not None and max_len is not None:
        explanation.append(f"String must be between {min_len} and {max_len} characters long")
    elif min_len is not None:
        explanation.append(f"String must be at least {min_len} characters long")
    elif max_len is not None:
        explanation.append(f"String must be at most {max_len} characters long")

    if not position_map and not range_constraints and whole_class is None and not contains_list \
            and not count_fragments and exact_len is None and min_len is None and max_len is None:
        return None, explanation, unmatched

    whole_token = whole_class[0] if whole_class else '.'

    non_neg = sorted(i for i in position_map if i >= 0)
    neg = sorted(i for i in position_map if i < 0)

    range_max_end = max((end for _, end, _ in range_constraints), default=-1)
    prefix_len = max((max(non_neg) + 1) if non_neg else 0, range_max_end + 1)

    # Spans covering the prefix: single characters from position_map, plus
    # (possibly multi-character) literal-alternation spans from range_constraints.
    spans = {start: (end - start + 1, token) for start, end, token in range_constraints}
    for i in non_neg:
        spans.setdefault(i, (1, position_map[i][0]))

    prefix_parts = []
    i = 0
    while i < prefix_len:
        if i in spans:
            length, token = spans[i]
            prefix_parts.append(token)
            i += length
        else:
            prefix_parts.append(whole_token)
            i += 1
    prefix = ''.join(prefix_parts)

    suffix_len = -min(neg) if neg else 0
    suffix = ''.join(
        position_map[i][0] if i in position_map else whole_token
        for i in range(-suffix_len, 0)
    )

    if exact_len is not None:
        middle_count = exact_len - prefix_len - suffix_len
        if middle_count < 0:
            unmatched.append(
                f"(length {exact_len} is too short for the fixed positions requested)")
            middle_count = 0
        middle = f'{whole_token}{{{middle_count}}}' if middle_count > 0 else ''
    else:
        min_middle = max(0, (min_len or 0) - prefix_len - suffix_len)
        if max_len is not None:
            max_middle = max(min_middle, max_len - prefix_len - suffix_len)
            middle = f'{whole_token}{{{min_middle},{max_middle}}}'
        elif min_middle > 0:
            middle = f'{whole_token}{{{min_middle},}}'
        else:
            middle = f'{whole_token}*'

    lookaheads = ''.join(
        f'(?!.*{re.escape(v)})' if neg_ else f'(?=.*{re.escape(v)})'
        for v, neg_ in contains_list
    )
    lookaheads += ''.join(f'(?={frag}$)' for frag in count_fragments)

    body = prefix + middle + suffix
    if not body and not lookaheads:
        return None, explanation, unmatched

    anchored = not FIND_IN_STRING_RE.search(description)
    regex = (('^' if anchored else '') + lookaheads + body
              + ('$' if anchored else ''))
    try:
        re.compile(regex)
    except re.error as e:
        explanation.append(f"(internal: generated pattern failed to compile: {e})")
        return None, explanation, unmatched

    return regex, explanation, unmatched


class RegexGuiApp:
    def __init__(self, root):
        self.root = root
        root.title("Text → Regex Translator")
        root.geometry("760x700")
        root.minsize(600, 560)
        self.ollama_queue = queue.Queue()
        self.ollama_ping_queue = queue.Queue()

        pad = {'padx': 10, 'pady': (10, 0)}

        ttk.Label(root, text="Describe the pattern in plain English:").pack(anchor='w', **pad)
        self.desc_box = scrolledtext.ScrolledText(root, height=5, wrap='word', font=('Segoe UI', 10))
        self.desc_box.pack(fill='x', padx=10, pady=(2, 0))
        self.desc_box.insert(
            '1.0',
            "select only strings which have only numbers in the first position "
            "and characters 'aA' on the last position."
        )

        btn_row = ttk.Frame(root)
        btn_row.pack(fill='x', padx=10, pady=8)
        ttk.Button(btn_row, text="Generate Regex", command=self.generate).pack(side='left')
        ttk.Button(btn_row, text="Clear", command=self.clear).pack(side='left', padx=(8, 0))

        ttk.Label(root, text="Generated regex (editable):").pack(anchor='w', **pad)
        regex_row = ttk.Frame(root)
        regex_row.pack(fill='x', padx=10, pady=(2, 0))
        self.regex_var = tk.StringVar()
        self.regex_entry = ttk.Entry(regex_row, textvariable=self.regex_var, font=('Consolas', 11))
        self.regex_entry.pack(side='left', fill='x', expand=True)
        ttk.Button(regex_row, text="Copy", command=self.copy_regex).pack(side='left', padx=(6, 0))

        ttk.Label(root, text="Interpretation:").pack(anchor='w', **pad)
        self.explain_box = scrolledtext.ScrolledText(root, height=6, wrap='word', font=('Segoe UI', 9))
        self.explain_box.pack(fill='x', padx=10, pady=(2, 0))
        self.explain_box.configure(state='disabled')

        ai_frame = ttk.LabelFrame(root, text="Local AI fallback (Ollama — no subscription needed)")
        ai_frame.pack(fill='x', padx=10, pady=(8, 0))

        self.use_ollama_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            ai_frame, text="Auto-ask when heuristic parsing fails completely",
            variable=self.use_ollama_var
        ).grid(row=0, column=0, columnspan=4, sticky='w', padx=6, pady=(4, 2))

        self.ollama_alive_var = tk.StringVar(value='● checking…')
        self.ollama_alive_label = ttk.Label(
            ai_frame, textvariable=self.ollama_alive_var, foreground='#999')
        self.ollama_alive_label.grid(row=0, column=4, sticky='e', padx=6, pady=(4, 2))

        ttk.Label(ai_frame, text="Model:").grid(row=1, column=0, sticky='w', padx=(6, 2))
        self.ollama_model_var = tk.StringVar(value=DEFAULT_OLLAMA_MODEL)
        ttk.Entry(ai_frame, textvariable=self.ollama_model_var, width=16).grid(row=1, column=1, sticky='w')

        ttk.Label(ai_frame, text="Host:").grid(row=1, column=2, sticky='w', padx=(10, 2))
        self.ollama_host_var = tk.StringVar(value=DEFAULT_OLLAMA_HOST)
        ttk.Entry(ai_frame, textvariable=self.ollama_host_var, width=22).grid(row=1, column=3, sticky='w')

        self.ollama_btn = ttk.Button(ai_frame, text="Ask Local AI Now", command=self.ask_ollama_manual)
        self.ollama_btn.grid(row=1, column=4, sticky='w', padx=(10, 6))

        self.ollama_status_var = tk.StringVar(value='')
        ttk.Label(ai_frame, textvariable=self.ollama_status_var, foreground='#666').grid(
            row=2, column=0, columnspan=5, sticky='w', padx=6, pady=(2, 4))

        ttk.Separator(root, orient='horizontal').pack(fill='x', padx=10, pady=10)

        ttk.Label(root, text="Test strings (one per line):").pack(anchor='w', padx=10)
        self.test_box = scrolledtext.ScrolledText(root, height=6, wrap='none', font=('Consolas', 10))
        self.test_box.pack(fill='both', expand=True, padx=10, pady=(2, 0))

        ttk.Button(root, text="Test Match", command=self.test_match).pack(padx=10, pady=8, anchor='w')

        ttk.Label(root, text="Results:").pack(anchor='w', padx=10)
        self.result_box = scrolledtext.ScrolledText(root, height=6, wrap='none', font=('Consolas', 10))
        self.result_box.pack(fill='both', expand=True, padx=10, pady=(2, 10))
        self.result_box.configure(state='disabled')

        self._poll_ollama_alive()

    def _poll_ollama_alive(self):
        host = self.ollama_host_var.get().strip() or DEFAULT_OLLAMA_HOST

        def worker():
            self.ollama_ping_queue.put(check_ollama_alive(host))

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(200, self._check_ollama_ping_queue)

    def _check_ollama_ping_queue(self):
        try:
            alive = self.ollama_ping_queue.get_nowait()
        except queue.Empty:
            self.root.after(200, self._check_ollama_ping_queue)
            return
        if alive:
            self.ollama_alive_var.set('● Ollama running')
            self.ollama_alive_label.configure(foreground='#1a7f37')
        else:
            self.ollama_alive_var.set('● Ollama not detected')
            self.ollama_alive_label.configure(foreground='#c0392b')
        self.root.after(OLLAMA_STATUS_POLL_MS, self._poll_ollama_alive)

    def generate(self):
        description = self.desc_box.get('1.0', 'end').strip()
        regex, explanation, unmatched = text_to_regex(description)

        self.regex_var.set(regex if regex else '')

        self.explain_box.configure(state='normal')
        self.explain_box.delete('1.0', 'end')
        if explanation:
            for line in explanation:
                self.explain_box.insert('end', f"• {line}\n")
        if unmatched:
            self.explain_box.insert('end', "\nNot understood (regex not adjusted for these):\n")
            for line in unmatched:
                self.explain_box.insert('end', f"  ? {line}\n")
        if not regex:
            self.explain_box.insert(
                'end', "\nCould not build a regex from this description. "
                       "Try rephrasing, or edit the regex field manually.\n")
        self.explain_box.configure(state='disabled')

        # Only auto-fall-back when the heuristic produced nothing at all. A
        # non-empty `unmatched` alongside a real regex is often just
        # descriptive framing text ("find phone numbers...") rather than a
        # missed constraint, and auto-calling the (slower, less reliable)
        # AI would silently override a correct heuristic answer with it.
        if self.use_ollama_var.get() and regex is None:
            self._start_ollama_request(description)

    def _append_explain(self, text):
        self.explain_box.configure(state='normal')
        self.explain_box.insert('end', text)
        self.explain_box.configure(state='disabled')

    def ask_ollama_manual(self):
        description = self.desc_box.get('1.0', 'end').strip()
        if not description:
            return
        self._start_ollama_request(description)

    def _start_ollama_request(self, description):
        model = self.ollama_model_var.get().strip() or DEFAULT_OLLAMA_MODEL
        host = self.ollama_host_var.get().strip() or DEFAULT_OLLAMA_HOST
        self.ollama_btn.configure(state='disabled')
        self.ollama_status_var.set(f"Asking local model '{model}'…")

        def worker():
            try:
                regex, raw = ask_ollama(description, model=model, host=host)
                self.ollama_queue.put(('ok', regex, raw))
            except Exception as e:
                self.ollama_queue.put(('error', str(e), None))

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(150, self._poll_ollama_queue)

    def _poll_ollama_queue(self):
        try:
            status, a, b = self.ollama_queue.get_nowait()
        except queue.Empty:
            self.root.after(150, self._poll_ollama_queue)
            return

        self.ollama_btn.configure(state='normal')
        if status == 'ok':
            regex, raw = a, b
            if regex:
                self.ollama_status_var.set(f"AI suggestion: {regex}")
                self._append_explain(f"\n[Local AI via Ollama] suggested regex: {regex}\n")
                self.regex_var.set(regex)
            else:
                self.ollama_status_var.set("AI response did not contain a valid regex.")
                self._append_explain(f"\n[Local AI via Ollama] could not extract a valid regex "
                                      f"from the response:\n  {raw.strip()[:300]}\n")
        else:
            err = a
            self.ollama_status_var.set("Ollama unavailable — see interpretation panel.")
            self._append_explain(f"\n[Local AI via Ollama] {err}\n")

    def copy_regex(self):
        value = self.regex_var.get()
        if not value:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(value)

    def clear(self):
        self.desc_box.delete('1.0', 'end')
        self.regex_var.set('')
        self.ollama_status_var.set('')
        for box in (self.explain_box, self.result_box):
            box.configure(state='normal')
            box.delete('1.0', 'end')
            box.configure(state='disabled')
        self.test_box.delete('1.0', 'end')

    def test_match(self):
        pattern_str = self.regex_var.get().strip()
        self.result_box.configure(state='normal')
        self.result_box.delete('1.0', 'end')
        if not pattern_str:
            self.result_box.insert('end', "No regex to test. Generate one first.\n")
            self.result_box.configure(state='disabled')
            return
        try:
            pattern = re.compile(pattern_str)
        except re.error as e:
            self.result_box.insert('end', f"Regex does not compile: {e}\n")
            self.result_box.configure(state='disabled')
            return

        lines = self.test_box.get('1.0', 'end').splitlines()
        lines = [l for l in lines if l.strip() != '']
        if not lines:
            self.result_box.insert('end', "No test strings entered.\n")
        for line in lines:
            ok = pattern.search(line) is not None
            self.result_box.insert('end', f"[{'MATCH' if ok else 'FAIL '}] {line}\n")
        self.result_box.configure(state='disabled')


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if 'vista' in style.theme_names():
            style.theme_use('vista')
    except Exception:
        pass
    RegexGuiApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
