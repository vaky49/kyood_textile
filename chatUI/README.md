# Local WhatsApp test chat

A standalone, testing-only browser UI for the procurement application. It uses
Python's standard library (no Gradio, Node, or extra packages). The production
application does not import, start, or depend on this folder.

## Start on Windows

1. Start your **test** API, PostgreSQL, and worker from the repository root:

   ```powershell
   Set-Location C:\per\kyood_tex\Impl
   docker compose up -d
   docker compose exec api python -m app.seed
   ```

2. Ensure the backend worker uses `MESSAGING_PROVIDER=mock`. The UI creates real
   database records and cannot suppress WhatsApp sends if the backend is configured
   with `whatsapp_cloud`. Use a separate local database, not production. LLM/vision
   providers can still make paid external calls if configured; use mock LLMs and
   fictional data for offline testing.

3. Open a new PowerShell terminal:

   ```powershell
   Set-Location C:\per\kyood_tex\Impl
   $env:CHATUI_OPERATOR_TOKEN = "replace-with-a-local-development-token"
   .\.venv\Scripts\python.exe .\chatUI\server.py
   ```

   Replace the token with the **actual** `OPERATOR_API_TOKEN` used by your backend.
   The value above matches the checked-in mock Compose configuration. If no local
   virtual environment exists, any installed Python 3.12+ can run the server:
   `py -3.12 .\chatUI\server.py`. No dependency installation is needed.

4. Open **http://127.0.0.1:7860** (use this exact host, not `localhost`).
5. Keep the server terminal open. Stop it with **Ctrl+C**.

The UI does not automatically load `.env`. Never put real credentials in source
files or browser JavaScript. Credentials remain on the local server.

### Optional environment variables

| Variable | Default / purpose |
| --- | --- |
| `CHATUI_API_URL` | `http://127.0.0.1:8000`; local HTTP origins only |
| `CHATUI_OPERATOR_TOKEN` | Required; falls back to `OPERATOR_API_TOKEN` |
| `CHATUI_WHATSAPP_APP_SECRET` | Falls back to `WHATSAPP_APP_SECRET`; signs simulated webhooks if backend signature verification is enabled |
| `CHATUI_PORT` | `7860`; server always binds to `127.0.0.1` |

If the backend has a WhatsApp app secret configured, set the same value before
starting this server:

```powershell
$env:CHATUI_WHATSAPP_APP_SECRET = "<same secret as the backend>"
```

There is no need for Meta credentials, a tunnel, or a real WhatsApp account when
the test backend has no app secret and uses mock providers. Do not expose this
test server to a network or deploy it as a buyer application.

## Try the workflow

1. Keep the default fictional test buyer (separate from seeded demo buyers), or
   click **New buyer** for a fresh fictional identity. Do not use a registered
   supplier's number. The seeded number `+911111110001` already has a request
   awaiting a buyer decision; messages from it update that request rather than
   creating a new Requests row.
2. Check the test-backend acknowledgement.
3. Send: `Need 100 cotton ladies suits around INR 500-600. Delivery to Jaipur.`
4. The server posts a normalized incoming message to `POST /webhooks/whatsapp`.
   Each message gets a unique external ID. Unchanged retries retain that ID.
5. The UI polls authenticated `GET /api/requests/{id}` every three seconds for
   persisted conversation messages, extracted requirements, status, and pending
   approvals. It does not manufacture assistant replies or success messages.
6. Use the existing operator dashboard at **http://localhost:3000** for supplier
   selection, RFQ approval, uncertain response review, negotiation approval, and
   order approval. This UI deliberately does not approve anything.
7. Supplier replies must be simulated through the existing webhook using the
   selected supplier's registered number (or received through your configured
   provider). Mock transport does not generate supplier quotes automatically.
8. Once a comparison is ready and the request is `AWAITING_BUYER_DECISION`, send
   the selection/negotiation reply using the same buyer identity.

### Finding a chat message in the operator dashboard

The Requests list refreshes every five seconds while visible and when you return
to its tab. Clear the status filter to **All statuses** and search for the request
ID shown by Chat UI (the table displays its first eight characters). Messages
that update an existing buyer-decision request do not create another row; search
for that same ID and open the existing request. If no request ID appears in Chat UI,
check its error banner rather than assuming the message was accepted.

Both UIs must use the same API instance. Chat UI displays its target under the
buyer controls (`CHATUI_API_URL`, default `http://127.0.0.1:8000`). The operator
frontend uses `NEXT_PUBLIC_API_BASE_URL` (default `http://localhost:8000`).
If you override either, keep them pointed at the same backend and restart the
relevant development server. Confirm both use the correct operator token.

After a reload, paste a request ID from the dashboard, enter its buyer phone,
and click **Load / refresh**. History comes from the application's database, not
browser storage. The request API returns the buyer's entire conversation, which
may contain messages for multiple requests. **New buyer** does not delete data.

### Images

Attach a JPEG/PNG/WebP up to 5 MB, or enter an HTTPS image URL. Attached files
are sent as data URLs through the existing normalized `media_url` field; they
are persisted in the message record, not uploaded through the real WhatsApp
media API or object storage. Remote URLs are not loaded by this test browser.
The backend/model may fetch them if vision is configured.

Mock extraction does not perform actual visual recognition. This UI tests
media submission, not the full provider media download/storage pipeline.

### Current backend limitations are preserved

- Incoming webhook replies are attached to an existing buyer decision only
  when that request is awaiting a decision. Otherwise the webhook creates a new
  request; clarification replies are not resumed automatically. Supply a complete
  corrected requirement as a new message if clarification is needed.
- The mock extractor is rule-based, not a general language model. Use
  `Delivery to Jaipur` (including `to`) in a new buyer's requirement.
- Worker statuses are displayed separately, not disguised as assistant messages.
  An immediate acknowledgement or clarification chat bubble may not be emitted
  by the current backend.
- On duplicate acceptance the webhook does not return the original request ID.
  After an ambiguous timeout, retry the unchanged message once. If it is reported
  as a duplicate, recover its ID from the dashboard instead of creating a new ID.
- This is an operator-assisted simulator with access to management APIs, not a
  secure multi-buyer portal.

## Tests

Run from the repository root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s .\chatUI -p "test_*.py" -v
```

Tests run an isolated fake HTTP backend and the real local UI server. No database,
Meta account, app imports, or third-party Python packages are required.
