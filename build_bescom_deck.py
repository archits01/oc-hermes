from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.dml import MSO_LINE_DASH_STYLE
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.enum.text import MSO_AUTO_SIZE
from pptx.oxml import parse_xml
from pptx.oxml.ns import nsdecls
from pathlib import Path

OUT = Path('/opt/opencomputer-v2/247AutogenV_BESCOM_Executive_Briefing.pptx')
prs = Presentation()
prs.slide_width = Inches(13.333333)
prs.slide_height = Inches(7.5)
blank = prs.slide_layouts[6]

NAVY = '0B0F19'; SLATE = '162447'; CYAN = '00D2FF'; GREEN = '38EF7D'; WHITE = 'FFFFFF'; SILVER = 'CBD5E1'; MUTED = '64748B'; RED = 'FF6B6B'; AMBER = 'F6C453'; CARD = '121B2D'; GRID = '26324A'
FONT = 'Aptos'

def rgb(h): return RGBColor.from_string(h)
def set_bg(slide, color=NAVY):
    fill = slide.background.fill; fill.solid(); fill.fore_color.rgb = rgb(color)
def shape(slide, typ, x,y,w,h, fill=CARD, line=None, radius=True):
    shp = slide.shapes.add_shape(typ, Inches(x), Inches(y), Inches(w), Inches(h))
    shp.fill.solid(); shp.fill.fore_color.rgb = rgb(fill)
    shp.line.color.rgb = rgb(line or fill)
    if line:
        shp.line.width = Pt(0.8)
    return shp

def textbox(slide, text, x,y,w,h, size=16, color=WHITE, bold=False, font=FONT, align=PP_ALIGN.LEFT, valign=MSO_ANCHOR.TOP, margin=0.06, italic=False):
    tb=slide.shapes.add_textbox(Inches(x),Inches(y),Inches(w),Inches(h)); tf=tb.text_frame
    tf.clear(); tf.word_wrap=True; tf.margin_left=Inches(margin); tf.margin_right=Inches(margin); tf.margin_top=Inches(margin); tf.margin_bottom=Inches(margin); tf.vertical_anchor=valign
    p=tf.paragraphs[0]; p.alignment=align
    r=p.add_run(); r.text=text; r.font.name=font; r.font.size=Pt(size); r.font.bold=bold; r.font.italic=italic; r.font.color.rgb=rgb(color)
    return tb

def richbox(slide, runs, x,y,w,h, size=16, color=WHITE, align=PP_ALIGN.LEFT, margin=0.06):
    tb=slide.shapes.add_textbox(Inches(x),Inches(y),Inches(w),Inches(h)); tf=tb.text_frame; tf.clear(); tf.word_wrap=True
    tf.margin_left=tf.margin_right=tf.margin_top=tf.margin_bottom=Inches(margin)
    p=tf.paragraphs[0]; p.alignment=align
    for txt, col, bold in runs:
        r=p.add_run(); r.text=txt; r.font.name=FONT; r.font.size=Pt(size); r.font.bold=bold; r.font.color.rgb=rgb(col)
    return tb

def line(slide,x1,y1,x2,y2,color=GRID,width=1.0,dash=None):
    l=slide.shapes.add_connector(1,Inches(x1),Inches(y1),Inches(x2),Inches(y2)); l.line.color.rgb=rgb(color); l.line.width=Pt(width)
    if dash: l.line.dash_style = dash
    return l

def pill(slide, text, x,y,w, color=CYAN, fill='102B3A'):
    s=shape(slide,MSO_SHAPE.ROUNDED_RECTANGLE,x,y,w,0.28,fill,color); textbox(slide,text,x,y+0.01,w,0.22,8,color,True,align=PP_ALIGN.CENTER,margin=0.01)

def title(slide, kicker, head, sub=None, n=None):
    textbox(slide,kicker.upper(),0.62,0.34,7.5,0.25,9,CYAN,True)
    textbox(slide,head,0.62,0.66,11.6,0.56,25,WHITE,True)
    if sub: textbox(slide,sub,0.64,1.28,11.8,0.35,10,SILVER)
    if n is not None: textbox(slide,f'{n:02d}',12.15,0.38,0.5,0.25,10,MUTED,True,align=PP_ALIGN.RIGHT)

def footer(slide, txt='247AutogenV  |  Confidential executive briefing'):
    line(slide,0.62,7.12,12.72,7.12,GRID,0.8); textbox(slide,txt,0.62,7.18,8,0.16,7,MUTED)

def notes(slide, text):
    ns=slide.notes_slide.notes_text_frame
    ns.text=text

def bullet(slide, label, body, x,y,w, accent=CYAN, h=0.5, body_size=11):
    shape(slide,MSO_SHAPE.OVAL,x,y+0.08,0.12,0.12,accent,accent)
    richbox(slide,[(label,accent,True),(body,SILVER,False)],x+0.22,y,w,h,body_size)

def stat(slide, x,y,w,h, value, label, detail='', accent=CYAN):
    shape(slide,MSO_SHAPE.ROUNDED_RECTANGLE,x,y,w,h,CARD,GRID)
    line(slide,x,y+0.05,x+w,y+0.05,accent,2)
    textbox(slide,value,x+0.18,y+0.22,w-0.36,0.42,23,WHITE,True)
    textbox(slide,label.upper(),x+0.18,y+0.72,w-0.36,0.28,8,accent,True)
    if detail: textbox(slide,detail,x+0.18,y+1.03,w-0.36,h-1.12,9,SILVER)

def add_chart(slide,x,y,w,h,cats,vals,series_name='FY25', color=CYAN, maxv=None):
    data=CategoryChartData(); data.categories=cats; data.add_series(series_name,vals)
    chart=slide.shapes.add_chart(XL_CHART_TYPE.BAR_CLUSTERED,Inches(x),Inches(y),Inches(w),Inches(h),data).chart
    chart.has_legend=False; chart.value_axis.has_major_gridlines=True; chart.value_axis.major_gridlines.format.line.color.rgb=rgb(GRID)
    chart.value_axis.tick_labels.font.size=Pt(8); chart.value_axis.tick_labels.font.color.rgb=rgb(MUTED)
    chart.category_axis.tick_labels.font.size=Pt(8); chart.category_axis.tick_labels.font.color.rgb=rgb(SILVER)
    chart.chart_title=None; chart.plot.vary_by_categories=False
    chart.series[0].format.fill.solid(); chart.series[0].format.fill.fore_color.rgb=rgb(color)
    chart.series[0].format.line.color.rgb=rgb(color)
    if maxv: chart.value_axis.maximum_scale=maxv
    return chart

# 1 Cover
s=prs.slides.add_slide(blank); set_bg(s)
shape(s,MSO_SHAPE.RECTANGLE,8.35,0,4.98,7.5,SLATE,SLATE)
for i in range(8): line(s,8.5+i*0.52,0.0,13.3,4.8-i*0.35,'20345B',0.8)
shape(s,MSO_SHAPE.OVAL,9.5,1.0,2.6,2.6,'102B3A',CYAN)
shape(s,MSO_SHAPE.OVAL,10.12,1.62,1.36,1.36,NAVY,CYAN)
textbox(s,'247',0.62,0.45,1.0,0.3,14,CYAN,True)
pill(s,'EXECUTIVE BRIEFING  |  ENTERPRISE UTILITY AI',0.62,1.35,3.35)
textbox(s,'Autonomous Voice AI\nInfrastructure for BESCOM',0.62,1.9,7.15,1.42,30,WHITE,True)
textbox(s,'Slashing trade arrears, unlocking operational liquidity, and elevating 1912 citizen service with native Kannada voice intelligence',0.66,3.62,6.35,0.72,15,SILVER)
shape(s,MSO_SHAPE.ROUNDED_RECTANGLE,0.62,5.12,6.55,0.68,CARD,GRID)
textbox(s,'Prepared for: Sri Mahadeva, K.S.A.S  |  CFO & Director (Finance), BESCOM',0.82,5.31,6.15,0.25,10,WHITE,True)
textbox(s,'247AutogenV Team  •  In-person leadership briefing',0.66,6.55,5.4,0.25,10,MUTED)
footer(s)
notes(s,'Good morning, Sri Mahadeva, and members of the executive leadership team.\n\nToday is not a generic AI discussion. It is a finance-and-service operating proposal: use native Kannada voice automation to improve collection velocity, reduce avoidable 1912 pressure, and create stronger evidence trails around customer and payment interactions.\n\nWe will stay candid about what is confirmed, what is a pilot hypothesis, and what must be validated through a controlled BESCOM data handshake.')

# 2 baseline
s=prs.slides.add_slide(blank); set_bg(s); title(s,'01  |  Strategic finance baseline','Liquidity is constrained by scale, finance cost, and reconciliation friction','FY24–25 baseline: user-provided briefing figures paired with primary-report audit observations',2)
stat(s,0.62,1.9,3.75,1.55,'₹5,175.84 Cr','Closing trade receivables','Briefing baseline. Adjusted debtors cited in brief: ₹5,956.21 Cr.',CYAN)
stat(s,4.78,1.9,3.75,1.55,'₹2,288.51 Cr','Finance costs','Briefing baseline. Report should be reconciled to the final signed statements before circulation.',GREEN)
stat(s,8.94,1.9,3.75,1.55,'14.94 Mn','Consumers served','Briefing baseline across BESCOM’s eight-district service territory.',CYAN)
shape(s,MSO_SHAPE.ROUNDED_RECTANGLE,0.62,3.85,6.0,2.6,CARD,GRID)
textbox(s,'AUDIT-RELEVANT FRICTION',0.88,4.12,3.5,0.23,9,AMBER,True)
bullet(s,'TRM → ledger', '₹80.64 Cr debit item in GL 23.898 pending reconciliation as at 31 Mar 2025.',0.88,4.52,5.25,AMBER,0.52,11)
bullet(s,'Collateral reporting', 'Quarterly returns versus books showed differences from ₹1,813.67 Cr to ₹6,937.53 Cr.',0.88,5.16,5.25,RED,0.58,11)
bullet(s,'Control environment', 'Auditors reported no non-disableable audit-trail feature in accounting software.',0.88,5.88,5.25,CYAN,0.44,11)
shape(s,MSO_SHAPE.ROUNDED_RECTANGLE,6.9,3.85,5.79,2.6,'101827',GRID)
textbox(s,'CFO QUESTION',7.18,4.12,2.0,0.23,9,GREEN,True)
textbox(s,'Where can a controlled automation layer improve cash conversion and service resilience without creating another reconciliation surface?',7.18,4.55,4.98,0.88,20,WHITE,True)
textbox(s,'Decision lens: liquidity • revenue assurance • cost-to-serve • auditability',7.18,5.73,4.98,0.28,10,SILVER)
footer(s,'Source: BESCOM FY24–25 report, official domain | Briefing figures marked explicitly where report text was not independently matched')
notes(s,'The starting point is scale. The briefing baseline cites ₹5,175.84 crore of closing trade receivables, ₹2,288.51 crore of finance costs, and 14.94 million consumers. Before external circulation, Finance should reconcile these three figures to the signed financial-statement tables.\n\nThe primary FY24–25 report gives us a more specific control case. It identifies ₹80.64 crore in a pending reconciliation GL code linked to differences between TRM MIS DCB reports and the books. It also records quarterly reporting differences between collateral returns and the books ranging from ₹1,813.67 crore to ₹6,937.53 crore.\n\nThat is the problem we can responsibly address: not “AI fixes accounting,” but a controlled workflow that captures verified identifiers, commitments, and payment evidence before data reaches downstream reconciliation.')

# 3 voice
s=prs.slides.add_slide(blank); set_bg(s); title(s,'02  |  Solution one','Native Kannada voice for 1912: absorb demand without adding queue pressure','A service-resilience layer, subject to BESCOM CRM / DAS / SCADA validation',3)
shape(s,MSO_SHAPE.ROUNDED_RECTANGLE,0.62,1.88,4.05,4.9,CARD,GRID)
textbox(s,'CALLER EXPERIENCE',0.9,2.15,3,0.25,9,CYAN,True)
textbox(s,'“Current hogide.\nTransformer spark.”',0.9,2.68,3.1,0.78,25,WHITE,True)
textbox(s,'Recognise colloquial Kannada power vocabulary, capture the caller’s intent, and route the case with a structured record.',0.9,3.7,3.1,0.88,13,SILVER)
pill(s,'KANNADA-FIRST',0.9,5.04,1.55)
pill(s,'NO QUEUE',2.63,5.04,1.15,GREEN,'12332A')
textbox(s,'Design principle',0.9,5.78,1.4,0.22,8,MUTED,True)
textbox(s,'Voice is the front door. The system of record remains BESCOM’s approved CRM and complaint process.',0.9,6.07,3.1,0.47,10,SILVER)
# right flow
textbox(s,'FROM SPEECH TO RESOLUTION',5.12,1.98,4.0,0.25,9,CYAN,True)
steps=[('01','Understand','Kannada intent + RR number lookup'),('02','Classify','Category A–N complaint routing'),('03','Inform','Feeder status / restoration message'),('04','Close loop','Ticket ID + next-best action')]
for i,(num,head,body) in enumerate(steps):
    y=2.42+i*0.9
    shape(s,MSO_SHAPE.OVAL,5.12,y,0.46,0.46,'102B3A',CYAN); textbox(s,num,5.12,y+0.09,0.46,0.2,9,CYAN,True,align=PP_ALIGN.CENTER)
    textbox(s,head,5.82,y+0.02,1.65,0.25,13,WHITE,True); textbox(s,body,7.4,y+0.04,4.3,0.28,10,SILVER)
    if i<3: line(s,5.35,y+0.47,5.35,y+0.86,GRID,1)
shape(s,MSO_SHAPE.ROUNDED_RECTANGLE,5.12,6.13,7.55,0.64,'101827',GRID)
richbox(s,[('Pilot gate: ',GREEN,True),('validate Kannada ASR, CRM write-back, outage-data access, and escalation policy before any production claim.',SILVER,False)],5.36,6.31,7.08,0.29,10)
footer(s)
notes(s,'The first use case is 1912 and customer care. The operating idea is simple: understand the caller in the language they naturally use, identify the consumer and issue, create the structured complaint, and return a ticket or next action.\n\nThe capabilities shown here are proposed solution capabilities, not claims that BESCOM systems are already connected. The pilot must validate Kannada recognition across service districts, CRM write-back, RR-number lookup, escalation rules, and whether outage or restoration data can be exposed safely.\n\nThe value case is operational: absorb surge demand, reduce repetitive agent work, and provide a consistent case record that Finance and Operations can inspect later.')

# 4 collections
s=prs.slides.add_slide(blank); set_bg(s); title(s,'03  |  Solution two','Turn overdue accounts into a governed conversation, not a blind dialler','Working-capital lever: segment, explain, collect, and evidence the outcome',4)
# pipeline
pipe=[('01','PROFILE','DCB / TRM\nsegment'),('02','ENGAGE','Kannada\nconversation'),('03','COLLECT','UPI / BBPS\npayment path'),('04','EVIDENCE','UTR + promise\nledger')]
for i,(num,head,body) in enumerate(pipe):
    x=0.75+i*2.05
    shape(s,MSO_SHAPE.ROUNDED_RECTANGLE,x,2.06,1.72,1.56,CARD,GRID)
    textbox(s,num,x+0.16,2.24,0.4,0.2,9,GREEN,True); textbox(s,head,x+0.16,2.58,1.4,0.22,10,CYAN,True); textbox(s,body,x+0.16,2.98,1.4,0.43,11,WHITE,True)
    if i<3: line(s,x+1.72,2.84,x+2.03,2.84,GREEN,1.4)
textbox(s,'CFO MATHEMATICS',0.75,4.17,2.2,0.25,9,GREEN,True)
shape(s,MSO_SHAPE.ROUNDED_RECTANGLE,0.75,4.55,5.25,1.75,'101827',GRID)
textbox(s,'1% collection-velocity improvement',1.03,4.82,4.55,0.31,18,WHITE,True)
textbox(s,'≈ ₹51.75 Cr of receivables moved into liquidity',1.03,5.27,4.45,0.35,17,GREEN,True)
textbox(s,'Illustrative arithmetic on the briefing baseline of ₹5,175.84 Cr; not a forecast or guaranteed recovery.',1.03,5.84,4.5,0.28,8,MUTED)
shape(s,MSO_SHAPE.ROUNDED_RECTANGLE,6.42,4.55,6.25,1.75,CARD,GRID)
textbox(s,'GUARDRAILS',6.72,4.82,1.2,0.25,9,AMBER,True)
bullet(s,'Respectful', 'No coercion; approved scripts, opt-out handling, and contact-time rules.',6.72,5.18,5.3,AMBER,0.34,10)
bullet(s,'No overclaim', 'Payment links, acknowledgements, and ledger updates require verified provider receipts.',6.72,5.62,5.3,CYAN,0.34,10)
footer(s)
notes(s,'The second use case is collections. The workflow starts with DCB and TRM profiling, then uses a Kannada conversation to explain the specific account position, and only then offers an approved payment path. The system captures the transaction identifier, UTR, and promise-to-pay outcome for Finance.\n\nThe ₹51.75 crore figure is a transparent sensitivity calculation: one percent of the briefing baseline of ₹5,175.84 crore. It is not a recovery forecast. The pilot should measure contact rate, right-party contact, promise-to-pay conversion, payment completion, and reconciliation quality.\n\nThe CFO benefit is not merely more calls. It is faster cash conversion with evidence that can be reconciled.')

# 5 architecture
s=prs.slides.add_slide(blank); set_bg(s); title(s,'04  |  Architecture & controls','Add an evidence layer around TRM, ERP, and payment events','The design objective is fewer unidentified actions and stronger audit traceability',5)
# architecture boxes
layers=[('CHANNELS','1912 • Kannada voice\nOutbound collections',CYAN),('ORCHESTRATION','Identity • policy •\nworkflow state',GREEN),('SYSTEMS OF RECORD','TRM / DCB • ERP F&A\nCRM / complaint ledger',CYAN),('EVIDENCE','RR no. • transaction ID\nUTR • timestamp • consent',AMBER)]
for i,(head,body,col) in enumerate(layers):
    y=1.94+i*0.95
    shape(s,MSO_SHAPE.ROUNDED_RECTANGLE,0.78,y,4.35,0.72,CARD,GRID)
    line(s,0.78,y,0.84,y+0.72,col,4)
    textbox(s,head,1.06,y+0.13,1.85,0.2,9,col,True); textbox(s,body,2.72,y+0.12,2.1,0.38,10,WHITE,True)
    if i<3: line(s,2.95,y+0.73,2.95,y+0.95,GRID,1)
# right audit card
shape(s,MSO_SHAPE.ROUNDED_RECTANGLE,5.65,1.94,7.02,4.72,'101827',GRID)
textbox(s,'AUDIT DESIGN PRINCIPLES',5.98,2.23,3.0,0.25,9,CYAN,True)
principles=[('Verified identity','RR number, caller consent, account context'),('No unidentified credits','Evidence captured before receipt or commitment'),('Immutable event trail','Timestamped transcript, disposition, payment event'),('Zero-trust boundary','Least privilege, encryption, retention controls')]
for i,(h,b) in enumerate(principles):
    y=2.7+i*0.77
    shape(s,MSO_SHAPE.OVAL,5.98,y+0.02,0.24,0.24,GREEN,GREEN); textbox(s,'✓',5.98,y+0.015,0.24,0.2,10,NAVY,True,align=PP_ALIGN.CENTER)
    textbox(s,h,6.42,y,2.4,0.21,11,WHITE,True); textbox(s,b,8.78,y,3.35,0.26,10,SILVER)
textbox(s,'Primary report fact',5.98,5.85,1.25,0.2,8,AMBER,True)
textbox(s,'BESCOM auditors cite TRM MIS DCB-to-books differences and ₹80.64 Cr pending reconciliation in GL 23.898.',7.35,5.82,4.85,0.43,10,SILVER)
footer(s,'Architecture claims are proposal controls; final integration and security posture require BESCOM IT / CISO approval')
notes(s,'This is the architecture boundary. Voice is not the system of record. The orchestration layer should be policy-aware and should write only through approved BESCOM interfaces. Every meaningful event needs a verified identifier: RR number, transaction ID, UTR, timestamp, consent, and disposition.\n\nThe primary report is clear about the current control challenge: TRM DCB MIS reports and books do not always agree, and ₹80.64 crore was identified in a pending-reconciliation GL code as at 31 March 2025. This proposal is designed to avoid creating new unidentified activity.\n\nWe should not claim “pre-built webhooks,” “SCADA sync,” or compliance certification until BESCOM IT, cybersecurity, and ERP owners validate the actual interfaces and controls.')

# 6 pilot
s=prs.slides.add_slide(blank); set_bg(s); title(s,'05  |  Pull-through close','30-day proof of value: small perimeter, hard evidence, reversible decision','Two synchronized workstreams. One sandbox handshake. No production rollout assumed.',6)
# pilot perimeter
shape(s,MSO_SHAPE.ROUNDED_RECTANGLE,0.62,1.94,3.55,4.8,CARD,GRID)
textbox(s,'PILOT PERIMETER',0.9,2.22,2.3,0.22,9,CYAN,True)
textbox(s,'1 urban + 1 rural division',0.9,2.67,2.6,0.35,19,WHITE,True)
textbox(s,'Candidate examples',0.9,3.2,1.9,0.2,9,MUTED,True)
textbox(s,'Indiranagar / HSR Layout\nTumkur / Davanagere',0.9,3.47,2.6,0.58,14,SILVER,True)
line(s,0.9,4.33,3.78,4.33,GRID,1)
textbox(s,'ENTRY GATE',0.9,4.62,1.2,0.2,9,GREEN,True)
textbox(s,'Sandbox API + data handshake\napproved scripts + owners\nstop criteria agreed up front',0.9,4.94,2.75,0.9,12,WHITE,True)
# streams
shape(s,MSO_SHAPE.ROUNDED_RECTANGLE,4.45,1.94,4.05,2.25,'101827',GRID)
textbox(s,'WORKSTREAM A  |  CASH',4.75,2.23,3.0,0.23,9,GREEN,True)
textbox(s,'90+ day default accounts',4.75,2.68,3.2,0.28,16,WHITE,True)
bullet(s,'Measure', 'contact → promise → payment → reconciliation',4.75,3.17,3.25, GREEN,0.38,10)
shape(s,MSO_SHAPE.ROUNDED_RECTANGLE,8.72,1.94,3.95,2.25,CARD,GRID)
textbox(s,'WORKSTREAM B  |  SERVICE',9.02,2.23,3.0,0.23,9,CYAN,True)
textbox(s,'1912 Kannada spillover',9.02,2.68,3.2,0.28,16,WHITE,True)
bullet(s,'Measure', 'answer → ticket → resolution / escalation',9.02,3.17,3.15,CYAN,0.38,10)
# KPIs
textbox(s,'PILOT SCORECARD  |  TARGETS TO VALIDATE',4.45,4.56,4.5,0.22,9,AMBER,True)
kpis=[('>15%','Promise-to-pay','Pilot target'),('100%','Call capture','Peak outage window'),('>85%','Kannada CSAT','Survey sample')]
for i,(v,l,d) in enumerate(kpis):
    x=4.45+i*2.72
    shape(s,MSO_SHAPE.ROUNDED_RECTANGLE,x,4.93,2.42,1.36,CARD,GRID)
    textbox(s,v,x+0.18,5.18,2.05,0.35,20,GREEN if i==0 else CYAN,True)
    textbox(s,l.upper(),x+0.18,5.66,2.05,0.22,8,WHITE,True)
    textbox(s,d,x+0.18,5.96,2.05,0.18,8,MUTED)
shape(s,MSO_SHAPE.ROUNDED_RECTANGLE,4.45,6.43,8.22,0.33,'12332A',GREEN)
textbox(s,'DECISION REQUEST  →  GREENLIGHT SANDBOX API / DATA HANDSHAKE',4.65,6.49,7.8,0.18,9,GREEN,True,align=PP_ALIGN.CENTER)
footer(s)
notes(s,'The ask is deliberately narrow: approve a 30-day sandbox proof of value, not a production rollout. One urban and one rural division give us enough operating variation to test language, queue patterns, account segmentation, and governance without creating a large change program.\n\nWorkstream A focuses on 90-plus-day defaults and measures the entire chain, not just call volume. Workstream B focuses on Kannada 1912 spillover and measures whether a call becomes a correctly logged ticket and a clear next action.\n\nThe three targets shown are pilot targets to validate, not guaranteed outcomes. If Finance and Operations agree the data handshake, owners, and stop criteria are safe, the decision requested today is simply to greenlight the sandbox.')

# add sources slide? User asked 6 slides, keep sources as appendix notes? Put sources in notes and tiny footer. add hidden appendix not desired. Add citations in final notes? We'll add source URLs to slide 2 and 5 footer.
# Add document properties
prs.core_properties.title='247AutogenV | BESCOM Executive Briefing'
prs.core_properties.subject='Autonomous Voice AI Infrastructure for BESCOM'
prs.core_properties.author='247AutogenV Team'
prs.core_properties.keywords='BESCOM, Kannada voice AI, collections, 1912, finance, auditability'
prs.save(OUT)
print(OUT)
