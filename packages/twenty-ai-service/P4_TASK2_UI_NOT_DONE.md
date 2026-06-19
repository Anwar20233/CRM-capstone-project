# P4 Task 2 — UI (Not Started)

> **Status:** Not done — explicitly deferred from [P4 Task 2 Backend plan](.cursor/plans/p4_task_2_backend_82f506d6.plan.md).  
> **Scope:** All `twenty-front` Follow-Up Intelligence UI work.

---

## Summary

Backend Task 2 is implemented in `twenty-ai-service` (email workflows, accept/send API, e2e scripts). **No React UI exists yet** — `packages/twenty-front/src/modules/followup-intelligence/` has **0 files**.

---

## Not done checklist

### Module scaffold

- [ ] Create `packages/twenty-front/src/modules/followup-intelligence/`
- [ ] Barrel exports in `index.ts`
- [ ] Wire module into opportunity record page / settings entry point

### Data layer (GraphQL / REST hooks)

- [ ] `useFollowupActions(opportunityId)` — poll `GET /followup/actions?opportunity_id=…`
- [ ] `useFollowupProfile(opportunityId)` — `GET /followup/profile/{id}` (optional narrative panel)
- [ ] `acceptFollowupAction(actionId, userId)` — `POST /followup/actions/{id}/accept`
- [ ] `rejectFollowupAction` / `reviseFollowupAction` hooks
- [ ] Error + loading states; refetch after accept/reject

### Components

- [ ] **OpportunityHealthPanel** — risk score, open concerns, pending action count
- [ ] **DraftPreview** — human-readable subject/body (not raw JSON)
- [ ] Copy-to-clipboard for subject + body
- [ ] **Accept** button → calls accept API, shows execution status
- [ ] **Reject** / **Revise** buttons (chat loop)
- [ ] Urgency badge + expiry countdown on pending actions

### UX flows (v1 spec)

- [ ] Rep opens opportunity → sees pending follow-up draft
- [ ] Rep copies draft → Twenty email composer (manual send path)
- [ ] Rep clicks Accept → CRM note / writer execution (backend already supports this)
- [ ] In-app notification when new pending action arrives (polling or subscription)

### Explicitly out of scope (v2+)

- [ ] One-click Send (`sendEmail` GraphQL) from DraftPreview
- [ ] Proposal PDF attachment UI
- [ ] `GET /followup/health/{id}` (branch uses `GET /followup/actions` instead)

---

## Backend APIs ready for UI (already implemented)

| Endpoint | Purpose |
|----------|---------|
| `GET /followup/actions` | List pending actions for opportunity tab |
| `POST /followup/actions/{id}/accept` | Accept → execute via writer |
| `POST /followup/actions/{id}/reject` | Reject pending action |
| `POST /followup/actions/{id}/revise` | Revise → re-run pipeline |
| `GET /followup/profile/{opportunity_id}` | Profile narrative |

Python service must be running (`AI_SERVICE_URL`, default `http://127.0.0.1:8001`).

---

## Suggested order when UI work starts

1. REST client + `useFollowupActions` hook
2. `DraftPreview` + copy button (no accept yet)
3. Accept button e2e against live backend
4. OpportunityHealthPanel + polish
5. Notifications (polling interval)

---

## Test plan (UI, when built)

- [ ] Opportunity with pending action renders DraftPreview fields
- [ ] Copy puts subject/body on clipboard
- [ ] Accept transitions action status; panel refetches
- [ ] Reject removes action from pending list
- [ ] No changes required in `twenty-ai-service` for basic v1 UI
