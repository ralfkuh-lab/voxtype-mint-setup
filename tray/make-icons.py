#!/usr/bin/env python3
"""Erzeugt die Tray-Icons aus dem generierten Basis-Icon (mic-base.png).

Das Basis-Icon kommt aus GPT Image 2 und hat einen eingebackenen
Schachbrett-Hintergrund (kein Alpha). Dieses Skript extrahiert den weissen
Mikrofon-Glyph (Schachbrett-Grautoene ~211/~233, Glyph ~254) und baut daraus
drei Zustands-Icons mit echter Transparenz:

  mic-idle.png         - grauer Glyph, kein Hintergrund
  mic-recording.png    - weisser Glyph auf gruenem Kreis
  mic-transcribing.png - weisser Glyph auf gelbem Kreis
"""
from PIL import Image, ImageDraw

SRC = "icons/mic-base.png"
OUT_SIZE = 256
SUPERSAMPLE = 4  # Kreis in hoher Aufloesung zeichnen, dann herunterskalieren

GREY_MAX = 238   # alles unterhalb ist Hintergrund
WHITE_REF = 252  # ab hier voll deckend

IDLE_TINT = (168, 176, 184)      # dezentes Grau fuer den Ruhezustand
COLOR_RECORDING = (46, 158, 79)   # Gruen
COLOR_TRANSCRIBING = (224, 163, 28)  # Amber


def extract_glyph() -> Image.Image:
    """Weissen Glyph mit weichem Alpha aus dem Schachbrett-Bild loesen."""
    lum = Image.open(SRC).convert("L")
    scale = 255.0 / (WHITE_REF - GREY_MAX)
    alpha = lum.point(lambda v: max(0, min(255, int((v - GREY_MAX) * scale))))
    glyph = Image.new("RGBA", lum.size, (255, 255, 255, 0))
    glyph.putalpha(alpha)
    return glyph.crop(alpha.getbbox())


def tint(glyph: Image.Image, rgb) -> Image.Image:
    out = Image.new("RGBA", glyph.size, rgb + (0,))
    out.putalpha(glyph.getchannel("A"))
    return out


def on_canvas(glyph: Image.Image, canvas_px: int, glyph_frac: float) -> Image.Image:
    """Glyph zentriert und proportional auf quadratische Flaeche setzen."""
    canvas = Image.new("RGBA", (canvas_px, canvas_px), (0, 0, 0, 0))
    target = int(canvas_px * glyph_frac)
    ratio = min(target / glyph.width, target / glyph.height)
    resized = glyph.resize(
        (max(1, int(glyph.width * ratio)), max(1, int(glyph.height * ratio))),
        Image.LANCZOS,
    )
    canvas.alpha_composite(
        resized,
        ((canvas_px - resized.width) // 2, (canvas_px - resized.height) // 2),
    )
    return canvas


def with_circle(glyph: Image.Image, rgb) -> Image.Image:
    px = OUT_SIZE * SUPERSAMPLE
    img = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = px // 32
    draw.ellipse((margin, margin, px - margin, px - margin), fill=rgb + (255,))
    img.alpha_composite(on_canvas(glyph, px, 0.60))
    return img.resize((OUT_SIZE, OUT_SIZE), Image.LANCZOS)


def main():
    glyph = extract_glyph()

    idle = on_canvas(tint(glyph, IDLE_TINT), OUT_SIZE * SUPERSAMPLE, 0.82)
    idle.resize((OUT_SIZE, OUT_SIZE), Image.LANCZOS).save("icons/mic-idle.png")

    with_circle(glyph, COLOR_RECORDING).save("icons/mic-recording.png")
    with_circle(glyph, COLOR_TRANSCRIBING).save("icons/mic-transcribing.png")
    print("Icons geschrieben: mic-idle.png, mic-recording.png, mic-transcribing.png")


if __name__ == "__main__":
    main()
