# n8n workflows: forward new leads, delete them after 90 days

`brightsmile-leads-workflow.json` receives each consented lead from the assistant and:
1. appends it as a row to a Google Sheet (tab `Leads`, written as RAW values), and
2. emails the front desk a notice **without personal data** ("new request, see the sheet").

`brightsmile-leads-cleanup-workflow.json` runs daily at 03:00 and deletes Sheet rows whose
`created_at` is older than 90 days, the same rule the app applies to `leads.csv`.

Visitors are told "we automatically delete this request after 90 days" (consent text `v2`).
That holds only if **both** workflows are active: the app purges its CSV, the cleanup workflow
purges the Sheet, and the email never contained the data. If you don't run the cleanup
workflow, you must delete old Sheet rows by hand.

```
Webhook (POST, header auth) → Pick lead fields → Append to Google Sheet
                                               → Email front desk
```

## Import
1. In n8n: **Workflows → Import from File** and pick both JSON files
   (or CLI: `n8n import:workflow --input=<file>.json`).
2. Create three credentials and select them on the nodes (exports never contain credentials;
   the `REPLACE_ME` ids are placeholders):
   - **Header Auth** on the webhook node: name `X-Webhook-Secret`, value = a long random string.
   - **Google Sheets OAuth2** on the Sheets node. Replace `YOUR_SHEET_ID` with your sheet's URL
     and add a header row: `created_at, name, contact, contact_type, preferred_time, consent,
     consent_text_version, session_id, source`.
   - **SMTP** on the email node; change the from/to addresses.
3. In the cleanup workflow, select the same Google Sheets credential and sheet URL.
4. Activate both workflows and copy the **production** webhook URL.
5. Configure the assistant:
   ```
   LEAD_WEBHOOK_URL=https://your-n8n.example.com/webhook/brightsmile-lead
   LEAD_WEBHOOK_SECRET=<same random string as the Header Auth credential>
   ```

The assistant always writes `leads.csv` first; the webhook is best-effort, so an n8n outage never
loses a lead. Network errors and 5xx responses are retried twice (after ~1 s and ~4 s); a 4xx or a
redirect is not retried. A final failure is logged as an error and the lead is only in the CSV. A
retry can deliver the same lead twice if n8n handled it but the response was lost, so dedupe on
`session_id` if that matters.

## What was verified
- Both JSON files import cleanly with the n8n CLI (`import:workflow`, n8n 2.42.6).
- The cleanup workflow's row-selection code was run with Node against sample rows (old, recent,
  unparseable dates): it returns only the expired rows, highest row number first. Node
  parameter names (`read`, `delete`, `row_number`, `cellFormat`) were checked in the n8n node source.
- The assistant's webhook call (JSON body + `X-Webhook-Secret` header) is covered by a pytest
  test against a local HTTP server.
- **Not verified end-to-end:** running either workflow against a real Google Sheet / SMTP server
  (needs real credentials).
