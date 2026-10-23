# SCCRS Increment 2 & 3 — Auto-routing, SLA deadlines, and escalation

## What's added
- Automatically assigns each new complaint to an active resolution officer in the selected department who currently has the fewest `Pending` + `In Progress` complaints.
- The officer dashboard query includes the assigned officer's name, so the `Assigned officer` column is not blank when an assignment exists.
- SLA deadlines: High priority = 24 hours; Normal priority = 72 hours.
- Dashboard displays deadlines and marks overdue unresolved complaints; overdue complaints appear first in the officer workspace.
- An assigned officer can escalate an unresolved complaint only after its SLA deadline has passed, and must provide a reason of at least 10 characters. The department head and administrator receive in-app notifications (and email if SMTP is configured).
- Existing complaints are given a deadline during database initialization if they do not have one.

## Run on Windows
1. Back up your existing `sccrs.db` before replacing files.
2. Extract this ZIP into a new folder.
3. Open that folder in VS Code.
4. Run:
   ```bash
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   python app.py
   ```
5. Open http://127.0.0.1:5000

The app runs database initialization and migrations at startup. Keep your existing database file if you want to retain existing accounts and complaints. Back it up first.

## Create resolution officers
Log in as admin (`admin@campus.edu` / `Admin@123` on a fresh database), then use **Create staff account** on the admin dashboard or open `/staff/new`. Create each officer in the department they handle. Example: Amit Patil, `amit.officer@campus.edu`, role `Resolution officer`, department `Facilities`.

## Workflow
1. Student selects a department and priority and submits a complaint.
2. The app counts active complaints for each officer in that department and assigns the ticket to the least-loaded officer. If no officer exists in that department, it remains unassigned and admin/department-head accounts are notified.
3. Officer opens the ticket, updates status, and adds progress notes.
4. High-priority tickets are due in 24 hours; Normal tickets are due in 72 hours.
5. If the ticket remains unresolved after the deadline, the assigned officer can open the ticket and escalate it with a reason. Department head and admin are notified; they can then review/reassign it.

## Notes
- SMTP email delivery is optional and requires `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, and optionally `SMTP_FROM` environment variables. In-app notifications work without SMTP.
- The deadline is an SLA timestamp, not an automatic background scheduler. The page flags overdue complaints when viewed; escalation is validated server-side against the deadline.
- For production, change `SECRET_KEY`, use HTTPS, and configure a production database and email service.
