# speech2regex

A small Tkinter GUI that translates a plain-English description of a string
pattern into a regular expression.

It is **not** a general NLU system — it's a rule-based/heuristic parser that
recognizes common phrasings for position, character classes, containment and
length constraints, and combines them into a regex. Anything it doesn't
recognize is listed as a warning, and the generated regex is editable so you
can hand-tune it.

## Installation

### 1. Python

Requires Python 3 with Tk (the standard `tkinter` module, bundled with most
Python installs on Windows — nothing extra to install). No third-party
Python packages are required; the app runs fully on the standard library.

Optionally, `pip install word2number` extends the heuristic's number-word
recognition beyond 0-10 (e.g. "twelve digits", "twenty characters") for the
**sequence** phrasing described below. Without it, that phrasing still
works for the numbers zero through ten.

### 2. Ollama + the `tinyllama` model (optional, for the AI fallback)

The AI fallback (see below) needs [Ollama](https://ollama.com), a free
program that runs small open language models entirely on your own PC — no
account, no API key, no subscription, nothing sent over the internet.
`tinyllama` is one of the models Ollama can run: a small (~1.1B-parameter)
open model, chosen here because it was the fastest and most accurate of the
lightweight models tested on ordinary CPU-only hardware (no GPU required).

To set it up:

1. Download and install Ollama for Windows from https://ollama.com/download
   and run the installer (default options are fine).
2. Ollama installs a background service that listens on
   `http://127.0.0.1:11434`. It normally starts automatically (look for its
   icon in the system tray); if it's ever not running, start it from a
   terminal with:
   ```
   ollama serve
   ```
3. Pull the `tinyllama` model (one-time download, ~637 MB, then works fully
   offline):
   ```
   ollama pull tinyllama
   ```
4. That's it — no further configuration. The app talks to Ollama
   automatically whenever the AI fallback is used.

If you skip this step, the app still works fully for everything the
heuristic parser can handle — you'll just see a friendly "Ollama not
reachable" message instead of an AI suggestion when it's needed.

### 3. Run the app

```
python speech2regex.py
```

## Usage

1. Type a description in the top box, e.g.:

   > select only strings which have only numbers in the first position and characters 'aA' on the last position.

2. Click **Generate Regex**. This produces:

   ```
   ^\d.*[aA]$
   ```

   along with a plain-English breakdown of what was understood, and a list
   of any parts of the description that weren't recognized.

3. The regex field is editable — tweak it directly if the heuristic missed
   something.

4. Enter one or more test strings (one per line) in the **Test strings** box
   and click **Test Match** to see which ones match. This checks whether the
   pattern is found anywhere in each line (like `re.search`), not just at
   the very start — so an unanchored regex (no `^`) will match a string that
   merely contains it.

## Local AI fallback (Ollama)

For phrasing the heuristic parser can't confidently handle, the app can ask
a local Ollama model to suggest a regex instead. See **Installation**
above for how to set up Ollama and pull the default model (`tinyllama`).

In the app, the **Local AI fallback** panel lets you set the model name
and host (defaults: `tinyllama:latest` at `http://127.0.0.1:11434`).

- With "Auto-ask when heuristic parsing fails completely" checked, it's
  called automatically only when the heuristic produces no regex at all.
  If the heuristic found a working regex but left some descriptive text
  unmatched (shown in the Interpretation panel), it is *not* auto-called —
  that leftover text is often just framing ("find phone numbers...") rather
  than a missed constraint, and the AI's answer is frequently less reliable
  than the heuristic's, so it shouldn't silently override a working result.
- **Ask Local AI Now** calls it manually at any time, e.g. if you suspect
  the unmatched text really does matter.
- The suggested regex is dropped into the (still editable) regex field,
  and the raw response is shown if it couldn't be parsed into a valid
  pattern. If Ollama isn't running or the model isn't pulled, the panel
  shows a plain-language error instead of failing silently.

Responses are capped at 64 generated tokens and stop at the first newline,
so a request can't run away rambling for minutes past a one-line answer —
if a request does take unexpectedly long, it's more likely stuck loading
the model for the first time than generating.

This is a suggestion from a small local model, not a guarantee — always
check it against the "Interpretation" panel and your test strings.
`tinyllama` is fast (single-digit seconds on CPU) and was the most
structurally correct of the lightweight models tested during development,
but it can still miss part of a request — e.g. on a "password with at
least 8 characters, containing an uppercase letter, a lowercase letter, a
digit, and a special character" prompt, it correctly used lookaheads for
three of the four required character classes but dropped the
special-character one.

If your machine has a GPU (or you don't mind waiting), you can point the
Model field at a larger pulled model (e.g. `llama3.2` or `qwen3:4b`) for
potentially better answers — on CPU-only hardware, "thinking" models like
the `qwen3`/`deepseek-r1` families were tested and found too slow to be
practical (over a minute per request).

## What it understands

- **Position**: "first/second/... position or character", "last position",
  "second to last", "3rd position", "starts with", "ends with"
- **Character classes**: "numbers/digits", "letters", "uppercase/lowercase
  letters", "vowels", "consonants", "spaces", or an explicit quoted set of
  characters like `'aA'`
- **Whole-string constraints**: "only \<class\>" when no position is given
  (applies to every character, not just one position)
- **Position ranges with literal alternatives**: "position N-M can be 'A' or
  'B' or 'C'" builds a multi-character alternation at that span, e.g.
  "position 3-4 can be '20' or '30' or '70'" → `(?:20|30|70)` at characters
  3-4. Also accepts unquoted lists ("20, 30 or 70") and "N to M" / "N
  through M" instead of "N-M".
- **Containment**: "contains X" / "does not contain X"
- **Length**: "exactly N characters", "at least/at most N characters",
  "between N and M characters", "length > N", "length >= N", "longer than N
  characters", "shorter than N characters"
- **Sequences**: "A then B then C" style descriptions build an ordered,
  literal composition instead of a whole-string template — useful for
  formats like phone numbers or IDs that don't fit the position/only
  framing above. Each `then`-separated part can be:
  - a literal token, e.g. `06`
  - slash-separated alternatives, e.g. `20/30/70` → `(?:20|30|70)`
  - a separator word: `slash`, `dash`, `hyphen`, `underscore`, `dot`, `period`
  - a quoted literal, e.g. `'foo'`
  - a count + character class, e.g. `seven digits` → `\d{7}`
  - a bare character class, e.g. `letters` → `[A-Za-z]+`

  Add `sample: <example>` at the end to have the generated regex
  automatically checked against a known-good example, reported in the
  Interpretation panel.

  By default the sequence is anchored to the whole string (`^...$`); phrase
  the description as "find X **in a string**", "**anywhere in the**
  string", or "**substrings before and after**" to instead search for it as
  a substring anywhere. The same unanchoring hint also applies to the
  position/range-based heuristics above, not just sequences.

## Limitations

- Combining many overlapping/contradictory constraints in one sentence can
  produce a regex that isn't what you meant — always check the
  "Interpretation" panel and test with sample strings.
- Only ASCII-oriented character classes are built in; there's no locale or
  Unicode-category support.
- The heuristic parser alone can't handle unusual phrasing — rephrase, hand-
  edit the regex, or use the optional local AI fallback described above.
