"""Renderers.

Two targets that run offline, and one that needs credentials.

Markdown is the reference rendering: it is diffable, it is what CI checks, and it carries the
full provenance appendix. PPTX is what actually gets sent to a client. Google Docs and Slides
are a thin adapter over the same :class:`~campaign_copilot.reporting.builder.WeeklyReview`,
imported lazily, because a report generator that cannot be tested without a service account is
a report generator that is never tested.

Every renderer footnotes every number and reproduces the appendix. A deck whose numbers cannot
be traced is exactly the artifact this project exists to argue against, and shipping one as the
project's own output would be funny in the wrong way.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from campaign_copilot.reporting.builder import WeeklyReview

__all__ = ["push_to_google_slides", "render_markdown", "render_pptx"]

# A palette for media performance, not a default. Ink dominates; spend is the warm signal
# colour, revenue the cool one, so the two series read correctly even in greyscale.
INK = "14181F"
PAPER = "FFFFFF"
MUTED = "6B7280"
SPEND = "FF5A36"
REVENUE = "1F8A70"
RULE = "E5E7EB"


def render_markdown(review: WeeklyReview) -> str:
    """The reference rendering. Diffable, and checked in CI."""
    f = review.figures
    lines = [
        f"# {review.title}",
        "",
        review.narrative,
        "",
        "## Headline",
        "",
        "| metric | value | source |",
        "|---|---:|---|",
    ]
    for fid in ("F1", "F2", "F3", "F4"):
        figure = f[fid]
        lines.append(f"| {figure.label} | {figure.render()} | `[{fid}]` |")

    lines += [
        "",
        "## By channel",
        "",
        "| channel | spend | revenue | ROAS |",
        "|---|---:|---:|---:|",
    ]
    for row in review.channels:
        roas = "undefined" if row.roas is None else f"{row.roas:.2f}x"
        lines.append(f"| {row.channel} | ${row.spend:,.2f} | ${row.revenue:,.2f} | {roas} |")

    lines += [
        "",
        "## Provenance",
        "",
        "Every number above resolves to a query or to arithmetic over other figures.",
        "",
        "| id | figure | derivation |",
        "|---|---|---|",
    ]
    for fid, label, provenance in review.figures.appendix():
        lines.append(f"| `{fid}` | {label} | `{provenance}` |")
    lines.append("")
    return "\n".join(lines)


#: Beyond this the table runs off the bottom of a 7.5" slide once SQL wraps.
ROWS_PER_APPENDIX_SLIDE = 5

#: The slide is not the archive. Full SQL lives in the speaker notes and in the markdown.
MAX_PROVENANCE_CHARS = 96


def _paginate(rows: list[tuple[str, str, str]], size: int) -> list[list[tuple[str, str, str]]]:
    """Split the appendix into slide-sized chunks."""
    return [rows[i : i + size] for i in range(0, len(rows), size)]


def _compress(provenance: str) -> str:
    """Shorten SQL for a slide. The schema prefix is noise on every single row."""
    text = " ".join(provenance.split()).replace("main_marts.", "")
    if len(text) <= MAX_PROVENANCE_CHARS:
        return text
    return text[: MAX_PROVENANCE_CHARS - 3] + "..."


def _rgb(hex_color: str) -> Any:
    from pptx.dml.color import RGBColor

    return RGBColor.from_string(hex_color)


def _text(shape: Any, runs: list[tuple[str, int, bool, str]], *, align: str = "left") -> None:
    """Write styled runs. `text_frame.text = ...` collapses formatting, so use runs."""
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Pt

    frame = shape.text_frame
    frame.word_wrap = True
    frame.margin_left = frame.margin_right = 0
    frame.margin_top = frame.margin_bottom = 0
    paragraph = frame.paragraphs[0]
    paragraph.alignment = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER}[align]
    for content, size, bold, colour in runs:
        run = paragraph.add_run()
        run.text = content
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.color.rgb = _rgb(colour)


def render_pptx(review: WeeklyReview, path: Path) -> Path:
    """Render a four-slide client deck. Charts are native PowerPoint charts, not images."""
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
    from pptx.util import Inches, Pt

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    blank = prs.slide_layouts[6]
    f = review.figures

    # ---------------------------------------------------------------- 1. title
    slide = prs.slides.add_slide(blank)
    background = slide.shapes.add_shape(1, 0, 0, prs.slide_width, prs.slide_height)
    background.fill.solid()
    background.fill.fore_color.rgb = _rgb(INK)
    background.line.fill.background()
    background.shadow.inherit = False

    box = slide.shapes.add_textbox(Inches(1.0), Inches(2.4), Inches(11.3), Inches(1.6))
    _text(box, [(review.title, 40, True, PAPER)])
    sub = slide.shapes.add_textbox(Inches(1.0), Inches(4.1), Inches(11.3), Inches(1.4))
    _text(
        sub,
        [
            (
                "Campaign-attributed media only. Unattributed traffic is excluded, and every "
                "figure in this deck resolves to the query that produced it.",
                15,
                False,
                MUTED,
            )
        ],
    )
    slide.notes_slide.notes_text_frame.text = review.narrative

    # ------------------------------------------------------------- 2. headline
    slide = prs.slides.add_slide(blank)
    title = slide.shapes.add_textbox(Inches(0.8), Inches(0.6), Inches(11.7), Inches(0.8))
    _text(title, [("The week in four numbers", 32, True, INK)])

    for i, fid in enumerate(("F1", "F2", "F3", "F4")):
        figure = f[fid]
        left = Inches(0.8 + i * 3.05)
        card = slide.shapes.add_shape(5, left, Inches(1.9), Inches(2.75), Inches(2.3))
        card.fill.solid()
        card.fill.fore_color.rgb = _rgb("F7F7F5")
        card.line.color.rgb = _rgb(RULE)
        card.shadow.inherit = False
        card.adjustments[0] = 0.06

        value = slide.shapes.add_textbox(
            left + Inches(0.25), Inches(2.35), Inches(2.25), Inches(0.9)
        )
        accent = SPEND if fid in {"F1", "F4"} else REVENUE
        _text(value, [(figure.render(), 30, True, accent)])

        label = slide.shapes.add_textbox(
            left + Inches(0.25), Inches(3.25), Inches(2.25), Inches(0.8)
        )
        _text(label, [(figure.label.replace(", this week", ""), 12, False, INK)])

        source = slide.shapes.add_textbox(
            left + Inches(0.25), Inches(3.85), Inches(2.25), Inches(0.3)
        )
        _text(source, [(f"[{fid}]", 9, False, MUTED)])

    prose = slide.shapes.add_textbox(Inches(0.8), Inches(4.7), Inches(11.7), Inches(2.0))
    _text(prose, [(review.narrative, 13, False, INK)])
    slide.notes_slide.notes_text_frame.text = (
        "Every bracketed id resolves on the provenance slide."
    )

    # -------------------------------------------------------------- 3. channels
    slide = prs.slides.add_slide(blank)
    title = slide.shapes.add_textbox(Inches(0.8), Inches(0.6), Inches(11.7), Inches(0.8))
    _text(title, [("Spend against revenue, by channel", 32, True, INK)])

    data = CategoryChartData()
    data.categories = [c.channel.replace("_", " ") for c in review.channels]
    data.add_series("Spend", [c.spend for c in review.channels])
    data.add_series("Revenue", [c.revenue for c in review.channels])
    frame = slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED,
        Inches(0.8),
        Inches(1.8),
        Inches(11.7),
        Inches(4.6),
        data,
    )
    chart = frame.chart
    chart.has_title = False
    chart.has_legend = True
    chart.legend.position = XL_LEGEND_POSITION.TOP
    chart.legend.include_in_layout = False
    chart.legend.font.size = Pt(12)
    chart.plots[0].series[0].format.fill.solid()
    chart.plots[0].series[0].format.fill.fore_color.rgb = _rgb(SPEND)
    chart.plots[0].series[1].format.fill.solid()
    chart.plots[0].series[1].format.fill.fore_color.rgb = _rgb(REVENUE)
    chart.category_axis.tick_labels.font.size = Pt(11)
    chart.value_axis.tick_labels.font.size = Pt(11)
    chart.value_axis.has_major_gridlines = True

    best_roas = f["F9"] if "F9" in f.figures else None
    if best_roas is not None:
        note = slide.shapes.add_textbox(Inches(0.8), Inches(6.5), Inches(11.7), Inches(0.5))
        _text(
            note,
            [
                (
                    f"Highest efficiency: {best_roas.label.replace('ROAS, ', '')} at "
                    f"{best_roas.render()} [{best_roas.id}]. Branded search cannibalises "
                    "organic demand; do not read it as incremental.",
                    11,
                    False,
                    MUTED,
                )
            ],
        )

    # ------------------------------------------------------------ 4. provenance
    # Paginated. A single table of nine SQL statements runs off the bottom of the slide, and
    # a deck whose provenance appendix is cut off is worse than one with no appendix at all.
    rows = review.figures.appendix()
    for page, chunk in enumerate(_paginate(rows, ROWS_PER_APPENDIX_SLIDE)):
        slide = prs.slides.add_slide(blank)
        heading = "Where every number came from"
        if len(rows) > ROWS_PER_APPENDIX_SLIDE:
            heading += f" ({page + 1}/{-(-len(rows) // ROWS_PER_APPENDIX_SLIDE)})"
        title = slide.shapes.add_textbox(Inches(0.8), Inches(0.5), Inches(11.7), Inches(0.7))
        _text(title, [(heading, 30, True, INK)])

        table_shape = slide.shapes.add_table(
            len(chunk) + 1, 3, Inches(0.8), Inches(1.5), Inches(11.7), Inches(0.4)
        )
        table = table_shape.table
        table.columns[0].width = Inches(0.8)
        table.columns[1].width = Inches(3.2)
        table.columns[2].width = Inches(7.7)

        for column, label in enumerate(("id", "figure", "derivation")):
            run = table.cell(0, column).text_frame.paragraphs[0].add_run()
            run.text = label
            run.font.size = Pt(11)
            run.font.bold = True

        for r, (fid, figure_label, provenance) in enumerate(chunk, start=1):
            table.rows[r].height = Inches(0.75)
            for c, content in enumerate((fid, figure_label, _compress(provenance))):
                run = table.cell(r, c).text_frame.paragraphs[0].add_run()
                run.text = content
                run.font.size = Pt(8 if c == 2 else 9)

        slide.notes_slide.notes_text_frame.text = "\n\n".join(
            f"[{fid}] {label}\n{provenance}" for fid, label, provenance in chunk
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(path))
    return path


def push_to_google_slides(review: WeeklyReview, credentials: Any) -> str:
    """Create the same deck in Google Slides. Imported lazily; not exercised by the suite.

    Deliberately thin. The document model is :class:`WeeklyReview`, and the Google adapter is
    a rendering of it, exactly like `render_pptx`. Anything clever belongs in the builder,
    where it can be tested without a service account.
    """
    from googleapiclient.discovery import build

    service = build("slides", "v1", credentials=credentials)
    deck = service.presentations().create(body={"title": review.title}).execute()
    presentation_id: str = deck["presentationId"]

    requests: list[dict[str, Any]] = [
        {
            "createSlide": {
                "objectId": f"slide_{i}",
                "slideLayoutReference": {"predefinedLayout": "TITLE_AND_BODY"},
            }
        }
        for i in range(2)
    ]
    service.presentations().batchUpdate(
        presentationId=presentation_id, body={"requests": requests}
    ).execute()
    return presentation_id
