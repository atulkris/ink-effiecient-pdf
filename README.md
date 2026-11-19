# Ink-Efficient Presentation to 2-Up A4 PDF

Convert PowerPoint and PDF presentations into printer-friendly PDFs that use far
less ink, without losing text, diagrams or photos.

Designed for lecture slides and decks with dark backgrounds, coloured header
bands and light text. Input: `.pptx`, `.ppt`, `.pdf`. Each input file becomes
its own output PDF (files are never merged).

## What it does

| Original | Printed result |
|---|---|
| Dark / gradient slide background | Pure white (no faint tint) |
| White or light text | Black / dark grey (light grey stays lighter than white, so hierarchy survives) |
| Yellow, cyan, orange, red text | Same hue, darkened until readable on white |
| Coloured boxes and header bands | Pale tint of the same colour + thin outline |
| White shapes on a dark background | White with an outline |
| Thin lines, table grids | Darkened, kept |
| Photos and figures | Untouched |
| Slides that are already light | Kept exactly as-is (vector) |

- **Text stays vector**: on converted pages the text layer is kept as real PDF
  text (sharp, selectable, searchable) and only its colours are rewritten. The
  artwork behind it is recoloured as an image. Image-only slides fall back to
  full image processing automatically.
- **Pure black text** is written as greyscale, so printers use black ink rather
  than mixing colours; colourless converted pages are stored as greyscale.
- **2 slides per portrait A4 sheet** with a hairline border around each slide.
- Lossless final optimisation with `qpdf`.

Typical results: a dark-theme slide drops from ~100% to ~6% ink coverage; a
white slide with a dark header band drops by ~85%.

```text
┌──────────────────────────────┐
│ ┌──────────────────────────┐ │
│ │         SLIDE 1          │ │
│ └──────────────────────────┘ │
│ ┌──────────────────────────┐ │
│ │         SLIDE 2          │ │
│ └──────────────────────────┘ │
└──────────────────────────────┘
              A4
```

## Requirements

- Python 3.10+
- LibreOffice (for `.ppt` / `.pptx`)
- qpdf (optional, final lossless optimisation)

```bash
sudo apt update
sudo apt install libreoffice qpdf python3-venv
```

## Installation

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## Usage

In every new terminal, activate the virtual environment first:

```bash
cd /path/to/pptx-ink-efficient
source venv/bin/activate
python app.py "/path/to/presentations"
```

Every `.pptx`, `.ppt` and `.pdf` in that folder is converted; each gets its own
output file. Example: `python app.py ~/Documents/lectures`.

Output goes to `/path/to/presentations/output/`, e.g.
`Lecture 1.pptx` → `output/Lecture 1_2up_A4.pdf`.

Custom output folder:

```bash
python app.py "/path/to/presentations" "/path/to/print-ready"
```

Options:

| Option | Effect |
|---|---|
| `--workers N` | Files processed in parallel (default: up to 4) |
| `--raster-only` | Force full image processing on every page. Not needed normally: each page is handled automatically |
| `--no-border` | Don't draw the hairline border around each slide |

With `--workers 1` a per-page log is printed, for example:

```text
page 001: CONVERT | ink 104.7%->5.7% (-95%) | vector-text | accepted
page 002: KEEP    | ink 1.0% | already efficient
```

## How it works

```text
PPT / PPTX ──LibreOffice (original, unmodified)──┐
PDF ─────────────────────────────────────────────┤
                                                 ▼
                      per page: measure ink (CMYK estimate)
                                 │
              already light ─────┴───── worth converting
                    │                         │
                  keep            text layer ──► recolour to dark (vector)
                                  artwork ────► recolour as image
                                                 │
                                   check the saving, else keep original
                                                 ▼
                                  2-up A4 + border ─► qpdf
```

Recolouring compares every pixel with its *local* background:

1. Thin features (text, lines, icons — under ~16 pt wide) are foreground;
   larger areas are background/panels.
2. The slide background (including gradients) becomes white; other coloured
   panels become a pale tint with a thin outline.
3. Foreground keeps its hue but moves to a dark, readable lightness.
   Anti-aliased edges are un-mixed first, so text edges stay smooth.
4. Large areas are never inverted from light to dark (safety net).
5. Embedded photos/figures are detected and left untouched.

## Notes and limitations

- Ink coverage is an estimate from the rendered page, not a printer driver
  measurement.
- Thin parts of large shapes (arrow tips, narrow gaps between bars) are
  recognised as part of the shape and follow its colour instead of being
  inverted.
- Text drawn over a photo is darkened like any other text.
- All pages, including large posters, are processed at full resolution. The
  page is processed in strips across CPU cores, so memory stays bounded.
- The mode is chosen automatically per page: pages with a text layer use
  vector text; image-only pages (scans, pictures of slides) or pages that fail
  the vector safety check use full image processing. `--raster-only` only
  forces the image path and is not needed normally.
- Image processing handles coloured bars/boxes filled with text (the bar
  becomes a pale tint, the text dark). Very small body text on image-only
  posters can still show an occasional speck.
- Some PPTX files exported by third-party tools cannot be opened by LibreOffice
  ("source file could not be loaded"); export those to PDF first and use the PDF.

## Troubleshooting

- `libreoffice: command not found` → `sudo apt install libreoffice`
- `qpdf: command not found` → `sudo apt install qpdf` (optional)
- `ModuleNotFoundError` → `source venv/bin/activate && pip install -r requirements.txt`
