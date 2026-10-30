"""Regenerate static/felix-atandi-cv.pdf. Fill in EMAIL/PHONE, then run: python make_cv.py"""
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable

EMAIL = ""   # e.g. "you@example.com"
PHONE = ""   # e.g. "+254 7XX XXX XXX"
WEBSITE = "felix-ke-ideas.onrender.com"

NAVY, TEAL, GREY = colors.HexColor("#0a2742"), colors.HexColor("#16a085"), colors.HexColor("#475569")
name = ParagraphStyle("n", fontName="Helvetica-Bold", fontSize=24, textColor=NAVY, leading=28)
role = ParagraphStyle("r", fontName="Helvetica", fontSize=11.5, textColor=TEAL, leading=15)
meta = ParagraphStyle("m", fontName="Helvetica", fontSize=9.5, textColor=GREY, leading=13)
h = ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=12, textColor=NAVY, spaceBefore=14, spaceAfter=3)
body = ParagraphStyle("b", fontName="Helvetica", fontSize=10, textColor=colors.HexColor("#1e293b"), leading=14.5)
bul = ParagraphStyle("bl", parent=body, leftIndent=12, bulletIndent=2, spaceAfter=2)
bold = ParagraphStyle("bd", parent=body, fontName="Helvetica-Bold")

contact = [x for x in [EMAIL, PHONE, "Maseno, Kenya", WEBSITE] if x]
S = []
def section(t):
    S.extend([Paragraph(t, h), HRFlowable(width="100%", thickness=0.8, color=TEAL, spaceAfter=5)])
def bullets(items):
    S.extend(Paragraph(i, bul, bulletText="\u2022") for i in items)

S += [Paragraph("Felix Atandi", name),
      Paragraph("Mathematical Science with IT student", role), Spacer(1, 3),
      Paragraph("  |  ".join(contact), meta)]

section("Profile")
S.append(Paragraph(
    "Third-year Mathematical Science with IT student at Maseno University with an interest in "
    "programming, statistics and data analysis. I have designed, built and deployed a database-backed "
    "web application with Python and Flask, and I am looking for an attachment or internship where I "
    "can apply my mathematical and technical skills to practical problems.", body))

section("Education")
S.append(Paragraph("Maseno University", bold))
S.append(Paragraph("Degree programme: Mathematical Science with IT (third year)", body))
S.append(Paragraph("Areas of study: mathematical reasoning, statistics, probability, computational mathematics.", body))

section("Technical skills")
bullets(["<b>Programming and web:</b> Python, Flask, HTML, CSS, PostgreSQL",
         "<b>Data and statistics:</b> Microsoft Excel, R, statistical analysis, data organisation and visualisation"])

section("Project")
S.append(Paragraph("Felix KE Ideas: personal portfolio and web application (felix-ke-ideas.onrender.com)", bold))
bullets(["Built and deployed a multi-page website with Python, Flask, HTML, CSS and a PostgreSQL database.",
         "Implemented a contact form that stores messages in the database and a password-protected administrator dashboard with search and delete.",
         "Applied security practices: hashed passwords, CSRF protection, parameterised SQL queries, input validation and rate limiting."])

section("Career goals")
S.append(Paragraph(
    "To combine mathematical knowledge and information technology to solve practical problems, "
    "gain industry experience through university attachment, and grow towards a career in programming, "
    "statistics or data analysis.", body))

section("Referees")
S.append(Paragraph("Available on request.", body))

SimpleDocTemplate("static/felix-atandi-cv.pdf", pagesize=A4, leftMargin=20*mm, rightMargin=20*mm,
                  topMargin=18*mm, bottomMargin=16*mm, title="Felix Atandi - CV", author="Felix Atandi").build(S)
print("CV written")
