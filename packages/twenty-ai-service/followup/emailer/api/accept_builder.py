from followup.emailer.agents.drafting.schemas import EmailDraft, ProposalDraft, ProposalSection

# Deprecated: v1 accept now routes through FollowupActionExecutor in
# followup/api/execution.py (build_send_email_args / send_drafted_email).


def format_proposal_sections(draft: ProposalDraft) -> str:
    return "\n\n".join(
        f"## {section.heading}\n{section.content}" for section in draft.sections
    )


def build_crm_instruction_for_draft(
    draft: EmailDraft | ProposalDraft,
    opportunity_id: str,
) -> str:
    if isinstance(draft, EmailDraft):
        return (
            f"Create a note on opportunity {opportunity_id} with this draft email:\n"
            f"Subject: {draft.subject}\n\n{draft.body}"
        )

    formatted_sections = format_proposal_sections(draft)
    return (
        f"Create a note on opportunity {opportunity_id} with this proposal draft:\n"
        f"Title: {draft.title}\n\n{formatted_sections}"
    )
