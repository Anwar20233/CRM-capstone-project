"""Conversational chat interface for the Follow-Up Intelligence Agent.

Exposes ``run_followup_chat`` — a tool-using LLM agent embedded on the
opportunity record. It surfaces pending follow-up actions and lets the rep
accept / reject / revise them or ask for new follow-ups in natural language,
reusing the same operations as the structured REST endpoints.
"""

from followup.chat.agent import FollowupChatResult, run_followup_chat

__all__ = ["FollowupChatResult", "run_followup_chat"]
