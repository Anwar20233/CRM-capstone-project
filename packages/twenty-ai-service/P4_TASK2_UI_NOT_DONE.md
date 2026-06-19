# P4 Task 2 — UI (Drafting Agent UI branch)

> **Branch:** `drafting-agent-ui`  
> **Reference:** Backend APIs in `twenty-ai-service` (see `P4_TASK2_BACKEND_SUMMARY.md`)

---

## Summary

v1 Follow-Up Intelligence UI is scaffolded in `packages/twenty-front/src/modules/followup-intelligence/`. Opportunity records get a **Follow-Up** tab that polls pending drafts from `twenty-ai-service` (`:8001`).

---

## Done (v1)

### Module scaffold

- [x] Create `packages/twenty-front/src/modules/followup-intelligence/`
- [x] Barrel exports in `index.ts`
- [x] Wire module into opportunity record page (Follow-Up tab via `WidgetType.VIEW`)

### Data layer (REST hooks)

- [x] `useFollowupActions(opportunityId)` — poll `GET /followup/actions?opportunity_id=…`
- [x] `useFollowupProfile(opportunityId)` — `GET /followup/profile/{id}`
- [x] `acceptFollowupAction` / `rejectFollowupAction` / `reviseFollowupAction`
- [x] Error + loading states; refetch after accept/reject; 30s polling

### Components

- [x] **OpportunityHealthPanel** — risk score, pending action count, narrative
- [x] **DraftPreview** — subject/body
- [x] Copy-to-clipboard for subject / body / all
- [x] **Accept** / **Reject** / **Revise** buttons
- [x] Urgency badge + expiry countdown

### UX flows (v1)

- [x] Rep opens opportunity → **Follow-Up** tab → sees pending draft
- [x] Rep copies draft → manual send in Twenty email composer
- [x] Rep clicks Accept → backend executes send via bridge
- [x] Polling for new pending actions (30s interval)

### Infrastructure

- [x] CORS on `twenty-ai-service` for browser → `:8001` calls
- [x] `REACT_APP_AI_SERVICE_URL` (defaults to `http://<host>:8001`)

---

## Still out of scope (v2+)

- [ ] One-click Send (`sendEmail` GraphQL) from DraftPreview
- [ ] Proposal PDF attachment UI
- [ ] `GET /followup/health/{id}` dedicated endpoint
- [ ] Dedicated `WidgetType.FOLLOWUP_INTELLIGENCE` in GraphQL metadata (uses `VIEW` interim)
- [ ] In-app toast when new pending action arrives (beyond polling refresh)
- [ ] Unit tests for followup-intelligence components/hooks

---

## How to run

1. Start Twenty backend (`:3000`) and `twenty-ai-service` (`:8001`)
2. Start frontend: `npx nx start twenty-front`
3. Open any opportunity with pending actions
4. Go to **Follow-Up** tab

Optional env:

```bash
REACT_APP_AI_SERVICE_URL=http://127.0.0.1:8001
```

---

## Backend APIs used

| Endpoint | Purpose |
|----------|---------|
| `GET /followup/actions` | List pending actions |
| `POST /followup/actions/{id}/accept` | Accept → execute |
| `POST /followup/actions/{id}/reject` | Reject |
| `POST /followup/actions/{id}/revise` | Revise → new draft |
| `GET /followup/profile/{opportunity_id}` | Health narrative + risk |
