from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage


def send_approved_digest_email(
    subject: str,
    body_markdown: str,
    recipient: str = 'getgad.uss@gmail.com',
) -> None:
    smtp_host = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
    smtp_port = int(os.environ.get('SMTP_PORT', '587'))
    smtp_user = os.environ['SMTP_USER']
    smtp_password = os.environ['SMTP_APP_PASSWORD']

    msg = EmailMessage()
    msg['From'] = smtp_user
    msg['To'] = recipient
    msg['Subject'] = subject
    msg.set_content(body_markdown)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)
