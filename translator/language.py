import re
from dataclasses import dataclass
from functools import lru_cache

from langdetect import DetectorFactory, LangDetectException, detect_langs
from lingua import Language as LinguaLanguage
from lingua import LanguageDetectorBuilder

DetectorFactory.seed = 0
NOISE = re.compile(r"https?://\S+|www\.\S+|@[\w]+|```[\s\S]*?```|`[^`]*`", re.I)
CYRILLIC = re.compile(r"[\u0400-\u052f]")


@lru_cache(maxsize=1)
def cyrillic_detectors():
    # First rule out other Cyrillic languages, then distinguish the two chat
    # languages. A 55-language classifier is unreliable on short RU/UK phrases.
    multilingual = LanguageDetectorBuilder.from_all_languages_with_cyrillic_script().build()
    ukrainian_russian = LanguageDetectorBuilder.from_languages(
        LinguaLanguage.UKRAINIAN,
        LinguaLanguage.RUSSIAN,
    ).build()
    return multilingual, ukrainian_russian


@dataclass(frozen=True)
class Language:
    code: str = "unknown"
    confidence: float = 0
    letters: int = 0
    has_ukrainian_letters: bool = False

    @property
    def ukrainian(self):
        return self.code == "uk" and self.confidence >= 0.85

    def clear_russian(self, threshold=0.75, min_letters=3):
        return (
            self.code == "ru"
            and self.confidence >= threshold
            and self.letters >= min_letters
            and not self.has_ukrainian_letters
        )


def detect_language(text: str) -> Language:
    clean = NOISE.sub(" ", text)
    letters = sum(c.isalpha() for c in clean)
    uk_letters = bool(re.search("[іїєґ]", clean, re.I))
    if letters < 2:
        return Language(letters=letters, has_ukrainian_letters=uk_letters)
    if len(CYRILLIC.findall(clean)) >= letters / 2:
        multilingual, ukrainian_russian = cyrillic_detectors()
        candidates = multilingual.compute_language_confidence_values(clean)
        if not candidates:
            return Language(letters=letters, has_ukrainian_letters=uk_letters)
        best = candidates[0]
        if best.language in {LinguaLanguage.RUSSIAN, LinguaLanguage.UKRAINIAN} or best.value < 0.65:
            best = ukrainian_russian.compute_language_confidence_values(clean)[0]
        return Language(best.language.iso_code_639_1.name.lower(), best.value, letters, uk_letters)
    try:
        best = detect_langs(clean)[0]
        return Language(best.lang, best.prob, letters, uk_letters)
    except LangDetectException:
        return Language(letters=letters, has_ukrainian_letters=uk_letters)
