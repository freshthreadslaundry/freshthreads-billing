"""Portable PDF invoice export, using the same calculated context as HTML invoices."""
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle


def create_invoice_pdf(context):
    bill = context['bill']
    styles = getSampleStyleSheet()
    normal = styles['BodyText']

    def text(value):
        return Paragraph(escape(str(value or '')), normal)

    output = BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4, rightMargin=18*mm, leftMargin=18*mm,
                            topMargin=15*mm, bottomMargin=15*mm, title=f"Fresh Threads Invoice {bill['id']}")
    story = [Paragraph('Fresh Threads Laundry &amp; Dry Cleaning Services', styles['Title'])]
    for line in ('H.No 1, 4/175/53, TELANGANA, 500084', 'GST: 36DJKPS4114P1ZK',
                 '7036022033 | customercare@freshthreadslaundry.co.in',
                 f"Bill No: {bill['id']} | Bill Date: {bill['formatted_date']}",
                 f"Customer: {bill['customer_name']} | Phone: {bill['customer_phone']}",
                 f"Address: {bill.get('pickup_address') or ''}",
                 f"Order Type: {bill.get('order_type') or ''} | Delivery Date: {bill['formatted_due_date']}"):
        story.append(text(line))
    story.append(Spacer(1,6*mm))
    rows = [['Item','Qty / Pieces','Rate (INR)','Total (INR)']]
    for item in context['items']:
        rows.append([text(item['service_name']), str(item['items']), f"{item['rate']:.2f}",f"{item['total']:.2f}"])
    table = Table(rows, colWidths=[83*mm,28*mm,27*mm,36*mm], repeatRows=1, hAlign='LEFT')
    table.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#eeeeee')),
        ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),
        ('GRID',(0,0),(-1,-1),.4,colors.lightgrey),
        ('VALIGN',(0,0),(-1,-1),'TOP'),
        ('ALIGN',(1,0),(-1,-1),'RIGHT'),
        ('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7),
    ]))
    story.extend([table, Spacer(1,4*mm)])
    amounts = [('Subtotal',context['subtotal']),('Discount',-context['discount_amount']),
               ('Express Service',context['surcharge_amount']),('Wallet Used',-context['wallet_used']),
               ('Final Bill',context['final_total']),('Advance Paid',context['advance_paid']),
               ('Amount Due',context['amount_due'])]
    summary = Table([[label,f'INR {value:.2f}'] for label,value in amounts],colWidths=[125*mm,49*mm])
    summary.setStyle(TableStyle([('ALIGN',(1,0),(1,-1),'RIGHT'),('FONTNAME',(0,-1),(-1,-1),'Helvetica-Bold')]))
    story.extend([summary, Spacer(1,4*mm)])
    status = 'CANCELLED - no payment due' if bill['is_cancelled'] else ('PAID' if bill['is_paid'] else 'Payment Pending')
    story.append(text(status))
    story.append(text('Total Items: '+str(sum(item['items'] or 0 for item in context['items']))))
    story.append(Spacer(1,4*mm))
    story.append(text('Thank you! Visit again.'))
    story.append(text('All items are accepted subject to our Terms & Conditions displayed at the store and available online.'))
    story.append(text('www.freshthreadslaundry.co.in/terms.html'))
    doc.build(story)
    output.seek(0)
    return output
