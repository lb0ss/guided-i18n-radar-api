"""Digest delivery: always saved to data/digests/, emailed and pushed when configured in .env."""

import smtplib
from email.message import EmailMessage
from pathlib import Path

import httpx

from .config import Settings


def save(s: Settings, name: str, text: str) -> Path:
    d = s.data_dir / "digests"
    d.mkdir(exist_ok=True)
    p = d / f"{name}.md"
    p.write_text(text)
    return p


def email(s: Settings, subject: str, text: str) -> str:
    if not (s.smtp_host and s.smtp_user and s.smtp_password and s.digest_to):
        return "email not configured"
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, s.digest_from, s.digest_to
    msg.set_content(text)
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(s.smtp_user, s.smtp_password)
        smtp.send_message(msg)
    return "emailed"


def push(s: Settings, title: str, text: str) -> str:
    if not (s.pushover_token and s.pushover_user):
        return "push not configured"
    r = httpx.post("https://api.pushover.net/1/messages.json", timeout=30,
                   data={"token": s.pushover_token, "user": s.pushover_user, "title": title, "message": text[:1000]})
    r.raise_for_status()
    return "pushed"
