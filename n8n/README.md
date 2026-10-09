# n8n workflow: forward new leads to Google Sheets and email

`brightsmile-leads-workflow.json` receives each consented lead from the assistant and:
1. appends it as a row to a Google Sheet (tab `Leads`), and
2. emails the front desk.

```
Webhook (POST, header auth) → Pick lead fields → Append to Google Sheet
                                               → Email front desk
```

## Import
1. In n8n: **Workflows → Import from File** and pick `brightsmile-leads-workflow.json`
   (or CLI: `n8n import:workflow --input=brightsmile-leads-workflow.json`).
2. Create three credentials and select them on the nodes (exports never contain credentials;
   the `REPLACE_ME` ids are placeholders):
   - **Header Auth** on the webhook node: name `X-Webhook-Secret`, value = a long random string.
   - **Google Sheets OAuth2** on the Sheets node. Replace `YOUR_SHEET_ID` with your sheet's URL
     and add a header row: `created_at, name, contact, contact_type, preferred_time, consent,
     consent_text_version, session_id, source`.
   - **SMTP** on the email node; change the from/to addresses.
3. Activate the workflow and copy the **production** webhook URL.
4. Configure the assistant:
   ```
   LEAD_WEBHOOK_URL=https://your-n8n.example.com/webhook/brightsmile-lead
   LEAD_WEBHOOK_SECRET=<same random string as the Header Auth credential>
   ```

The assistant always writes `leads.csv` first; the webhook is best-effort, so an n8n outage never
loses a lead (it is logged as a warning).

## What was verified
- The JSON imports cleanly with the n8n CLI (`import:workflow`, n8n 2.42.6) and exports back
  with all 4 nodes and connections.
- The assistant's webhook call (JSON body + `X-Webhook-Secret` header) is covered by a pytest
  test against a local HTTP server.
- **Not verified end-to-end:** sending to a real Google Sheet / SMTP server (needs real credentials).
