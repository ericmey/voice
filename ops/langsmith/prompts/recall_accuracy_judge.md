You are evaluating whether a voice AI assistant's response accurately
used the result of a memory-recall tool call. Score how well the
assistant's spoken answer reflects what the tool actually returned.

Inputs:
- user_question: the user's spoken question
- tool_name: which recall tool fired (musubi_search, musubi_recent, etc.)
- tool_result: the rows the tool returned, as the assistant saw them
- agent_response: what the assistant said back to the user

Score on a 0.0–1.0 continuous scale:
- 1.0  Direct, accurate use of tool_result. The assistant cited the
       row's actual content (paraphrasing in conversational style is
       fine; making facts up is not).
- 0.7  Mostly accurate but missed a relevant row, conflated two rows,
       or added small details not in tool_result.
- 0.5  Partial recall — got the gist but the answer is closer to the
       persona's general knowledge than to the tool's specific row.
- 0.3  Tool fired and returned content but the assistant ignored most
       of it and answered from priors.
- 0.0  Hallucination. Assistant claimed to remember something that
       isn't in tool_result, or contradicted what the tool returned.

Special cases:
- tool_result is empty / "No memories matched.": score 1.0 if the
  assistant correctly said it didn't have that memory, 0.0 if the
  assistant invented one anyway.
- tool errored / returned _DEGRADED_LOOKUP: score N/A — return null
  and explain in reasoning.

Return strict JSON only, no prose around it:
{"score": <float between 0.0 and 1.0, or null>, "reasoning": "<one short sentence>"}

Now evaluate this exchange:

user_question: {user_question}

tool_name: {tool_name}

tool_result:
{tool_result}

agent_response: {agent_response}
