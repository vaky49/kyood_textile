# WhatsApp Setup

## Cloud API

Configure `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET`, `WHATSAPP_ACCESS_TOKEN`, and `WHATSAPP_PHONE_NUMBER_ID`. Set `MESSAGING_PROVIDER=whatsapp_cloud` to enable outbound text/RFQ/negotiation messages. `WHATSAPP_API_VERSION` defaults to `v24.0`.

Register `GET /webhooks/whatsapp` as the verification endpoint and `POST /webhooks/whatsapp` as the event endpoint. The GET route validates `hub.mode`, `hub.verify_token`, and returns `hub.challenge`. POST validates `X-Hub-Signature-256` against the raw request body.

The webhook accepts normalized test events and actual Meta Cloud API `entry/changes/value/messages` batches. Text, image, document, audio, video, interactive replies, and buttons are normalized. External message IDs are unique; duplicate delivery returns accepted without creating another request/job. Replies from known supplier numbers are attached to the active RFQ or negotiation; unmatched replies are retained for operator review.

## Local Mock

Leave `MESSAGING_PROVIDER=mock`. Tests can post a normalized message:

```json
{
  "external_message_id": "wamid-demo-1",
  "sender_phone": "+919876543210",
  "profile_name": "Demo Buyer",
  "text": "Need 100 cotton ladies suits around INR 500-600, delivery to Jaipur",
  "message_type": "text"
}
```

For normalized image messages, supply `message_type: "image"` and a `media_url` reference; Cloud events carry a provider media ID and the worker stores the downloaded asset when Cloud credentials are configured.