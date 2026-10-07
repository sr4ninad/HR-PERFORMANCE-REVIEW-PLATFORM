"""Blocks profanity, slurs, insults and unprofessional slang in review text.

Matching rules, chosen to catch real abuse without flagging normal words:

* **Whole words only** for almost everything, using letter boundaries, so
  "assess", "class", "Scunthorpe", "cockpit", "Dickens" and "scrap" are fine.
* **Inflections and compounds** of swear words are covered by an explicit
  list of prefixes and suffixes: fuck -> fucker, fucking, fuckin;
  shit -> shitty, bullshit, shithead; ass -> asshole, dumbass, jackass.
* **Disguised spellings**: common symbol/number swaps (f*ck, sh1t, $hit,
  @ss) and stretched letters (fuuuck, shiiit) still match.
* **"fuck" is matched inside any word** too (e.g. "clusterfuck"), since no
  normal English word contains it.

No word list is complete -- a determined person can always get creative --
so this is a first line of defence, not a guarantee.

(Phase 6 history: the original prototype's filter matched unanchored
substrings, so "mad" flagged "made", and showed one popup per bad word.
This keeps the single pass and the whole-word matching from that fix.)
"""

import re

# Swear words: matched with the prefixes/suffixes below.
PROFANITY_ROOTS = [
    "fuck", "shit", "ass", "arse", "bitch", "bastard", "cunt", "dick", "cock",
    "prick", "pussy", "twat", "wank", "piss", "crap", "damn", "hell", "bollock",
    "bugger", "douche", "slut", "whore", "jizz", "tit", "boob", "bloody", "suck",
]
_PREFIXES = ["mother", "bull", "horse", "dip", "bat", "ape", "dumb", "jack", "smart", "fat",
             "lard", "bad", "kick", "god", "cluster", "half"]
_SUFFIXES = ["s", "es", "ed", "er", "ers", "ing", "in", "y", "ies", "head", "heads", "hole",
             "holes", "face", "faces", "wit", "wits", "bag", "bags", "show", "storm", "load",
             "loads", "off", "tard", "ty", "tastic"]

# Insults, slurs and unprofessional slang: whole words, plus a plural "s"/"es".
INSULTS_AND_SLURS = [
    # insults
    "idiot", "idiotic", "stupid", "stupidity", "moron", "moronic", "imbecile", "dumb", "dummy",
    "dumbwitted", "brainless", "mindless", "senseless", "useless", "worthless", "pathetic",
    "loser", "jerk", "scumbag", "dimwit", "nitwit", "halfwit", "dunce", "clown", "freak",
    "goofy", "foolish", "lame", "crazy", "insane", "mad", "psycho", "lunatic", "trash",
    # slurs
    "retard", "retarded", "fag", "faggot", "nigger", "nigga", "tranny", "spic",
    "kike", "dyke", "gypsy", "cripple",
    # unprofessional slang and abbreviations
    "wtf", "stfu", "gtfo", "lmao", "lmfao", "omfg", "fml", "ffs",
]
# Phrases (matched as whole phrases, any spacing).
PHRASES = ["shut up", "screw you", "screw off", "piss off"]

# Matched anywhere, even inside another word.
EMBEDDED_ROOTS = ["fuck"]

# Look-alike characters people use to slip past filters.
_SUBSTITUTES = {
    "a": "a@4*", "e": "e3*", "i": "i1!*", "o": "o0*", "u": "uv*", "s": "s$5", "t": "t7", "l": "l1",
}
_WORDCHAR = r"[a-z0-9@$*!]"  # characters that count as "part of a word" here


def _fuzzy(word: str) -> str:
    """Regex for `word` allowing look-alike swaps and stretched letters."""
    parts = []
    for ch in word:
        if ch in _SUBSTITUTES:
            parts.append("[" + re.escape(_SUBSTITUTES[ch]) + "]+")
        elif ch == " ":
            parts.append(r"\s+")
        else:
            parts.append(re.escape(ch) + "+")
    return "".join(parts)


def _alternation(words, fuzzy: bool = True) -> str:
    # Longest first, so "faggot" is tried before "fag".
    ordered = sorted(set(words), key=len, reverse=True)
    return "|".join(_fuzzy(w) if fuzzy else re.escape(w) for w in ordered)


_START = r"(?<![a-z0-9])"
_END = r"(?![a-z0-9])"

_PATTERN = re.compile(
    "|".join(
        [
            # swear word, with an optional compound prefix and inflection
            # suffix. Only the swear word itself is fuzzy: a stretchy suffix
            # would let "ass" + "es" match "assess".
            f"{_START}(?:{_alternation(_PREFIXES, fuzzy=False)})?(?:{_alternation(PROFANITY_ROOTS)})"
            f"(?:{_alternation(_SUFFIXES, fuzzy=False)})?{_END}",
            # insults, slurs and slang, plus a plural
            f"{_START}(?:{_alternation(INSULTS_AND_SLURS)})(?:e?s)?{_END}",
            # phrases
            f"{_START}(?:{_alternation(PHRASES)}){_END}",
            # roots that are offensive even inside another word
            f"{_WORDCHAR}*(?:{_alternation(EMBEDDED_ROOTS)}){_WORDCHAR}*",
        ]
    ),
    re.IGNORECASE,
)

# Every listed term, for reference and tests.
BANNED_WORDS = sorted(set(PROFANITY_ROOTS + INSULTS_AND_SLURS + PHRASES + EMBEDDED_ROOTS))


def find_banned_language(*texts: str | None) -> list[str]:
    """Every offending word or phrase across all `texts`, lowercased and
    de-duplicated, in the order found -- so a form can report them all at
    once instead of one at a time."""
    found: list[str] = []
    for text in texts:
        if not text:
            continue
        for match in _PATTERN.finditer(text):
            word = re.sub(r"\s+", " ", match.group(0).lower())
            if word not in found:
                found.append(word)
    return found
