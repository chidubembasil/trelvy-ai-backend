"""
Trelvy Coordination Engine
============================
The Coordinator is the sole authority that assigns and completes tasks.
Agents never bypass it. They communicate, request help, and receive work
exclusively through the functions below - this is what makes the swarm
behave like a colony of autonomous-but-cooperative workers rather than a
single centralized brain doing everything itself.

Agent lifecycle (repeats until the Coordinator stops the agent):
  Receive Task -> Thinking Cap -> Read Memory -> Analyze Task ->
  Determine Confidence -> Determine Required Tools -> Determine Required Help
  -> Execute Task -> Update Memory -> Send Feedback -> Notify Coordinator ->
  Wait For Next Task
"""

import asyncio
import json
import time
import uuid
from enum import Enum
from typing import Any, Callable, Optional

import redis.asyncio as aioredis
from openai import AsyncOpenAI

from database import db
import include.memory as mem

redis_client = aioredis.from_url("redis://localhost:6379/0", decode_responses=True)
openai_client = AsyncOpenAI()

THINKING_MODEL = "gpt-4.1"

EVENT_CHANNEL = "trelvy:events"
MESSAGE_QUEUE_PREFIX = "trelvy:messages"
STIMULUS_LOW_THRESHOLD = 0.3
STIMULUS_HIGH_THRESHOLD = 0.7


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MessageType(str, Enum):
    REQUEST = "request"
    REPLY = "reply"
    BROADCAST = "broadcast"
    NOTIFICATION = "notification"
    WARNING = "warning"
    ERROR = "error"
    STATUS = "status"


class EventType(str, Enum):
    TASK_CREATED = "TaskCreated"
    TASK_ASSIGNED = "TaskAssigned"
    TASK_UPDATED = "TaskUpdated"
    TASK_COMPLETED = "TaskCompleted"
    TASK_FAILED = "TaskFailed"
    TASK_CANCELLED = "TaskCancelled"
    MEMORY_CREATED = "MemoryCreated"
    MEMORY_UPDATED = "MemoryUpdated"
    AGENT_JOINED = "AgentJoined"
    AGENT_LEFT = "AgentLeft"
    HELP_REQUESTED = "HelpRequested"
    HELP_RESOLVED = "HelpResolved"
    FEEDBACK_RECEIVED = "FeedbackReceived"
    EXECUTION_STARTED = "ExecutionStarted"
    EXECUTION_FINISHED = "ExecutionFinished"


# ---------------------------------------------------------------------------
# TASK API
# ---------------------------------------------------------------------------

async def get_task(task_id: str) -> Any:
    return await db.task.find_first(where={"uniqueId": task_id})


async def update_task(task_id: str, **fields) -> Any:
    task = await db.task.update(where={"uniqueId": task_id}, data=fields)
    await publish_event(EventType.TASK_UPDATED, {"task_id": task_id, "fields": fields})
    return task


async def create_task(goal: str, description: str, priority: int = 5,
                       owner: Optional[str] = None, parent_task: Optional[str] = None,
                       deadline: Optional[str] = None) -> Any:
    task = await db.task.create(data={
        "uniqueId": str(uuid.uuid4()),
        "goal": goal,
        "description": description,
        "priority": priority,
        "status": TaskStatus.PENDING.value,
        "owner": owner,
        "parentTask": parent_task,
        "deadline": deadline,
    })
    await publish_event(EventType.TASK_CREATED, {"task_id": task.uniqueId, "goal": goal})
    return task


async def complete_task(task_id: str, result: Optional[dict] = None, success: bool = True) -> Any:
    status_value = TaskStatus.COMPLETED.value if success else TaskStatus.FAILED.value
    task = await update_task(task_id, status=status_value, result=json.dumps(result or {}))
    await publish_event(
        EventType.TASK_COMPLETED if success else EventType.TASK_FAILED,
        {"task_id": task_id, "result": result},
    )
    return task


# ---------------------------------------------------------------------------
# AGENT STATE API
# ---------------------------------------------------------------------------

async def get_agent(agent_id: str) -> Any:
    return await db.agentstate.find_first(where={"uniqueId": agent_id})


async def list_agents(available_only: bool = False) -> Any:
    where = {"available": True} if available_only else {}
    return await db.agentstate.find_many(where=where)


async def update_agent_state(agent_id: str, **fields) -> Any:
    fields["lastActivity"] = time.time()
    agent = await db.agentstate.update(where={"uniqueId": agent_id}, data=fields)
    return agent


async def register_agent(name: str, role: str, model: str = "gpt-4.1") -> Any:
    agent = await db.agentstate.create(data={
        "uniqueId": str(uuid.uuid4()),
        "name": name,
        "role": role,
        "model": model,
        "status": "idle",
        "stimulusScore": 0.5,
        "thresholdScore": 0.5,
        "confidence": 0.5,
        "busy": False,
        "available": True,
        "lastActivity": time.time(),
    })
    await publish_event(EventType.AGENT_JOINED, {"agent_id": agent.uniqueId, "role": role})
    return agent


async def deregister_agent(agent_id: str) -> Any:
    await update_agent_state(agent_id, available=False, status="offline")
    await publish_event(EventType.AGENT_LEFT, {"agent_id": agent_id})
    return True


# ---------------------------------------------------------------------------
# COMMUNICATION API
# ---------------------------------------------------------------------------

async def send_message(sender: str, receiver: str, msg_type: MessageType,
                        content: dict, task_id: Optional[str] = None,
                        priority: int = 5) -> dict:
    message = {
        "message_id": str(uuid.uuid4()),
        "sender": sender,
        "receiver": receiver,
        "type": msg_type.value if isinstance(msg_type, MessageType) else msg_type,
        "priority": priority,
        "task_id": task_id,
        "content": content,
        "timestamp": time.time(),
    }
    queue_key = f"{MESSAGE_QUEUE_PREFIX}:{receiver}"
    await redis_client.rpush(queue_key, json.dumps(message))
    return message


async def receive_messages(agent_id: str, limit: int = 10) -> list[dict]:
    queue_key = f"{MESSAGE_QUEUE_PREFIX}:{agent_id}"
    messages = []
    for _ in range(limit):
        raw = await redis_client.lpop(queue_key)
        if not raw:
            break
        messages.append(json.loads(raw))
    return messages


async def ask_for_help(agent_id: str, role_needed: str, reason: str, task_id: str,
                        confidence: float, stimulus_score: float,
                        threshold_score: float, required_skills: Optional[list] = None,
                        priority: int = 5) -> dict:
    """Agents never directly assign work - help requests are routed by the Coordinator."""
    help_request = {
        "agent": agent_id,
        "role_needed": role_needed,
        "reason": reason,
        "priority": priority,
        "task_id": task_id,
        "confidence": confidence,
        "stimulus_score": stimulus_score,
        "threshold_score": threshold_score,
        "required_skills": required_skills or [],
    }
    await publish_event(EventType.HELP_REQUESTED, help_request)

    # Coordinator routes to the best matching available agent
    candidates = await list_agents(available_only=True)
    best_match = None
    for candidate in candidates:
        if candidate.role == role_needed and candidate.uniqueId != agent_id:
            best_match = candidate
            break

    if best_match:
        await send_message(
            sender="coordinator", receiver=best_match.uniqueId,
            msg_type=MessageType.REQUEST, content=help_request, task_id=task_id,
            priority=priority,
        )
        await publish_event(EventType.HELP_RESOLVED, {
            "task_id": task_id, "assigned_to": best_match.uniqueId,
        })

    return {"help_request": help_request, "routed_to": best_match.uniqueId if best_match else None}


async def send_feedback(agent_id: str, task_id: str, positive_score: float,
                         negative_score: float, reason: str) -> dict:
    agent = await get_agent(agent_id)
    confidence_change = (positive_score - negative_score) * 0.1
    stimulus_change = (positive_score - negative_score) * 0.15

    new_confidence = max(0.0, min(1.0, (agent.confidence or 0.5) + confidence_change))
    new_stimulus = max(0.0, min(1.0, (agent.stimulusScore or 0.5) + stimulus_change))

    await update_agent_state(agent_id, confidence=new_confidence, stimulusScore=new_stimulus)

    feedback = {
        "positive_score": positive_score, "negative_score": negative_score,
        "confidence_change": confidence_change, "stimulus_change": stimulus_change,
        "reason": reason,
    }
    await publish_event(EventType.FEEDBACK_RECEIVED, {"agent_id": agent_id, "task_id": task_id, **feedback})

    # Store the lesson in episodic memory
    await mem.elephant("write", agent_id, event={
        "summary": reason, "outcome": "success" if positive_score >= negative_score else "failure",
        "details": feedback, "importance": "high" if abs(positive_score - negative_score) > 0.5 else "medium",
        "task_id": task_id,
    })
    return feedback


# ---------------------------------------------------------------------------
# EVENT BUS (pub/sub)
# ---------------------------------------------------------------------------

async def publish_event(event_type: EventType, data: dict) -> dict:
    event = {
        "event": event_type.value if isinstance(event_type, EventType) else event_type,
        "data": data,
        "timestamp": time.time(),
    }
    await redis_client.publish(EVENT_CHANNEL, json.dumps(event))
    return event


async def subscribe_event(handler: Callable[[dict], Any], event_filter: Optional[list[str]] = None):
    """Long-running listener. Call as a background task per agent / coordinator process."""
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(EVENT_CHANNEL)
    async for message in pubsub.listen():
        if message["type"] != "message":
            continue
        event = json.loads(message["data"])
        if event_filter and event["event"] not in event_filter:
            continue
        await handler(event)


# ---------------------------------------------------------------------------
# STIMULUS SYSTEM
# ---------------------------------------------------------------------------

def stimulus_decision(stimulus_score: float, threshold_score: float) -> str:
    """High stimulus -> autonomous execution. Low stimulus -> collaboration.
    Very low stimulus -> trigger a help request."""
    if stimulus_score >= max(threshold_score, STIMULUS_HIGH_THRESHOLD):
        return "execute_autonomously"
    if stimulus_score <= STIMULUS_LOW_THRESHOLD:
        return "request_help"
    return "collaborate"


# ---------------------------------------------------------------------------
# THINKING CAP - runs before every action, never executes tools directly
# ---------------------------------------------------------------------------

async def thinking_cap(agent_id: str, task: Any) -> dict:
    agent = await get_agent(agent_id)

    # Read relevant memory before reasoning
    goldfish_notes = await mem.goldfish("read", agent_id)
    related_memories = await mem.bloodhound("search", agent_id, text=task.description, top_k=5)
    facts = await mem.owl("read", agent_id, topic=None, limit=10)

    system_prompt = (
        f"You are the reasoning module ('Thinking Cap') for agent '{agent.name}' "
        f"(role: {agent.role}). You never execute tools - you only produce structured "
        f"reasoning: understanding of the task, confidence, complexity, required tools, "
        f"required knowledge, and whether help is required. Respond ONLY in JSON with keys: "
        f"understanding, confidence (0-1), complexity (low/medium/high), required_tools (list), "
        f"required_knowledge (list), needs_help (bool), help_reason (string or null), "
        f"execution_strategy (string)."
    )

    user_prompt = json.dumps({
        "task_goal": task.goal,
        "task_description": task.description,
        "working_memory": goldfish_notes,
        "related_memories": related_memories,
        "known_facts": [f.fact for f in facts] if facts else [],
        "current_confidence": agent.confidence,
        "current_stimulus": agent.stimulusScore,
        "current_threshold": agent.thresholdScore,
    })

    response = await openai_client.chat.completions.create(
        model=THINKING_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
    )
    reasoning = json.loads(response.choices[0].message.content)

    # Persist the reasoning as a working-memory entry
    await mem.goldfish("write", agent_id, key=f"reasoning:{task.uniqueId}", value={
        "task_id": task.uniqueId, "reasoning": reasoning, "importance": "medium",
    })

    await update_agent_state(agent_id, thinking=True, confidence=reasoning.get("confidence", agent.confidence))

    return reasoning


# ---------------------------------------------------------------------------
# AGENT LIFECYCLE - the swarm execution loop
# ---------------------------------------------------------------------------

async def run_agent_cycle(agent_id: str, task_id: str, executor: Callable[[Any, dict], Any]):
    """
    Drives a single full lifecycle for one agent on one task:
    Receive -> Thinking Cap -> Read Memory -> Analyze -> Confidence ->
    Tools/Help -> Execute -> Update Memory -> Feedback -> Notify Coordinator.

    `executor` is the function that actually performs the task (e.g. calls
    OpenAI, runs code in a sandbox, hits an API) and must accept
    (task, reasoning) and return a result dict with at least {"success": bool}.
    """
    task = await get_task(task_id)
    agent = await get_agent(agent_id)

    await update_agent_state(agent_id, status="thinking", busy=True, current_task=task_id, current_goal=task.goal)
    await publish_event(EventType.EXECUTION_STARTED, {"agent_id": agent_id, "task_id": task_id})

    # Thinking Cap -> Read Memory -> Analyze -> Confidence (all inside thinking_cap)
    reasoning = await thinking_cap(agent_id, task)

    decision = stimulus_decision(agent.stimulusScore or 0.5, agent.thresholdScore or 0.5)

    if reasoning.get("needs_help") or decision == "request_help":
        help_result = await ask_for_help(
            agent_id=agent_id,
            role_needed=reasoning.get("required_role", agent.role),
            reason=reasoning.get("help_reason") or "Confidence below threshold",
            task_id=task_id,
            confidence=reasoning.get("confidence", 0.5),
            stimulus_score=agent.stimulusScore or 0.5,
            threshold_score=agent.thresholdScore or 0.5,
            required_skills=reasoning.get("required_tools", []),
        )
        await update_task(task_id, status=TaskStatus.WAITING.value)
        await update_agent_state(agent_id, status="waiting_for_help", busy=False)
        return {"status": "waiting_for_help", "help_result": help_result}

    # Execute
    await update_task(task_id, status=TaskStatus.RUNNING.value)
    await update_agent_state(agent_id, status="executing")

    try:
        result = await executor(task, reasoning)
        success = result.get("success", False)
    except Exception as exc:  # never silently fail
        result = {"success": False, "error": str(exc)}
        success = False

    # Update memory with what happened
    await mem.goldfish("write", agent_id, key=f"result:{task_id}", value={
        "task_id": task_id, "summary": result.get("summary", str(result)[:200]),
        "outcome": "success" if success else "failure",
        "importance": "high" if not success else "medium",
        "fact": result.get("learned_fact"),
        "procedure": result.get("learned_procedure"),
    })
    await mem.consolidate(agent_id)

    # Feedback
    await send_feedback(
        agent_id, task_id,
        positive_score=1.0 if success else 0.0,
        negative_score=0.0 if success else 1.0,
        reason=result.get("summary", "Task execution finished"),
    )

    # Notify Coordinator
    await complete_task(task_id, result=result, success=success)
    if not success:
        # store explicit failure for recovery analysis
        await mem.elephant("write", agent_id, event={
            "summary": f"Task {task_id} failed: {result.get('error', 'unknown error')}",
            "outcome": "failure", "details": result, "importance": "high", "task_id": task_id,
        })

    await update_agent_state(agent_id, status="idle", busy=False, current_task=None, current_goal=None, thinking=False)
    await publish_event(EventType.EXECUTION_FINISHED, {"agent_id": agent_id, "task_id": task_id, "success": success})

    return {"status": "completed" if success else "failed", "result": result}