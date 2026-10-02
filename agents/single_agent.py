import os
import sys
import json
from ollama import chat

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rag.retrieval import search_news
from rag.refresh import refresh_index

# Free, locally-running open-source model (served via Ollama)
MODEL = "qwen2.5:7b"

SYSTEM_PROMPT = """You are a news research assistant. Before answering the user's question,
use the search_news tool to find relevant news articles. Among the results the tool returns,
only use the ones that are genuinely relevant - ignore irrelevant or noisy results.
When you answer, cite which article each piece of information came from (source name and link).
If none of the returned results are relevant enough, say so clearly instead of making things up."""

# Ollama's tool definition format - similar to Claude's, but with OpenAI-style schema differences
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_news",
            "description": (
                "Runs a semantic search against a vector database of current Turkish news. "
                "Returns the most semantically relevant articles, including source and link."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The topic to search for (e.g. 'technology news', 'economy updates')",
                    },
                    "n_results": {
                        "type": "integer",
                        "description": "How many results to return (default 5)",
                    },
                },
                "required": ["query"],
            },
        },
    }
]


def call_tool(tool_name, tool_input):
    """Actually executes the tool the model requested."""
    if tool_name == "search_news":
        query = tool_input["query"]
        n_results = tool_input.get("n_results", 5)
        results = search_news(query, n_results)
        return json.dumps(results, ensure_ascii=False)
    return json.dumps({"error": f"Unknown tool: {tool_name}"})


def run_agent(user_question: str, max_steps: int = 5):
    # Pull the latest articles from RSS and index only the new ones before
    # answering - this is what makes each run reflect current news, not a
    # stale snapshot from whenever build_index.py was last run in full.
    refresh_index()

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_question},
    ]

    for step in range(max_steps):
        print(f"\n--- Step {step + 1}: sending request to {MODEL} ---")

        response = chat(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
        )

        message = response["message"]

        # If the model didn't request a tool call, it produced a final answer
        if not message.get("tool_calls"):
            print(f"[TEXT] {message['content']}")
            print("\n--- FINAL ANSWER ---")
            print(message["content"])
            return message["content"]

        # Append the model's message (including tool_calls) to the conversation history
        messages.append(message)

        # Execute every requested tool call and feed the result back into the history
        for tool_call in message["tool_calls"]:
            tool_name = tool_call["function"]["name"]
            tool_input = tool_call["function"]["arguments"]
            print(f"[TOOL_USE] {tool_name}({tool_input})")

            result = call_tool(tool_name, tool_input)
            print(f"[TOOL_RESULT] {result[:200]}...")

            messages.append({
                "role": "tool",
                "content": result,
            })

    print("Reached max step count, stopping the loop.")
    return None


if __name__ == "__main__":
    # Note: the actual question sent to the agent stays in Turkish since the
    # news corpus itself is Turkish - this is real input data, not a comment.
    run_agent("Bugün spor ile ilgili hangi haberler var, özetler misin?")