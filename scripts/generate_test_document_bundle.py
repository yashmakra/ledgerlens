"""Generate a coherent four-PDF reconciliation test bundle."""

from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, Table, TableStyle

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "output" / "pdf" / "reconciliation-test-bundle"

PAGE_W, PAGE_H = A4
NAVY = colors.HexColor("#172033")
INDIGO = colors.HexColor("#4F46E5")
MUTED = colors.HexColor("#667085")
LINE = colors.HexColor("#E4E7EC")
SOFT = colors.HexColor("#F7F8FA")
GREEN = colors.HexColor("#287A52")
AMBER = colors.HexColor("#B66B1F")
WHITE = colors.white


def register_fonts() -> tuple[str, str]:
    regular_candidates = [
        Path(r"C:\Windows\Fonts\arial.ttf"),
        Path(r"C:\Windows\Fonts\segoeui.ttf"),
    ]
    bold_candidates = [
        Path(r"C:\Windows\Fonts\arialbd.ttf"),
        Path(r"C:\Windows\Fonts\segoeuib.ttf"),
    ]
    regular = next((path for path in regular_candidates if path.exists()), None)
    bold = next((path for path in bold_candidates if path.exists()), None)
    if regular and bold:
        pdfmetrics.registerFont(TTFont("BundleRegular", regular))
        pdfmetrics.registerFont(TTFont("BundleBold", bold))
        return "BundleRegular", "BundleBold"
    return "Helvetica", "Helvetica-Bold"


REGULAR, BOLD = register_fonts()


def base_canvas(path: Path, document_type: str, document_number: str) -> canvas.Canvas:
    pdf = canvas.Canvas(str(path), pagesize=A4, invariant=1)
    pdf.setTitle(f"Synthetic {document_type} {document_number}")
    pdf.setAuthor("LedgerLens Test Data")
    pdf.setSubject("Synthetic reconciliation test bundle")
    pdf.setFillColor(NAVY)
    pdf.setFont(BOLD, 13)
    pdf.drawString(20 * mm, PAGE_H - 21 * mm, "NORTHWIND COMPONENTS")
    pdf.setFillColor(MUTED)
    pdf.setFont(REGULAR, 8)
    pdf.drawString(20 * mm, PAGE_H - 27 * mm, "88 Harbor Avenue, Seattle, WA 98101")
    pdf.setFillColor(INDIGO)
    pdf.setFont(BOLD, 20)
    pdf.drawRightString(PAGE_W - 20 * mm, PAGE_H - 22 * mm, document_type.upper())
    pdf.setFillColor(MUTED)
    pdf.setFont(REGULAR, 8)
    pdf.drawRightString(PAGE_W - 20 * mm, PAGE_H - 28 * mm, document_number)
    pdf.setStrokeColor(LINE)
    pdf.line(20 * mm, PAGE_H - 34 * mm, PAGE_W - 20 * mm, PAGE_H - 34 * mm)
    return pdf


def info_grid(pdf: canvas.Canvas, rows: list[tuple[str, str]], top_y: float) -> float:
    left = 20 * mm
    width = PAGE_W - 40 * mm
    columns = 2
    cell_w = width / columns
    cell_h = 18 * mm
    for index, (label, value) in enumerate(rows):
        row = index // columns
        col = index % columns
        x = left + col * cell_w
        y = top_y - row * cell_h
        pdf.setFillColor(SOFT)
        pdf.roundRect(x, y - 14 * mm, cell_w - 3 * mm, 14 * mm, 2 * mm, fill=1, stroke=0)
        pdf.setFillColor(MUTED)
        pdf.setFont(BOLD, 7)
        pdf.drawString(x + 4 * mm, y - 5 * mm, label.upper())
        pdf.setFillColor(NAVY)
        pdf.setFont(BOLD, 10)
        pdf.drawString(x + 4 * mm, y - 10.5 * mm, value)
    return top_y - ((len(rows) + columns - 1) // columns) * cell_h - 2 * mm


def draw_table(
    pdf: canvas.Canvas,
    headers: list[str],
    rows: list[list[str]],
    widths: list[float],
    top_y: float,
) -> float:
    paragraph_style = ParagraphStyle(
        "cell",
        fontName=REGULAR,
        fontSize=8,
        leading=10,
        textColor=NAVY,
        alignment=TA_LEFT,
    )
    data = [headers] + [
        [Paragraph(str(value), paragraph_style) for value in row]
        for row in rows
    ]
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("FONTNAME", (0, 0), (-1, 0), BOLD),
        ("FONTSIZE", (0, 0), (-1, 0), 7),
        ("FONTNAME", (0, 1), (-1, -1), REGULAR),
        ("FONTSIZE", (0, 1), (-1, -1), 8),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, SOFT]),
        ("GRID", (0, 0), (-1, -1), 0.5, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
    ]))
    _, table_height = table.wrap(sum(widths), PAGE_H)
    table.drawOn(pdf, 20 * mm, top_y - table_height)
    return top_y - table_height


def section_label(pdf: canvas.Canvas, text: str, y: float) -> None:
    pdf.setFillColor(MUTED)
    pdf.setFont(BOLD, 8)
    pdf.drawString(20 * mm, y, text.upper())


def footer(pdf: canvas.Canvas, file_label: str) -> None:
    pdf.setStrokeColor(LINE)
    pdf.line(20 * mm, 21 * mm, PAGE_W - 20 * mm, 21 * mm)
    pdf.setFillColor(AMBER)
    pdf.setFont(BOLD, 7.5)
    pdf.drawString(20 * mm, 14 * mm, "SYNTHETIC TEST DATA - NOT FOR PAYMENT")
    pdf.setFillColor(MUTED)
    pdf.setFont(REGULAR, 7.5)
    pdf.drawRightString(PAGE_W - 20 * mm, 14 * mm, file_label)
    pdf.showPage()
    pdf.save()


def create_invoice(path: Path) -> None:
    pdf = base_canvas(path, "Invoice", "INV-2060")
    y = info_grid(pdf, [
        ("Invoice Number", "INV-2060"),
        ("Invoice Date", "2026-05-12"),
        ("Purchase Order Number", "PO-7781"),
        ("Currency", "USD"),
        ("Bill To", "Contoso Manufacturing"),
        ("Payment Terms", "Net 30"),
    ], PAGE_H - 43 * mm)
    section_label(pdf, "Invoice line items", y)
    y = draw_table(
        pdf,
        ["Item code", "Description", "Quantity", "Unit price", "Line amount"],
        [["WIDGET-1", "Industrial control widget", "100", "106.00", "10,600.00"]],
        [27 * mm, 65 * mm, 21 * mm, 26 * mm, 31 * mm],
        y - 4 * mm,
    )
    pdf.setFillColor(SOFT)
    pdf.roundRect(PAGE_W - 82 * mm, y - 40 * mm, 62 * mm, 32 * mm, 3 * mm, fill=1, stroke=0)
    totals = [("Subtotal", "10,600.00"), ("Tax", "0.00"), ("Total Due", "USD 10,600.00")]
    for index, (label, value) in enumerate(totals):
        row_y = y - 16 * mm - index * 8 * mm
        pdf.setFillColor(MUTED if index < 2 else NAVY)
        pdf.setFont(BOLD if index == 2 else REGULAR, 9 if index == 2 else 8)
        pdf.drawString(PAGE_W - 77 * mm, row_y, label)
        pdf.drawRightString(PAGE_W - 25 * mm, row_y, value)
    pdf.setFillColor(AMBER)
    pdf.setFont(BOLD, 8)
    pdf.drawString(20 * mm, y - 18 * mm, "TEST EXCEPTION: unit price is 6% above the contracted price.")
    footer(pdf, "invoice_INV-2060.pdf")


def create_purchase_order(path: Path) -> None:
    pdf = base_canvas(path, "Purchase Order", "PO-7781")
    y = info_grid(pdf, [
        ("Purchase Order Number", "PO-7781"),
        ("Order Date", "2026-05-01"),
        ("Contract Number", "CTR-2026-014"),
        ("Currency", "USD"),
        ("Buyer", "Contoso Manufacturing"),
        ("Supplier", "Northwind Components"),
    ], PAGE_H - 43 * mm)
    section_label(pdf, "Ordered items", y)
    y = draw_table(
        pdf,
        ["Item code", "Description", "Ordered qty", "Unit price", "Line total"],
        [["WIDGET-1", "Industrial control widget", "100", "100.00", "10,000.00"]],
        [27 * mm, 65 * mm, 21 * mm, 26 * mm, 31 * mm],
        y - 4 * mm,
    )
    pdf.setFillColor(SOFT)
    pdf.roundRect(PAGE_W - 82 * mm, y - 25 * mm, 62 * mm, 17 * mm, 3 * mm, fill=1, stroke=0)
    pdf.setFillColor(NAVY)
    pdf.setFont(BOLD, 10)
    pdf.drawString(PAGE_W - 77 * mm, y - 18 * mm, "Grand Total")
    pdf.drawRightString(PAGE_W - 25 * mm, y - 18 * mm, "USD 10,000.00")
    pdf.setFillColor(MUTED)
    pdf.setFont(REGULAR, 8)
    pdf.drawString(20 * mm, y - 18 * mm, "Delivery location: Contoso Plant 4, Receiving Dock B")
    footer(pdf, "purchase_order_PO-7781.pdf")


def create_receipt(path: Path) -> None:
    pdf = base_canvas(path, "Delivery Receipt", "GR-8801")
    y = info_grid(pdf, [
        ("Delivery Receipt Number", "GR-8801"),
        ("Receipt Date", "2026-05-10"),
        ("Purchase Order Number", "PO-7781"),
        ("Currency", "USD"),
        ("Receiving Location", "Contoso Plant 4"),
        ("Received By", "Jordan Lee"),
    ], PAGE_H - 43 * mm)
    section_label(pdf, "Receipt inspection", y)
    y = draw_table(
        pdf,
        ["Item code", "Description", "Received", "Accepted", "Rejected"],
        [["WIDGET-1", "Industrial control widget", "100", "80", "20"]],
        [27 * mm, 65 * mm, 24 * mm, 24 * mm, 30 * mm],
        y - 4 * mm,
    )
    pdf.setFillColor(colors.HexColor("#FFF7ED"))
    pdf.roundRect(20 * mm, y - 29 * mm, PAGE_W - 40 * mm, 20 * mm, 3 * mm, fill=1, stroke=0)
    pdf.setFillColor(AMBER)
    pdf.setFont(BOLD, 8)
    pdf.drawString(25 * mm, y - 17 * mm, "QUALITY HOLD")
    pdf.setFillColor(NAVY)
    pdf.setFont(REGULAR, 8)
    pdf.drawString(25 * mm, y - 23 * mm, "20 units were rejected during inspection. Accepted quantity available for invoicing: 80.")
    footer(pdf, "delivery_receipt_GR-8801.pdf")


def create_contract(path: Path) -> None:
    pdf = base_canvas(path, "Supplier Contract", "CTR-2026-014")
    y = info_grid(pdf, [
        ("Contract Number", "CTR-2026-014"),
        ("Effective Date", "2026-01-01"),
        ("Valid To", "2026-12-31"),
        ("Currency", "USD"),
        ("Price Tolerance", "5%"),
        ("Payment Terms", "Net 30"),
    ], PAGE_H - 43 * mm)
    section_label(pdf, "Contracted pricing", y)
    y = draw_table(
        pdf,
        ["Item code", "Description", "Contract price", "Tolerance", "Maximum allowed"],
        [["WIDGET-1", "Industrial control widget", "100.00", "5%", "105.00"]],
        [27 * mm, 65 * mm, 27 * mm, 22 * mm, 30 * mm],
        y - 4 * mm,
    )
    section_label(pdf, "Key commercial terms", y - 16 * mm)
    terms = [
        "Invoices must reference a valid purchase order number.",
        "Payment is limited to quantities accepted on delivery receipts.",
        "Prices above the 5% tolerance require written buyer approval.",
        "Payment terms are 30 calendar days after acceptance of a valid invoice.",
    ]
    for index, term in enumerate(terms, start=1):
        row_y = y - 24 * mm - (index - 1) * 9 * mm
        pdf.setFillColor(INDIGO)
        pdf.setFont(BOLD, 8)
        pdf.drawString(22 * mm, row_y, f"{index:02d}")
        pdf.setFillColor(NAVY)
        pdf.setFont(REGULAR, 8)
        pdf.drawString(31 * mm, row_y, term)
    pdf.setFillColor(GREEN)
    pdf.setFont(BOLD, 8)
    pdf.drawString(20 * mm, y - 65 * mm, "Approved synthetic agreement for LedgerLens testing.")
    footer(pdf, "supplier_contract_CTR-2026-014.pdf")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    files = {
        "invoice_INV-2060.pdf": create_invoice,
        "purchase_order_PO-7781.pdf": create_purchase_order,
        "delivery_receipt_GR-8801.pdf": create_receipt,
        "supplier_contract_CTR-2026-014.pdf": create_contract,
    }
    for filename, builder in files.items():
        builder(OUTPUT_DIR / filename)
    zip_path = OUTPUT_DIR / "ledgerlens_test_bundle.zip"
    with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as archive:
        for filename in files:
            archive.write(OUTPUT_DIR / filename, arcname=filename)
    print(f"Created {len(files)} PDFs and {zip_path.name} in {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
