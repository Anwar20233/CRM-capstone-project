# System Capability Scorecard

_Agentic CRM — Chat orchestrator, Follow-up agent — evaluated on 60 real end-to-end scenarios._

**Overall system reliability: 96%**  
**Fully-correct scenarios: 46/60**

| Capability | Score | Measured on | What it means |
|---|---:|---|---|
| **Completes the request** | 97% | 88/91 checks | Carries out the action asked for, or answers the question correctly. |
| **Uses the right tools** | 99% | 83/84 checks | Selects the correct specialist agents and CRM tools and calls them properly. |
| **Handles problems safely** | 100% | 7/7 checks | When data is missing, ambiguous, or invalid it stops and asks or refuses — it never guesses or fabricates a record. |
| **Protects personal data** | 99.9% | 691/692 AI calls with no exposed personal data  (1 exposed in 692 AI calls) | Masks names, emails, and phone numbers before they reach any AI model, and never exposes them in its replies. |
| **Stays grounded & accurate** | 87% | 101/116 checks | Acts on real records with the correct identifiers and returns a factual, reviewable result — no hallucinated data. |

## By component

| Component | Reliability | Fully correct |
|---|---:|---:|
| Chat orchestrator | 94% | 25/28 |
| Follow-up agent | 97% | 21/32 |

---
_Capabilities 1–4 are scored at the scenario level (a scenario counts only when every hard check testing it passed; soft checks are advisories that never reduce the score). Data privacy is a leak rate measured over every AI call made._
