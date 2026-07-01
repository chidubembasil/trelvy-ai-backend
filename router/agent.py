"""
Trelvy Multi-Agent Router
============================
Flow:
  1. User creates a MultiAgent with a name, prompt, and codingAgent type.
  2. OpenAI generates code files based on the prompt.
  3. Each generated file is stored as a SubAgent row linked to the MultiAgent.
  4. Tasks are assigned to the MultiAgent; the swarm coordinates SubAgents.
"""

import json
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Depends
from starlette import status
from openai import AsyncOpenAI

from database import db
from schema import MultiAgentDetails, MultiAgentCreate
from runtime.auth import get_current_user

import include.memory as mem
import include.coordination as coord

router = APIRouter(
    prefix="/multi-agents",
    tags=["Multi Agents"],
)

openai_client = AsyncOpenAI()
AGENT_MODEL = "gpt-4.1"


# ---------------------------------------------------------------------------
# Generate code files for an agent using OpenAI
# Returns a list of {name, filePath, content} dicts
# ---------------------------------------------------------------------------

async def generate_agent_files(name: str, prompt: str, coding_agent: str) -> list[dict]:
    system_prompt = (
        "You are an expert software architect. Given an agent name, prompt, and type, "
        "generate the necessary Python code files for that agent. "
        "Respond ONLY in JSON with this exact structure: "
        '{"files": [{"name": "filename.py", "path": "agents/<agent_name>/<filename>.py", "content": "...full python code..."}]}. '
        "Generate between 1 and 5 files depending on complexity. "
        "Each file should be a complete, standalone Python module."
    )
    user_prompt = json.dumps({
        "agent_name":   name,
        "prompt":       prompt,
        "coding_agent": coding_agent,
    })

    response = await openai_client.chat.completions.create(
        model=AGENT_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        response_format={"type": "json_object"},
    )
    result = json.loads(response.choices[0].message.content)
    return result.get("files", [])


# ---------------------------------------------------------------------------
# Task executor - used by the coordination lifecycle
# ---------------------------------------------------------------------------

async def trelvy_agent_executor(task, reasoning: dict) -> dict:
    system_prompt = (
        "You are an autonomous Trelvy agent. Execute the task below and respond "
        "ONLY in JSON with keys: success (bool), summary (string), output (string), "
        "learned_fact (string or null), learned_procedure (object or null)."
    )
    user_prompt = json.dumps({
        "goal":               task.goal,
        "description":        task.description,
        "execution_strategy": reasoning.get("execution_strategy"),
        "required_tools":     reasoning.get("required_tools", []),
    })

    response = await openai_client.chat.completions.create(
        model=AGENT_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        response_format={"type": "json_object"},
    )
    return json.loads(response.choices[0].message.content)


# ---------------------------------------------------------------------------
# Create a new MultiAgent + generate its SubAgent files via OpenAI
# ---------------------------------------------------------------------------

@router.post("/", response_model=MultiAgentDetails, status_code=status.HTTP_201_CREATED)
async def create_multi_agent(
    agent_details: MultiAgentCreate,
    current_user=Depends(get_current_user),
):
    # 1. Create the MultiAgent record first
    agent_unique_id = str(uuid.uuid4())
    new_agent = await db.multiagent.create(
        data={
            "uniqueId":    agent_unique_id,
            "name":        agent_details.name,
            "prompt":      agent_details.prompt,
            "filePath":    f"agents/{agent_details.name.lower().replace(' ', '_')}/",
            "version":     agent_details.version or "1.0.0",
            "status":      agent_details.status or "active",
            "codingAgent": agent_details.codingAgent or "CODEX",
            "userId":      current_user.id,
        }
    )

    if not new_agent:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to create multi-agent",
        )

    # 2. Ask OpenAI to generate the code files for this agent
    generated_files = await generate_agent_files(
        name=agent_details.name,
        prompt=agent_details.prompt,
        coding_agent=agent_details.codingAgent or "CODEX",
    )

    # 3. Each generated file becomes a SubAgent row linked to this MultiAgent
    for file in generated_files:
        sub_agent = await db.subagent.create(
            data={
                "uniqueId": str(uuid.uuid4()),
                "name":     file.get("name", "unknown.py"),
                "filePath": file.get("path", f"agents/{agent_details.name}/unknown.py"),
                "parentId": new_agent.id,
            }
        )

        # 4. Seed the SubAgent's semantic memory with its generated code
        await mem.owl("write", sub_agent.uniqueId, fact={
            "topic":      "generated_code",
            "fact":       file.get("content", ""),
            "confidence": 1.0,
            "source":     "openai_generation",
        })

    # 5. Register the MultiAgent with the Coordination Engine
    await coord.register_agent(
        name=agent_details.name,
        role=agent_details.codingAgent or "general",
        model=AGENT_MODEL,
    )

    # Return the agent with its sub-agents included
    return await db.multiagent.find_unique(
        where={"uniqueId": agent_unique_id},
        include={"subAgents": True},
    )



# ---------------------------------------------------------------------------
# Get all agents for the logged in user
# ---------------------------------------------------------------------------

@router.get("/", response_model=list[MultiAgentDetails], status_code=status.HTTP_200_OK)
async def get_all_multi_agents(
    current_user=Depends(get_current_user),
):
    return await db.multiagent.find_many(
        where={"userId": current_user.id},
        include={"subAgents": True},
    )


# ---------------------------------------------------------------------------
# Get a single agent with its sub-agents (files)
# ---------------------------------------------------------------------------

@router.get("/{unique_id}", response_model=MultiAgentDetails, status_code=status.HTTP_200_OK)
async def get_multi_agent(
    unique_id: str,
    current_user=Depends(get_current_user),
):
    agent = await db.multiagent.find_first(
        where={"uniqueId": unique_id, "userId": current_user.id},
        include={"subAgents": True},
    )
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )
    return agent


# ---------------------------------------------------------------------------
# Get agent's swarm state - memory + sub-agents + coordination snapshot
# ---------------------------------------------------------------------------

@router.get("/{unique_id}/state", status_code=status.HTTP_200_OK)
async def get_agent_swarm_state(
    unique_id: str,
    current_user=Depends(get_current_user),
):
    agent_record = await db.multiagent.find_first(
        where={"uniqueId": unique_id, "userId": current_user.id},
        include={"subAgents": True},
    )
    if not agent_record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found"
        )

    working_memory = await mem.goldfish("read", agent_record.uniqueId)
    episodic       = await mem.elephant("read", agent_record.uniqueId, limit=10)
    facts          = await mem.owl("read", agent_record.uniqueId, limit=10)

    return {
        "agent":              agent_record,
        "sub_agents":         agent_record.subAgents,
        "working_memory":     working_memory,
        "recent_experiences": episodic,
        "known_facts":        facts,
    }


# ---------------------------------------------------------------------------
# Delete an agent and all its sub-agents (cascade handled by Prisma)
# ---------------------------------------------------------------------------

@router.delete("/{unique_id}", status_code=status.HTTP_200_OK)
async def delete_multi_agent(
    unique_id: str,
    current_user=Depends(get_current_user),
):
    agent = await db.multiagent.find_first(
        where={"uniqueId": unique_id, "userId": current_user.id}
    )
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found or doesn't belong to you",
        )

    await coord.deregister_agent(agent.uniqueId)
    await db.multiagent.delete(where={"uniqueId": unique_id})
    return {"detail": "Agent and all its files deleted successfully"}