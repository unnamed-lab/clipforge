"""Bundled caption fonts and real font metrics.

The public font names are intentionally familiar (SF Pro Display, Helvetica,
Impact, The Bold Font) but the binaries shipped here are redistributable
substitutes. The registry records both names so the UI can be honest about the
substitution and a render remains reproducible offline.

Only the selected font is copied into a render workspace. libass resolves fonts
by family, so copying both Montserrat Bold and Montserrat Black into the same
directory would make the selected weight ambiguous.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

FONT_DIR = Path(__file__).resolve().parent / "fonts"
LICENSE_FILE = FONT_DIR / "OFL.txt"
NOTICE_FILE = FONT_DIR / "NOTICE.md"

_FILES = {
    "montserrat-black": "Montserrat-Black.ttf",
    "montserrat-bold": "Montserrat-Bold.ttf",
    "bebas-neue": "BebasNeue-Regular.ttf",
    "anton": "Anton-Regular.ttf",
    "archivo-black": "ArchivoBlack-Regular.ttf",
    "lato-bold": "Lato-Bold.ttf",
    "inter": "Inter-Variable.ttf",
}


@dataclass(frozen=True)
class FontSelection:
    requested: str
    family: str
    file: str
    weight: int
    substituted: bool = False
    note: str = ""

    @property
    def path(self) -> Path:
        return FONT_DIR / self.file

    @property
    def display_name(self) -> str:
        return self.family if not self.substituted else f"{self.family} (for {self.requested})"

    @property
    def subtitle(self) -> str:
        if not self.substituted:
            return f"{self.family} · bundled"
        return f"{self.family} · redistributable substitute for {self.requested}"


@lru_cache(maxsize=32)
def _family(file: str) -> str:
    try:
        return ImageFont.truetype(str(FONT_DIR / file), 48).getname()[0]
    except OSError:
        return Path(file).stem.replace("-", " ")


def _selection(requested: str, file: str, weight: int, *, substituted: bool, note: str = "") -> FontSelection:
    return FontSelection(
        requested=requested,
        family=_family(file),
        file=file,
        weight=weight,
        substituted=substituted,
        note=note,
    )


def resolve_font(family: str, *, weight: str = "auto", bold: bool = True) -> FontSelection:
    """Resolve a user-facing family to a bundled font file.

    Unknown families fall back to a bundled grotesque rather than silently
    asking libass for a font that does not exist on the machine.
    """
    name = " ".join((family or "").split()).strip()
    key = name.casefold()
    heavy = weight == "black" or (weight == "auto" and bold)

    if key in {"montserrat", "montserrat black", "montserrat bold"}:
        if weight == "black" or key.endswith(" black"):
            return _selection(name or "Montserrat", _FILES["montserrat-black"], 900, substituted=False)
        return _selection(name or "Montserrat", _FILES["montserrat-bold"], 700, substituted=False,)

    if key in {"the bold font", "theboldfont", "bold font"}:
        return _selection(
            "The Bold Font",
            _FILES["archivo-black"],
            900,
            substituted=True,
            note="Archivo Black is bundled as the redistributable equivalent.",
        )
    if key in {"sf pro display", "sf pro", "sf-pro-display"}:
        return _selection(
            "SF Pro Display",
            _FILES["inter"],
            700,
            substituted=True,
            note="Inter is bundled because SF Pro Display cannot be redistributed.",
        )
    if key in {"helvetica", "helvetica neue", "arial", "liberation sans"}:
        return _selection(
            name or "Helvetica",
            _FILES["lato-bold"],
            700,
            substituted=key not in {"liberation sans"},
            note="Lato is bundled as the redistributable metric-compatible substitute.",
        )
    if key in {"impact"}:
        return _selection(
            "Impact",
            _FILES["anton"],
            900,
            substituted=True,
            note="Anton is bundled as the redistributable condensed display face.",
        )
    if key in {"bebas neue", "bebasneue"}:
        return _selection("Bebas Neue", _FILES["bebas-neue"], 700, substituted=False)
    if key == "anton":
        return _selection("Anton", _FILES["anton"], 900, substituted=False)
    if key in {"archivo black", "archivoblack"}:
        return _selection("Archivo Black", _FILES["archivo-black"], 900, substituted=False)
    if key in {"inter"}:
        return _selection("Inter", _FILES["inter"], 700, substituted=False)
    if key in {"lato"}:
        return _selection("Lato", _FILES["lato-bold"], 700, substituted=False)

    return _selection(
        name or "Montserrat",
        _FILES["montserrat-bold"] if heavy else _FILES["lato-bold"],
        700 if heavy else 400,
        substituted=True,
        note="Unknown font family; using a bundled fallback.",
    )


def caption_font_choices() -> list[dict]:
    """Stable data for the web font picker."""
    choices = [
        ("Montserrat", "Montserrat", "Bold", "Geometric sans with a strong vertical rhythm."),
        ("Montserrat Black", "Montserrat Black", "Black", "Heavier Montserrat for loud vertical captions."),
        ("The Bold Font", "Archivo Black (substitute)", "Black", "Heavy grotesque with a compact footprint."),
        ("Bebas Neue", "Bebas Neue", "Regular", "Tall condensed caps; excellent for short phrases."),
        ("Anton", "Anton (substitute for Impact)", "Black", "Very condensed display sans."),
        ("Inter", "Inter (substitute for SF Pro Display)", "Bold", "Neutral UI face with a tall x-height."),
        ("Lato", "Lato (substitute for Helvetica)", "Bold", "Humanist grotesque; close Helvetica rhythm."),
        ("Helvetica", "Lato (substitute)", "Bold", "Helvetica cannot be bundled; Lato is used."),
        ("Impact", "Anton (substitute)", "Black", "Condensed, high-impact lettering."),
    ]
    return [
        {"id": name, "label": name, "family": family, "weight": weight, "hint": hint}
        for name, family, weight, hint in choices
    ]


@lru_cache(maxsize=96)
def _font(file: str, size: int, weight: int) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(FONT_DIR / file), max(8, int(size)))
    if "Variable" in file or "[" in file:
        try:
            font.set_variation_by_axes([14, int(weight)])
        except (OSError, ValueError):
            pass
    return font


def text_width(text: str, selection: FontSelection, size: int) -> float:
    """Advance width in output pixels, measured with the actual font file."""
    if not text:
        return 0.0
    return float(_font(selection.file, size, selection.weight).getlength(text))


def text_bbox(text: str, selection: FontSelection, size: int) -> tuple[int, int, int, int]:
    return _font(selection.file, size, selection.weight).getbbox(text)


def copy_fonts(destination: Path, selection: FontSelection) -> list[str]:
    """Copy the one font this render needs plus its licence.

    FFmpeg's ``ass`` filter receives ``fontsdir=fonts`` relative to the render
    workspace, so no host font installation is required.
    """
    destination.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    source = selection.path
    if not source.exists():
        raise FileNotFoundError(f"Bundled font is missing: {source.name}")
    target = destination / selection.file
    shutil.copyfile(source, target)
    copied.append(target.name)
    for licence in (LICENSE_FILE, NOTICE_FILE):
        if licence.exists():
            shutil.copyfile(licence, destination / licence.name)
            copied.append(licence.name)
    return copied
