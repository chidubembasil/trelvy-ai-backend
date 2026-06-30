"""
Trelvy Multi-Agent Router
============================
Creates and manages autonomous agents. Agent "intelligence" now comes from
OpenAI (gpt-4.1) instead of Codex. Every agent created here is registered
with the Coordination Engine and given a memory namespace, so it
automatically becomes part of the swarm: it can think (Thinking Cap), be
assigned tasks, request help from other agents, and learn over time.
"""

import json
import uuid

from fastapi import APIRouter, HTTPException, Depends
from starlette import status
from openai import AsyncOpenAI
from dotenv import load_dotenv
load_dotenv()

from database import db
from schema import MultiAgentDetails, AgentTaskRequest
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
# Agent intelligence: OpenAI replaces the old Codex code-generation call.
# This is the `executor` plugged into coord.run_agent_cycle for this agent type.
# ---------------------------------------------------------------------------

async def trelvy_agent_executor(task, reasoning: dict) -> dict:
    """
    Executes a task using OpenAI, guided by the Thinking Cap's reasoning.
    Returns a structured result that the Coordinator/Memory system consumes.
    """
    system_prompt = (
        "You are an autonomous Trelvy agent. You have already reasoned about "
        "this task (see the strategy below). Execute it and respond ONLY in "
        "JSON with keys: success (bool), summary (string), output (string), "
        "learned_fact (string or null), learned_procedure (object or null - "
        "{name, steps} if you discovered a reusable workflow)."
    )
    user_prompt = json.dumps({
        "goal": task.goal,
        "description": task.description,
        "execution_strategy": reasoning.get("execution_strategy"),
        "required_tools": reasoning.get("required_tools", []),
    })

    response = await openai_client.chat.completions.create(
        model=AGENT_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
    )
    return json.loads(response.choices[0].message.content)


# ---------------------------------------------------------------------------
# Create a new multi-agent for the logged in user
# ---------------------------------------------------------------------------

@router.post("/", response_model=MultiAgentDetails, status_code=status.HTTP_201_CREATED)
async def create_multi_agent(
    agent_details: MultiAgentDetails,
    current_user=Depends(get_current_user),
):
    new_agent = await db.multiAgent.create(
        data={
            "uniqueId": str(uuid.uuid4()),
            "name": agent_details.name,
            "prompt": agent_details.prompt,
            "filePath": agent_details.filePath,
            "version": agent_details.version,
            "status": agent_details.status,
            "codingAgent": agent_details.codingAgent,
            "userId": current_user.id,
        }
    )

    if not new_agent:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to create multi-agent",
        )

    # Register the agent with the Coordination Engine -> joins the swarm
    swarm_agent = await coord.register_agent(
        name=agent_details.name,
        role=agent_details.codingAgent or "general",
        model=AGENT_MODEL,
    )

    # Link the swarm agent id back onto the multi-agent record
    new_agent = await db.multiAgent.update(
        where={"uniqueId": new_agent.uniqueId},
        data={"swarmAgentId": swarm_agent.uniqueId},
    )

    # Seed semantic memory with the agent's founding prompt/instructions
    await mem.owl("write", swarm_agent.uniqueId, fact={
        "topic": "identity",
        "fact": agent_details.prompt,
        "confidence": 1.0,
        "source": "agent_creation",
    })

    return new_agent


# ---------------------------------------------------------------------------
# Assign a task to an agent - runs the full coordination lifecycle
# ---------------------------------------------------------------------------

@router.post("/{unique_id}/tasks", status_code=status.HTTP_200_OK)
async def assign_task(
    unique_id: str,
    task_request: AgentTaskRequest,
    current_user=Depends(get_current_user),
):
    agent_record = await db.multiAgent.find_first(
        where={"uniqueId": unique_id, "userId": current_user.id}
    )
    if not agent_record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if not agent_record.swarmAgentId:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Agent is not registered with the Coordination Engine",
        )

    task = await coord.create_task(
        goal=task_request.goal,
        description=task_request.description,
        priority=task_request.priority or 5,
        owner=agent_record.swarmAgentId,
    )

    result = await coord.run_agent_cycle(
        agent_id=agent_record.swarmAgentId,
        task_id=task.uniqueId,
        executor=trelvy_agent_executor,
    )

    return {"task_id": task.uniqueId, **result}


# ---------------------------------------------------------------------------
# Get all multi-agents for the logged in user only
# ---------------------------------------------------------------------------

@router.get("/", response_model=list[MultiAgentDetails], status_code=status.HTTP_200_OK)
async def get_all_multi_agents(
    current_user=Depends(get_current_user),
):
    agents = await db.multiAgent.find_many(
        where={"userId": current_user.id}
    )
    return agents


# ---------------------------------------------------------------------------
# Get a single multi-agent by uniqueId (only if it belongs to the user)
# ---------------------------------------------------------------------------

@router.get("/{unique_id}", response_model=MultiAgentDetails, status_code=status.HTTP_200_OK)
async def get_multi_agent(
    unique_id: str,
    current_user=Depends(get_current_user),
):
    agent = await db.multiAgent.find_first(
        where={
            "uniqueId": unique_id,
            "userId": current_user.id,
        }
    )
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )
    return agent


# ---------------------------------------------------------------------------
# Get an agent's full swarm state - memory + coordination snapshot
# ---------------------------------------------------------------------------

@router.get("/{unique_id}/state", status_code=status.HTTP_200_OK)
async def get_agent_swarm_state(
    unique_id: str,
    current_user=Depends(get_current_user),
):
    agent_record = await db.multiAgent.find_first(
        where={"uniqueId": unique_id, "userId": current_user.id}
    )
    if not agent_record or not agent_record.swarmAgentId:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    swarm_id = agent_record.swarmAgentId
    agent_state = await coord.get_agent(swarm_id)
    working_memory = await mem.goldfish("read", swarm_id)
    episodic = await mem.elephant("read", swarm_id, limit=10)
    facts = await mem.owl("read", swarm_id, limit=10)

    return {
        "agent_state": agent_state,
        "working_memory": working_memory,
        "recent_experiences": episodic,
        "known_facts": facts,
    }


# ---------------------------------------------------------------------------
# Delete a multi-agent (only if it belongs to the user)
# ---------------------------------------------------------------------------

@router.delete("/{unique_id}", status_code=status.HTTP_200_OK)
async def delete_multi_agent(
    unique_id: str,
    current_user=Depends(get_current_user),
):
    agent = await db.multiAgent.find_first(
        where={
            "uniqueId": unique_id,
            "userId": current_user.id,
        }
    )
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found or doesn't belong to you",
        )

    if agent.swarmAgentId:
        await coord.deregister_agent(agent.swarmAgentId)

    await db.multiAgent.delete(where={"uniqueId": unique_id})
    return {"detail": "Agent deleted successfully"}