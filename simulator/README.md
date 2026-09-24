# simulator

Synthetic alert generation for local development and (later) eval fixture
recording. `send_alert.py` is a small CLI that POSTs a synthetic alert to
the running API and prints the resulting incident -- the fastest way to
exercise the Phase 1 vertical slice by hand:

```
make send-alert
# or, with options:
python simulator/send_alert.py --service checkout --severity warning
```

More elaborate scenario generation (multi-alert storms, realistic
Alertmanager payload shapes) is future work once correlation grows beyond
Phase 1's minimal rule.
