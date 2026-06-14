# Public Event Schemas

These schemas document the public SAI sponsor-event boundary.

They are intentionally limited to fields that can cross the privacy boundary.
They do not publish backend anti-abuse logic, placement signature internals,
campaign caps, billing formulas, fraud thresholds, or qualification algorithms.

Local command:

```bash
sai privacy schema
```

## Files

- `placement-event.schema.json`: allowlisted placement event fields.
- `placement-request.schema.json`: public placement request metadata shape.
- `wait-state-event.schema.json`: sanitized wait-state event metadata.
- `forbidden-fields.json`: field names that must not appear in sponsor events.
- `examples/cli-rendered.json`: terminal placement event example.
- `examples/overlay-qualified.json`: desktop overlay placement event example.
- `examples/privacy-schema-output.json`: representative `sai privacy schema`
  output.
- `examples/rejected-extra-fields.json`: example payload that should be rejected
  or stripped because it contains forbidden work-context fields.
