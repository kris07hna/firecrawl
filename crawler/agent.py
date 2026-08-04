import json
import subprocess
from crawler.config import log, DEFAULT_MODEL

async def check_opencode() -> bool:
    try:
        res = subprocess.run(["opencode", "--version"], capture_output=True, text=True, timeout=5)
        return res.returncode == 0
    except Exception:
        return False

async def ask_opencode(prompt: str, model: str = DEFAULT_MODEL) -> str:
    log(f"Querying {model}...", "AI")
    try:
        res = subprocess.run(
            ["opencode", "run", "-m", model, prompt],
            capture_output=True,
            text=True,
            timeout=180
        )
        if res.returncode != 0:
            log(f"AI Error: {res.stderr.strip()}", "ERROR")
            return ""
        return res.stdout
    except Exception as e:
        log(f"AI Exception: {e}", "ERROR")
        return ""

def extract_json_block(text: str) -> str:
    start = text.find("```json")
    if start != -1:
        end = text.find("```", start + 7)
        if end != -1:
            return text[start+7:end].strip()
    
    # Fallback to finding first { and last }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end+1]
        
    return text.strip()

async def synthesize_complex_goal(goal: str, page_state: dict, history: list) -> dict:
    """
    Advanced LLM prompt for multi-step reasoning.
    Takes the current IA structure, visual elements, and previous history
    to decide the next best action.
    """
    prompt = f"""
You are an expert UX researcher and crawler. Your overarching goal is: {goal}

Current Page URL: {page_state.get('url')}
Current Page Semantic Structure (IA):
{json.dumps(page_state.get('ia', {}), indent=2)}

Available Interactive Elements:
{json.dumps(page_state.get('elements', [])[:20], indent=2)}

Past Action History:
{json.dumps(history, indent=2)}

Based on UX principles, formulate the next single action to progress towards the goal.
Respond ONLY with a JSON object in this format:
{{
  "reasoning": "Explain why this action helps achieve the goal",
  "action": "CLICK" or "TYPE" or "WAIT" or "SCROLL" or "DONE",
  "target_selector": "CSS selector of the element to interact with (if applicable)",
  "text_to_type": "Text to type (if action is TYPE)"
}}
"""
    response = await ask_opencode(prompt)
    json_text = extract_json_block(response)
    try:
        return json.loads(json_text)
    except Exception as e:
        log(f"Failed to parse LLM JSON: {e}", "ERROR")
        return {"action": "WAIT", "reasoning": "Fallback due to parse error"}
