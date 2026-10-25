"""Data-retention periods (decided 2026-10-19).

These must match the published privacy policy. Legal review is still
required before general availability; see docs/design/phase-2.1-database-design.md §13.
"""

from datetime import timedelta

# Security investigations and support-access review. 12 months is the common
# benchmark for audit logs (e.g. PCI DSS 10.5.1).
AUDIT_EVENTS = timedelta(days=365)

# Webhook payloads contain personal data and are only needed for replay
# protection (Stripe retries for up to 3 days) and debugging.
WEBHOOK_EVENTS_SETTLED = timedelta(days=30)  # processed or ignored
WEBHOOK_EVENTS_UNSETTLED = timedelta(days=90)  # received or failed, kept longer for investigation

# Raw CSV files and row-level previews duplicate data that now lives in
# invoices; keep them only long enough to debug an import.
IMPORT_DATA_AFTER_FINISH = timedelta(days=30)
IMPORT_ABANDONED = timedelta(days=7)  # uploaded or validated but never committed

# A workspace without a writable subscription stays readable and exportable
# for this long, then becomes due for deletion (not automated yet: deletion
# waits until 30- and 7-day warning emails exist).
READ_ONLY_WORKSPACE = timedelta(days=90)
