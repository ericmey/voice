You are evaluating whether a voice AI assistant chose the RIGHT tool
for the user's question. Voice agents have a small set of tools they
can call; picking the wrong one (or none, or one too many) is the
single biggest correctness failure mode after hallucination.

Inputs:
- agent_name: which voice agent fired the call (different agents have
  different tool inventories — see below)
- user_question: what the user actually asked, in their own words
- tool_name: the tool the agent decided to call
- tool_result: what that tool returned
- agent_response: what the agent ended up saying back to the user

Tool inventories (current as of 2026-04):

Nyla (household router):
- musubi_search: semantic memory search across all of Eric's saved
  notes / preferences / past conversations
- musubi_recent: most-recent N entries from memory, no semantic
  filtering — for "what's new" / "what did we talk about"
- session_send: route a message into another agent's session
- get_time, get_weather: utility lookups
- (no calendar or task tools yet — defer to "I don't have that")

Aoi (code partner):
- musubi_search, musubi_recent: same as Nyla
- code-related tools (read_file, run_command, etc. — Aoi-only)
- (no household-routing tools)

Score categories:

- 0  CORRECT — the tool fired matches the question's intent. Examples:
     * "do you remember what we said about X" → musubi_search ✓
     * "what's the latest" → musubi_recent ✓
     * "what time is it" → get_time ✓
     * Question that genuinely has no tool match, agent answered from
       priors without calling one → also CORRECT.

- 1  WRONG TOOL — a tool was called, but a DIFFERENT one would have
     been right. Examples:
     * "remember what we discussed Tuesday" → musubi_recent (too
       recency-coupled, semantic search would have been better)
     * "what's the most recent thing we talked about" → musubi_search
       (recency lookup, not semantic)
     * "tell Aoi I'm heading out" → musubi_search instead of
       session_send

- 2  MISSING TOOL — should have called one, didn't. Examples:
     * Question about saved memory, agent answered from priors only
     * "what time is it" answered with "I don't have a clock" when
       get_time exists

- 3  EXTRA TOOL — called a tool when none was needed. Examples:
     * Casual greeting → musubi_search fired anyway
     * Question with no factual lookup (opinion, banter, persona) →
       tool fired anyway

Score on a 0.0–1.0 continuous scale:
- 1.0  Category 0 (CORRECT)
- 0.5  Category 1 or 3 (WRONG / EXTRA — partial credit because the
       agent recognised a tool was relevant, just picked badly)
- 0.0  Category 2 (MISSING — failed to recognise tool was needed)

Special cases:
- agent_response is empty → score 0.0 with reasoning "agent silent"
  (the call timed out or the run is corrupted; can't judge intent).
- tool_result indicates an internal error → don't penalise the
  agent's choice; score the choice on its merits and note the error
  in reasoning.

Return strict JSON only, no prose around it:
{"score": <float between 0.0 and 1.0>, "category": <0|1|2|3>, "reasoning": "<one short sentence>"}

Now evaluate this exchange:

agent_name: {agent_name}

user_question: {user_question}

tool_name: {tool_name}

tool_result:
{tool_result}

agent_response: {agent_response}
