"""Someone's job applications as an Excel sheet, a Word document or a PDF.

Files are made on this PC with no account and no upload, and hold only that person's own applications.
Job titles and company names come from the web, so they're always written as plain text: a title
can't become an Excel formula, or make a PDF fetch something from the internet.
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape, quoteattr

from vera.agents import rounds

FORMATS = ("xlsx", "docx", "pdf")
SENT_STATUSES = {"submitted": "Sent", "applied": "Sent", "interview": "Interview", "offer": "Offer", "rejected": "Rejected", "withdrawn": "Withdrawn"}
UNFINISHED = {"needs_answer": "Needs your answer", "failed": "Failed", "blocked": "Bot check", "needs_login": "Needs a login"}
BOARDS = {"remote_board": "Remote board", "ats": "Company form", "company_form": "Company form"}
SENT_COLUMNS = (("date", "Date", 12), ("title", "Job", 38), ("company", "Company", 26), ("setup", "Setup", 10), ("location", "Location", 26), ("board", "Board", 14), ("status", "Status", 11), ("link", "Link", 32))
UNFINISHED_COLUMNS = (("date", "Date", 12), ("title", "Job", 42), ("company", "Company", 28), ("board", "Board", 15), ("status", "Status", 18), ("note", "Why", 50))
Row = Dict[str, str]


def clean(text, limit: int = 300) -> str:
    """Plain text on one line. Files can't hold control characters, and web pages sometimes contain them."""
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(text or ""))).strip()[:limit]


def web_link(url) -> str:
    url = clean(url, 800)
    return url if re.match(r"https?://", url, re.I) else ""


def _board(name) -> str:
    name = clean(name, 60)
    return BOARDS.get(name, name.replace("_", " ").title())


def collect(session) -> Dict[str, List[Row]]:
    """This person's sent applications, with the latest status from their history, and the ones that couldn't be finished."""
    tracked = session.db.applications(session.uid, limit=100000)
    by_url = {a.get("url"): a for a in tracked if a.get("url")}
    tried = rounds.read_results(session.uid, "")
    sent: Dict[str, Row] = {}
    for r in tried:                  # oldest first, so a later attempt replaces an earlier one
        if r.get("status") == "submitted":
            status = (by_url.get(r["url"]) or {}).get("status")
            sent[r["url"]] = {"date": clean(r.get("time"))[:10], "title": clean(r.get("title")), "company": clean(r.get("company")), "board": _board(r.get("site")),
                              "status": SENT_STATUSES.get(status, "Sent"), "link": web_link(r["url"]), "note": "", "setup": clean(r.get("setup")), "location": clean(r.get("location"))}
    for a in tracked:                # applied by hand, or marked by the person
        url = a.get("url") or f"app-{a['id']}"
        if a.get("status") in SENT_STATUSES and url not in sent:
            sent[url] = {"date": clean(a.get("applied_at") or a.get("created_at"))[:10], "title": clean(a.get("position")), "company": clean(a.get("company")),
                         "board": _board(a.get("source")), "status": SENT_STATUSES[a["status"]], "link": web_link(a.get("url")), "note": "", "setup": "", "location": ""}
    unfinished: Dict[str, Row] = {}
    for r in tried:
        if r.get("status") in UNFINISHED and r.get("url") not in sent:
            why = re.sub(r"\s*\(screenshot:[^)]*\)", "", clean(r.get("unanswered") or r.get("reason"), 5000))[:300]
            unfinished[r["url"]] = {"date": clean(r.get("time"))[:10], "title": clean(r.get("title")) or web_link(r["url"]), "company": clean(r.get("company")),
                                    "board": _board(r.get("site")), "status": UNFINISHED[r["status"]], "link": web_link(r["url"]), "note": why}
    newest_first = lambda rows: sorted(rows.values(), key=lambda x: x["date"], reverse=True)
    return {"sent": newest_first(sent), "unfinished": newest_first(unfinished)}


def summary(rows: Dict[str, List[Row]]) -> str:
    counts: Dict[str, int] = {}
    for r in rows["sent"]:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    detail = ", ".join(f"{n} {s.lower()}" for s, n in counts.items() if s != "Sent")
    text = f"**{len(rows['sent'])}** application{'' if len(rows['sent']) == 1 else 's'} sent" + (f" ({detail})" if detail else "")
    if rows["unfinished"]:
        text += f", and **{len(rows['unfinished'])}** that couldn't be finished"
    return text + "."


def narrow(rows: Dict[str, List[Row]], options: Optional[Dict[str, Any]]) -> Dict[str, List[Row]]:
    """Only what was asked for: these dates, these boards, these statuses."""
    o = options or {}
    since, until, boards, statuses, section, setups = o.get("since"), o.get("until"), o.get("boards"), o.get("statuses"), o.get("section"), o.get("setups")
    # Asked for as "interview" or "company_form" (the tool's own names) or as shown ("Interview", "Company form"): both work.
    # 9 Nov 2026: "interview" never matched the label "Interview", so she told him he had no interviews.
    norm = lambda x: re.sub(r"[^a-z]", "", str(x or "").lower()).rstrip("s")      # "remote_boards" is "Remote board"
    boards = {norm(BOARDS.get(b, b)) for b in boards} | {norm(b) for b in boards} if boards else None
    statuses = {norm(SENT_STATUSES.get(str(s).lower(), s)) for s in statuses} | {norm(s) for s in statuses} if statuses else None
    setups = {norm(s) for s in setups} if setups else None
    fits = lambda r: ((not since or r["date"] >= since) and (not until or r["date"] <= until) and (not boards or norm(r["board"]) in boards)
                      and (not setups or norm(r.get("setup")) in setups))
    sent = [r for r in rows["sent"] if fits(r) and (not statuses or norm(r["status"]) in statuses)]
    unfinished = [r for r in rows["unfinished"] if fits(r)]
    return {"sent": [] if section == "unfinished" else sent, "unfinished": [] if section == "sent" or statuses else unfinished}


def _chosen(columns, keep: Optional[List[str]]):
    """The columns asked for. The reason a job wasn't finished always stays with it."""
    return columns if not keep else tuple(c for c in columns if c[0] in keep or c[0] == "note")


def _plain(text: str) -> str:
    """Web text in a Discord message: no pings, and no pretending to be formatting."""
    return re.sub(r"[`*_~|>@\\]", "", text or "").strip()


def _split_lines(lines: List[str], limit: int = 1900) -> List[str]:
    parts, current = [], ""
    for line in lines:
        if current and len(current) + len(line) + 1 > limit:
            parts.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    return parts + [current] if current else parts


def _counts(rows: List[Row], key: str) -> str:
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r[key]] = counts.get(r[key], 0) + 1
    return ", ".join(f"{name}: **{n}**" for name, n in counts.items())


def render_chat(rows: Dict[str, List[Row]], options: Dict[str, Any]) -> List[str]:
    """A table or summary to read in Discord. Discord has no tables, so links get a line per job, and everything else gets an aligned block."""
    if options.get("count"):
        total = len(rows["sent"])
        return [f"**{total}** application{'' if total == 1 else 's'} sent{' for ' + options['label'] if options.get('label') else ' so far'}."]
    head = [f"📋 **Your applications{' · ' + options['label'] if options.get('label') else ''}**", summary(rows)]
    keep = [c for c in (options.get("columns") or ["date", "title", "company", "setup", "board", "status", "link"])]
    if options.get("chat") == "summary":
        lines = list(head)
        if rows["sent"]:
            lines += [f"**By status:** {_counts(rows['sent'], 'status')}", f"**By board:** {_counts(rows['sent'], 'board')}"]
            with_setup = [r for r in rows["sent"] if r.get("setup")]
            if with_setup:
                lines.append(f"**By setup:** {_counts(with_setup, 'setup')}")
            lines.append(f"**By day:** {_counts(rows['sent'], 'date')}")
        return _split_lines(lines)
    messages = []
    if "link" in keep:
        fields = [c for c in keep if c != "link"]
        job_line = lambda r: " · ".join(_plain(r.get(c, ""))[:70] or "?" for c in fields) + (f" · <{r['link']}>" if r["link"] else "")
        if options.get("group"):
            lines = list(head)
            for setup, title in (("Remote", "🏠 Remote"), ("Hybrid", "🔀 Hybrid"), ("On-site", "🏢 On-site"), ("", "❔ Setup not recorded")):
                group = [r for r in rows["sent"] if (r.get("setup") or "") == setup]
                if group:
                    lines += [f"**{title} ({len(group)})**"] + [job_line(r) for r in group]
        else:
            lines = head + [job_line(r) for r in rows["sent"]]
        messages = _split_lines(lines)
    else:
        sizes = {"date": 10, "title": 32, "company": 20, "setup": 7, "location": 22, "board": 13, "status": 9}
        labels = {"date": "Date", "title": "Job", "company": "Company", "setup": "Setup", "location": "Location", "board": "Board", "status": "Status"}
        cell = lambda key, text: text.replace("`", "")[:sizes[key]].ljust(sizes[key])
        top = "  ".join(cell(c, labels[c]) for c in keep)
        body = _split_lines(["  ".join(cell(c, r.get(c, "")) for c in keep) for r in rows["sent"]], 1750)
        messages = _split_lines(head) + [f"```\n{top}\n{part}\n```" for part in body]
    if rows["unfinished"]:
        messages += _split_lines(["**Not finished**"] + [f"{r['date']} · {_plain(r['title'])[:70]} · {r['status']} · {_plain(r['note'])[:120]}" for r in rows["unfinished"]])
    return messages


def _write_xlsx(rows: Dict[str, List[Row]], path: Path, keep: Optional[List[str]] = None, label: str = "") -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    def fill(sheet, columns, data):
        sheet.append([label for _key, label, _w in columns])
        for i, (_key, _label, width) in enumerate(columns, 1):
            head = sheet.cell(1, i)
            head.font, head.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F3864")
            sheet.column_dimensions[head.column_letter].width = width
        for r, item in enumerate(data, 2):
            for c, (key, _label, _w) in enumerate(columns, 1):
                cell = sheet.cell(r, c)
                cell.value = item.get(key, "")
                cell.data_type = "s"       # text, always: a job title starting with "=" must not run as a formula
                cell.alignment = Alignment(vertical="top", wrap_text=key != "link")
                if key == "link" and item.get(key):
                    cell.hyperlink = item[key]
                    cell.data_type = "s"
                    cell.font = Font(color="0563C1", underline="single")
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions

    book = Workbook()
    book.active.title = "Applications"
    fill(book.active, _chosen(SENT_COLUMNS, keep), rows["sent"])
    if rows["unfinished"]:
        fill(book.create_sheet("Not finished"), _chosen(UNFINISHED_COLUMNS, keep), rows["unfinished"])
    book.save(path)


def _heading_note(rows: Dict[str, List[Row]], label: str) -> str:
    return (f"{label}. " if label else "") + f"Made on {datetime.now():%B %d, %Y}. " + summary(rows).replace("**", "")


def _write_docx(rows: Dict[str, List[Row]], path: Path, keep: Optional[List[str]] = None, label: str = "") -> None:
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.shared import Inches, Pt

    doc = Document()
    page = doc.sections[0]
    page.orientation, page.page_width, page.page_height = WD_ORIENT.LANDSCAPE, page.page_height, page.page_width
    for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(page, side, Inches(0.6))
    doc.styles["Normal"].font.size = Pt(9)

    def table(columns, data):
        grid = doc.add_table(rows=1, cols=len(columns))
        grid.style = "Table Grid"
        for cell, (_key, label, _w) in zip(grid.rows[0].cells, columns):
            cell.text = ""
            cell.paragraphs[0].add_run(label).bold = True
        for item in data:
            for cell, (key, _label, _w) in zip(grid.add_row().cells, columns):
                cell.text = item.get(key, "")

    doc.add_heading("Job applications", level=1)
    doc.add_paragraph(_heading_note(rows, label))
    if rows["sent"]:
        doc.add_heading("Sent", level=2)
        table(_chosen(SENT_COLUMNS, keep), rows["sent"])
    if rows["unfinished"]:
        doc.add_heading("Not finished", level=2)
        table(_chosen(UNFINISHED_COLUMNS, keep), rows["unfinished"])
    doc.save(str(path))


def _pdf_font() -> tuple:
    """Arial covers more letters. Helvetica ships with every PDF reader, so it's the fallback."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    regular, bold = Path(r"C:\Windows\Fonts\arial.ttf"), Path(r"C:\Windows\Fonts\arialbd.ttf")
    if regular.exists() and bold.exists():
        pdfmetrics.registerFont(TTFont("Vera", str(regular)))
        pdfmetrics.registerFont(TTFont("Vera-Bold", str(bold)))
        return "Vera", "Vera-Bold"
    return "Helvetica", "Helvetica-Bold"


def _write_pdf(rows: Dict[str, List[Row]], path: Path, keep: Optional[List[str]] = None, label: str = "") -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font, bold = _pdf_font()
    body = ParagraphStyle("body", fontName=font, fontSize=8, leading=10)
    head = ParagraphStyle("head", fontName=bold, fontSize=8, leading=10, textColor=colors.white)
    title = ParagraphStyle("title", fontName=bold, fontSize=16, leading=20, spaceAfter=4)
    section = ParagraphStyle("section", fontName=bold, fontSize=11, leading=14, spaceBefore=10, spaceAfter=4)

    def words(text: str) -> Paragraph:
        # Escaping keeps web text as plain words. Reportlab would otherwise treat <img src=...> as an instruction.
        return Paragraph(escape(re.sub(r"[\U00010000-\U0010ffff]", "", text)), body)

    def cell(key: str, item: Row):
        if key == "link" and item.get(key):
            return Paragraph(f'<link href={quoteattr(item[key])} color="#0563C1"><u>Open job</u></link>', body)
        return words(item.get(key, ""))

    def table(columns, data):
        document_width = landscape(A4)[0] - 24 * mm
        scale = document_width / sum(w for _k, _l, w in columns)
        widths = [w * scale for _k, _l, w in columns]
        grid = Table([[Paragraph(label, head) for _k, label, _w in columns]] + [[cell(key, item) for key, _l, _w in columns] for item in data],
                     colWidths=widths, repeatRows=1)
        grid.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3864")), ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#B7B7B7")),
                                  ("VALIGN", (0, 0), (-1, -1), "TOP"), ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F2F2")])]))
        return grid

    story = [Paragraph("Job applications", title), Paragraph(escape(_heading_note(rows, label)), body), Spacer(1, 6)]
    if rows["sent"]:
        story += [Paragraph("Sent", section), table(_chosen(SENT_COLUMNS, keep), rows["sent"])]
    if rows["unfinished"]:
        story += [Paragraph("Not finished", section), table(_chosen(UNFINISHED_COLUMNS, keep), rows["unfinished"])]
    SimpleDocTemplate(str(path), pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
                      title="Job applications").build(story)


WRITERS = {"xlsx": _write_xlsx, "docx": _write_docx, "pdf": _write_pdf}
DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")


def write_all(rows: Dict[str, List[Row]], formats: List[str], folder: Path, options: Optional[Dict[str, Any]] = None) -> List[Path]:
    o = options or {}
    folder.mkdir(parents=True, exist_ok=True)
    dates = [str(o.get(k) or "") for k in ("since", "until")]
    # The file name only ever holds real dates: anything else ("..\..\Desktop\x") would be a way to write elsewhere.
    span = "_to_".join(dates) if all(DATE_ONLY.fullmatch(d) for d in dates) else f"{datetime.now():%Y-%m-%d}"
    paths = []
    for kind in formats:
        if kind not in WRITERS:
            continue
        path = folder / f"Job_Applications_{span}.{kind}"
        if folder.resolve() not in path.resolve().parents:
            raise ValueError("refusing to write a file outside the person's folder")
        WRITERS[kind](rows, path, o.get("columns"), o.get("label") or "")
        paths.append(path)
    return paths
