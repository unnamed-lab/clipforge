"""Small, explicit lexicons used by the deterministic ranker.

These are heuristics, not a model. They are intentionally readable so a user can
audit why a clip scored the way it did.
"""

from __future__ import annotations

STOPWORDS = frozenset(
    """
    a about above after again against all am an and any are aren't as at be because been before being below
    between both but by can can't cannot could couldn't did didn't do does doesn't doing don't down during each
    few for from further had hadn't has hasn't have haven't having he he'd he'll he's her here here's hers herself
    him himself his how how's i i'd i'll i'm i've if in into is isn't it it's its itself let's me more most mustn't
    my myself no nor not of off on once only or other ought our ours ourselves out over own same shan't she she'd
    she'll she's should shouldn't so some such than that that's the their theirs them themselves then there there's
    these they they'd they'll they're they've this those through to too under until up very was wasn't we we'd
    we'll we're we've were weren't what what's when when's where where's which while who who's whom why why's with
    won't would wouldn't you you'd you'll you're you've your yours yourself yourselves just really actually
    basically literally kind of sort of like well okay ok yeah right thing things stuff gonna wanna got get
    """.split()
)

FILLER_STARTS = (
    "um", "uh", "erm", "ah", "like", "so", "yeah", "okay", "ok", "right", "well",
    "i mean", "you know", "anyway", "alright",
)

CONTRASTIVE_STARTS = ("but", "however", "and yet", "that said", "although", "though", "whereas", "except")

ANAPHORA_STARTS = (
    "this", "that", "these", "those", "it", "they", "he", "she", "them", "him", "her",
    "such", "the same", "which", "therefore", "thus", "consequently", "as i said",
    "like i said", "earlier", "previously", "back then", "that's why", "that is why",
)

CURIOSITY_MARKERS = (
    "how to", "why ", "what nobody", "the reason", "the problem", "the thing is",
    "here's", "here is", "nobody tells you", "most people", "everyone thinks",
    "the truth", "what happened", "turns out", "secret", "mistake", "lesson",
)

EMOTION_WORDS = frozenset(
    """
    love hate scared afraid terrified excited amazing incredible awful terrible painful beautiful
    heartbreaking hilarious funny laugh crying cry angry furious embarrassed proud grateful hope
    desperate shocked stunned unbelievable wild crazy brutal devastating thrilled nervous anxious
    """.split()
)

INTENSIFIERS = frozenset("very really absolutely completely totally extremely insanely incredibly super dead".split())

ANECDOTE_MARKERS = (
    "i remember", "the day i", "one time", "when i was", "we were", "i once",
    "there was this", "so there i was", "my friend", "my dad", "my mom", "my boss",
)

DEBATE_MARKERS = (
    "disagree", "wrong", "actually", "the problem with", "i don't think", "that's not true",
    "nonsense", "bullshit", "the reality is", "let me push back", "controversial", "unpopular opinion",
)

TUTORIAL_MARKERS = (
    "step", "first you", "then you", "you need to", "make sure", "the way to", "how you",
    "the trick is", "pro tip", "for example", "in practice", "you should", "start by",
)

QUOTE_SHAPES = (
    "the best", "the worst", "never", "always", "nobody", "everyone", "if you", "you can't",
    "you don't", "the only", "the biggest", "most people",
)

SECOND_PERSON = frozenset("you your yours yourself yourselves".split())

AFFECT_PUNCT = ("!", "!!", "...")

WEAK_ENDINGS = ("and", "but", "so", "because", "which", "that", "if", "when", "or", "then", "with")

QUESTION_WORDS = ("what", "why", "how", "when", "who", "where", "which", "do", "does", "did", "can", "should", "would", "is", "are")
