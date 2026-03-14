# Self-Hosted SMTP Setup Reference (Postfix + DKIM + SPF + DMARC)

## Overview
This configuration allows a server (e.g., a VPS) to send authenticated email using:

- Postfix (SMTP server)
- OpenDKIM (DKIM signing)
- SPF DNS record
- DMARC DNS record

The sending application connects to localhost SMTP instead of an external provider.

---

# 1. Server Identity

Choose a hostname for the mail server.

Example:

newsmail.example.com

DNS:

A record  
newsmail.example.com → SERVER_IP

Reverse DNS (PTR):

SERVER_IP → newsmail.example.com

Server hostname:

sudo hostnamectl set-hostname newsmail.example.com

---

# 2. Postfix Configuration

Edit:

/etc/postfix/main.cf

Recommended core settings:

myhostname = newsmail.example.com
mydomain = example.com
myorigin = $mydomain

mydestination = localhost

inet_interfaces = loopback-only

mynetworks = 127.0.0.0/8 [::1]/128

smtpd_relay_restrictions = permit_mynetworks permit_sasl_authenticated defer_unauth_destination

Meaning:

myhostname → SMTP identity  
myorigin → domain used in From rewriting  
mydestination → domains delivered locally  
inet_interfaces → listen only on localhost  
mynetworks → trusted IPs  
smtpd_relay_restrictions → relay policy

Restart:

sudo systemctl restart postfix

---

# 3. Connecting Postfix to OpenDKIM

Add to main.cf:

milter_default_action = accept
milter_protocol = 6

smtpd_milters = unix:/opendkim/opendkim.sock
non_smtpd_milters = unix:/opendkim/opendkim.sock

Restart:

sudo systemctl restart postfix

---

# 4. OpenDKIM Configuration

Main file:

/etc/opendkim.conf

Minimal working configuration:

Syslog yes
SyslogSuccess yes

Canonicalization relaxed/simple
OversignHeaders From

Mode sv

Domain example.com
Selector mail
KeyFile /etc/opendkim/keys/example.com/mail.private

InternalHosts 127.0.0.1

UserID opendkim
UMask 007

Socket local:/run/opendkim/opendkim.sock

PidFile /run/opendkim/opendkim.pid

TrustAnchorFile /usr/share/dns/root.key

Restart:

sudo systemctl restart opendkim

---

# 5. DKIM Keys

Generate:

opendkim-genkey -t -s mail -d example.com

Files created:

mail.private  
mail.txt

Move private key:

sudo mkdir -p /etc/opendkim/keys/example.com
sudo mv mail.private /etc/opendkim/keys/example.com/

sudo chown opendkim:opendkim /etc/opendkim/keys/example.com/mail.private
sudo chmod 600 /etc/opendkim/keys/example.com/mail.private

---

# 6. DKIM DNS Record

Add DNS TXT record:

mail._domainkey.example.com

Value (from mail.txt):

v=DKIM1; k=rsa; p=PUBLIC_KEY

Verify:

opendkim-testkey -d example.com -s mail -vvv

Expected:

key OK

---

# 7. SPF DNS

Only one SPF record allowed per domain.

Example:

example.com TXT  
v=spf1 ip4:SERVER_IP include:zohomail.com include:zcsend.net ~all

Meaning:

ip4:SERVER_IP → allow your SMTP server  
include:zohomail.com → allow Zoho Mail  
include:zcsend.net → allow Zoho campaigns/transactional  
~all → soft fail for others

---

# 8. Optional HELO SPF

Fixes SPF_HELO_NONE warnings.

newsmail.example.com TXT  
v=spf1 ip4:SERVER_IP -all

---

# 9. DMARC DNS

Minimal DMARC:

_dmarc.example.com TXT  
v=DMARC1; p=none; sp=none; adkim=r; aspf=r

Recommended:

v=DMARC1; p=none; rua=mailto:dmarc@example.com; ruf=mailto:dmarc@example.com; fo=1; adkim=r; aspf=r

Create mailbox:

dmarc@example.com

Later enforcement:

p=quarantine

or

p=reject

---

# 10. Sending Mail From CLI

Example sendmail test:

printf 'From: Example <noreply@example.com>\nTo: user@gmail.com\nSubject: Test\nDate: '"$(LC_ALL=C date -R)"'\n\nHello from server.\n' | sendmail -f noreply@example.com user@gmail.com

---

# 11. Sendmail Test Script

#!/usr/bin/env bash

FROM_NAME="Example News"
FROM_EMAIL="noreply@example.com"
TO_EMAIL="test@mail-tester.com"
HOST_FQDN="newsmail.example.com"

DATE_HEADER="$(LC_ALL=C date -R)"
MESSAGE_ID="<$(date +%s).$RANDOM@$HOST_FQDN>"

sendmail -f "$FROM_EMAIL" "$TO_EMAIL" <<EOF
From: $FROM_NAME <$FROM_EMAIL>
To: $TO_EMAIL
Subject: Postfix test
Date: $DATE_HEADER
Message-ID: $MESSAGE_ID
MIME-Version: 1.0
Content-Type: text/plain; charset=UTF-8

Hello from my Postfix test server.
EOF

---

# 12. Python SMTP Client (Local Postfix)

Example sending through localhost:

import smtplib
from email.message import EmailMessage

SMTP_HOST = "127.0.0.1"
SMTP_PORT = 25

ENVELOPE_FROM = "noreply@example.com"

def connect_smtp():
    s = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
    s.ehlo()
    return s

def send_email(rcpt):
    msg = EmailMessage()
    msg["From"] = "Example News <noreply@example.com>"
    msg["To"] = rcpt
    msg["Subject"] = "Newsletter"
    msg.set_content("Hello from our mail server.")

    with connect_smtp() as smtp:
        smtp.sendmail(ENVELOPE_FROM, [rcpt], msg.as_bytes())

---

# 13. Testing Tools

Recommended tests:

check-auth@verifier.port25.com

or

mail-tester.com

Check results for:

SPF: PASS  
DKIM: PASS  
DMARC: PASS

---

# 14. Useful Debug Commands

Mail logs:

sudo tail -f /var/log/mail.log

Queue:

mailq

Postfix config:

postconf | grep milter

DNS check:

dig TXT example.com

---

# 15. Best Practices

- Use a dedicated sending hostname (newsmail.example.com)
- Warm up new servers gradually
- Keep only one SPF record
- Always sign mail with DKIM
- Publish DMARC even in monitor mode
- Send consistent volume patterns
