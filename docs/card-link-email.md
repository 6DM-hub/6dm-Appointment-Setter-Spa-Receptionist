# Secure card-link email

This release adds an owner-operated Calendar action for existing provider-backed
appointments with `pending_card`. For the configured tenant, the realtime call
flow collects and reads back an email, waits for explicit address confirmation,
then explains the card policy and asks permission to email the link. After a
successful provider booking it sends email instead of SMS. Other tenants retain
their existing SMS behavior. Ara's voice and audio connection are unchanged.

## Server configuration

Set these on the correct Railway backend service, without putting credentials
in chat, git, frontend settings or logs:

- `CARD_EMAIL_TENANT_ID`: exact Healing Waters SpaAccount UUID (not Railway project ID).
- `CARD_SMTP_HOST`: `smtp.gmail.com`
- `CARD_SMTP_PORT`: `465`
- `CARD_SMTP_USER`: `caravas4u@gmail.com`
- `CARD_EMAIL_DISPLAY_NAME`: `Healing Waters Wellness and MedSpa`
- `CARD_SMTP_PASSWORD`: new Gmail app password, entered privately by the owner.
- `PUBLIC_BASE_URL`: existing HTTPS Cara backend URL; preserve the correct value.

No email can send until the tenant UUID and credentials are configured.
The display name uses this tenant-scoped override or the account's business name. This initial transport
is deliberately restricted to one tenant. Other tenants need their own transport
configuration before email can be enabled for them. Staff-alert settings are untouched.

## Use and verification

In Calendar, expand **Email secure card link** on a pending-card booking. Verify
the customer's saved contact email, enter that same email, and confirm permission.
Only spa admins or platform admins acting within the correct tenant may send.
The endpoint is `POST /api/v1/appointments/{id}/card-email` with
`confirmed_email`, `consent`, and a UUID `request_id`. Reusing the request ID
does not resend, including after a timeout; it returns `already_attempted`.

Mail-server acceptance does not prove inbox delivery. Check the recipient's inbox
and spam folder. Complete the existing Square card form and verify the card state
becomes confirmed without creating another appointment. No charge is made.
Never automatically retry an uncertain SMTP result. An explicit fresh request
requires checking whether the first message arrived.

Email tokens are persisted before send. Sending email does not revoke working
SMS links. A known refusal revokes only the new token; timeout leaves it usable
because the server might already have accepted the email. Booking status is never
changed by delivery failure. Existing card-save handling closes access once the
card is confirmed. Live Gmail delivery and real Square save require configuration
and an authorized controlled test after deployment.
