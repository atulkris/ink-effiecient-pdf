# CLAUDE.md

Single-file tool (`app.py`) that turns `.ppt`/`.pptx`/`.pdf` presentations into
ink-efficient, 2-slides-per-A4 PDFs. See README.md for user-facing docs.

## Run

```bash
source venv/bin/activate            # Python deps: PyMuPDF, numpy, opencv-python-headless, Pillow
python app.py <input_folder> [output_folder] [--workers N] [--raster-only] [--no-border]
```

System deps: `libreoffice` (PPT/PPTX only), `qpdf` (optional). Output defaults to
`<input_folder>/output/<name>_2up_A4.pdf`. `--workers 1` prints a per-page log.

## Pipeline (in order)

1. `libreoffice_convert_original` – PPT/PPTX -> PDF, **unmodified** (never edit
   the PowerPoint itself; masters/themes break). PDFs skip this step.
2. `optimise_page` per page: `ink_coverage` (CMYK-style estimate) gates it. Pages
   at or below `PAGE_ALREADY_EFFICIENT` are kept as vector. Mode is chosen
   automatically:
   - `convert_hybrid` (page has a text layer): text is redacted out, the
     artwork is rendered and recoloured by `recolor_raster`, and the text layer
     is kept as **vector** with colours rewritten by `rewrite_text_colors`
     (content-stream tokenizer). `build_text_layer` refuses (falls back) if any
     text stays light.
   - `convert_raster` (image-only pages, or the hybrid failed): the whole page
     goes through `recolor_raster`.
   - A conversion is kept only if it saves enough ink (`MIN_*_SAVING`).
3. `create_two_up_a4` (hairline border) -> `qpdf_optimize`.

## recolor_raster design (read before changing)

- Decisions are made on a 4x-reduced copy; per-pixel work is done at full
  resolution in strips (`STRIP_ROWS`, `STRIP_PAD`, threads). There is no
  resolution cap: posters are processed at full DPI. Anything upsampled into a
  strip must use `_upsample_rows` so strips join seamlessly.
- "Foreground" = thin features under `FG_WINDOW_PT` (~16 pt, **physical size**,
  not relative to the page; a page-relative window broke A1 posters).
- `resolve_polarity`: text produces thin features of both polarities (letters
  plus the gaps between them). Rule 1: a coloured part that outlines its own
  bounding box (`_outlines_box` on `_largest_region`) is a bar, so the neutral
  part is the text. Rule 2: whichever polarity matches the surroundings is the
  background. Outside a detected bar, decide separately (a heading touching
  the bar).
- `absorb_shape_parts`: thin parts of large shapes (arrow tips, gaps between
  bars) follow the shape. Never use the **dominant** ring colour as a candidate;
  doing so erased whole lines of text in raster mode.
- `page_background_field`: flood fill from the page edges through smooth
  changes only (gradients become white, boxes do not). A plain wide median
  here produced white smudges inside boxes.
- Background -> white or pale tint (`TINT_*`) with hairline outlines;
  foreground -> `map_foreground_lab` (keep hue, darken; light neutrals invert).
  A safety net reverts large light->dark inversions. Output is snapped to pure
  white/black; colourless pages are stored greyscale (`encode_png`).

## Verifying changes

There is no test suite. Verify visually:

- Build synthetic PDFs with PyMuPDF covering: a dark slide with white, grey,
  yellow, cream, cyan and red text, coloured boxes and a thin grey grid; a white
  slide with a dark header band; a gradient background with a photo, a white
  arrow and white-on-red text; a normal light slide (must be KEPT); and an
  **image-only** slide (render a page to a pixmap and re-insert it as an
  image) with a bar full of bold white text and a box filled with large text.
- Run `optimise_pdf(src, dst, allow_vector_text)` with **both** `True` and
  `False`, render pages with `get_pixmap`, and inspect them. Raster mode is
  where text-related regressions show; vector mode hides them because its text
  is not in the image.
- Also test a large page (A1 poster) for scale-dependent bugs, and compare the
  logged ink figures before and after a change.
- Watch for: text that disappears or turns hollow, dark blocks or bands, white
  smudges inside tinted boxes, missing outlines, and marks at box edges next to
  text.
